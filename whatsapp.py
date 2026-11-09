"""WhatsApp gönderim katmanı.

Üç sağlayıcı desteklenir (Ayarlar sayfasından seçilir):

* ``simulation`` : Mesajı gerçekten göndermez, günlüğe yazar. Panelin ve
  otomatik motorun internet/WhatsApp hesabı olmadan test edilmesini sağlar.
* ``pywhatkit``  : WhatsApp Web'i masaüstü tarayıcıda açar ve mesajı yazar.
  Masaüstü oturumu + WhatsApp Web'e giriş yapılmış tarayıcı gerekir.
* ``http``       : Baileys / Puppeteer / WAHA gibi bir HTTP WhatsApp
  ağ geçidine JSON POST eder. Ağ geçidi ayrı çalışır (isteğe bağlı).

Bu modül Flask'tan ve veritabanından bağımsızdır; yalnızca ayar sözlüğü
alır. Böylece tek başına test edilebilir.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 900  # WhatsApp pratik sınırı (4096 karakterin çok altında)

PROVIDERS = {
    "simulation": "Simülasyon (gerçek mesaj göndermez)",
    "pywhatkit": "pywhatkit (WhatsApp Web, masaüstü)",
    "http": "HTTP API (Baileys / Puppeteer / WAHA)",
}

_TR_FOLD = str.maketrans(
    {
        "İ": "i", "I": "i", "ı": "i",
        "Ş": "s", "ş": "s",
        "Ğ": "g", "ğ": "g",
        "Ü": "u", "ü": "u",
        "Ö": "o", "ö": "o",
        "Ç": "c", "ç": "c",
    }
)
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


# `python main.py --demo` ile eklenen örnek müşterilerin numaraları. Uydurmadır ama
# gerçek birine ait olabilir: bu numaralara yalnızca simülasyonda "gönderilir".
DEMO_PHONES = frozenset(
    {
        "905551112233",
        "905322445566",
        "905443778899",
        "905554009911",
        "905365123456",
    }
)


class WhatsAppError(Exception):
    """Gönderim sırasında oluşan, kullanıcıya gösterilebilir hata."""


# ---------------------------------------------------------------------
# Telefon numarası
# ---------------------------------------------------------------------
def normalize_phone(raw: str) -> str:
    """Türk telefon numarasını ``90XXXXXXXXXX`` biçimine getirir.

    Kabul edilen girdiler: ``0555 111 22 33``, ``+90 555 111 22 33``,
    ``90 555 111 22 33``, ``5551112233``, ``0090...``.

    Raises:
        ValueError: Numara geçersizse (kullanıcıya gösterilebilir mesajla).
    """
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        raise ValueError("Telefon numarası boş olamaz.")
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("90") and len(digits) > 10:
        subscriber = digits[2:]
    elif digits.startswith("0"):
        subscriber = digits[1:]
    else:
        subscriber = digits
    if len(subscriber) != 10:
        raise ValueError(
            "Telefon numarası 10 haneli olmalı. Örnek: 0555 111 22 33 veya +90 555 111 22 33."
        )
    if subscriber[0] not in "2345":
        raise ValueError("Telefon numarası geçersiz görünüyor (alan kodu 2-5 ile başlamalı).")
    return "90" + subscriber


def format_phone_display(phone: str) -> str:
    """``905551112233`` -> ``+90 555 111 22 33``"""
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 12 and digits.startswith("90"):
        return f"+{digits[:2]} {digits[2:5]} {digits[5:8]} {digits[8:10]} {digits[10:]}"
    if len(digits) == 10:
        return f"+90 {digits[:3]} {digits[3:6]} {digits[6:8]} {digits[8:]}"
    return "+" + digits if digits else ""


def mask_phone(phone: str) -> str:
    """Günlük kayıtları için: ``905551112233`` -> ``+90 555 *** ** 33``."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 12 and digits.startswith("90"):
        return f"+90 {digits[2:5]} *** ** {digits[10:]}"
    return "***" + digits[-2:] if digits else ""


def wa_chat_id(phone: str) -> str:
    """Baileys/WAHA biçimindeki sohbet kimliği."""
    digits = re.sub(r"\D", "", str(phone or ""))
    return f"{digits}@c.us"


# ---------------------------------------------------------------------
# Şablon doldurma
# ---------------------------------------------------------------------
def _fold(text: str) -> str:
    return str(text).translate(_TR_FOLD).lower().strip()


def render_message(template: str, context: dict[str, object]) -> str:
    """``{isim}``, ``{saat}`` gibi yer tutucuları doldurur.

    Yer tutucular büyük/küçük harf ve Türkçe karakter farkı gözetmeden
    eşleşir: ``{isim}``, ``{Isim}`` ve ``{İsim}`` aynı sonucu verir.
    Bilinmeyen yer tutucular metinde olduğu gibi bırakılır.
    """
    normalized = {_fold(key): ("" if value is None else str(value)) for key, value in context.items()}

    def replace(match: re.Match[str]) -> str:
        key = _fold(match.group(1))
        if key in normalized:
            return normalized[key]
        logger.warning("Bilinmeyen mesaj yer tutucusu: {%s}", match.group(1))
        return match.group(0)

    return _PLACEHOLDER.sub(replace, str(template or ""))


def clean_body(text: str) -> str:
    """Mesajı kırpıp uzunluk sınırına getirir."""
    body = str(text or "").strip()
    if len(body) > MAX_MESSAGE_LENGTH:
        body = body[: MAX_MESSAGE_LENGTH - 1].rstrip() + "…"
    return body


# ---------------------------------------------------------------------
# Gönderim
# ---------------------------------------------------------------------
def provider_label(provider: str) -> str:
    return PROVIDERS.get(provider, provider or "bilinmiyor")


def provider_status(settings: dict[str, str]) -> tuple[bool, str]:
    """Sağlayıcının kullanıma hazır olup olmadığını döndürür."""
    provider = settings.get("whatsapp_provider") or "simulation"
    if provider == "simulation":
        return True, "Simülasyon modu: mesajlar gönderilmez, panelde günlüğe yazılır."
    if provider == "pywhatkit":
        try:
            import pywhatkit  # noqa: F401
        except ImportError:
            return False, "pywhatkit kurulu değil. 'pip install pywhatkit' çalıştırın."
        return True, "pywhatkit hazır. Gönderim sırasında tarayıcı açılır, sayfayı kapatmayın."
    if provider == "http":
        url = (settings.get("http_api_url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return False, "HTTP API adresi geçersiz (http:// veya https:// ile başlamalı)."
        return True, "HTTP ağ geçidi yapılandırıldı."
    return False, f"Bilinmeyen sağlayıcı: {provider}"


def check_connection(settings: dict[str, str], timeout: float = 4.0) -> dict[str, object]:
    """WhatsApp ağ geçidinin (Baileys/WAHA) bağlı olup olmadığını yoklar.

    Returns:
        ``{"state": "online"|"offline"|"simulation"|"unknown", "detail": str}``
    """
    provider = (settings.get("whatsapp_provider") or "simulation").strip()
    if provider == "simulation":
        return {"state": "simulation", "detail": "Simülasyon modu: gerçek mesaj gönderilmez."}
    if provider == "pywhatkit":
        ready, message = provider_status(settings)
        return {
            "state": "unknown" if ready else "offline",
            "detail": message if not ready else "pywhatkit bağlantı durumunu bildirmez; test mesajıyla deneyin.",
        }
    if provider != "http":
        return {"state": "offline", "detail": f"Bilinmeyen sağlayıcı: {provider}"}

    base_url = (settings.get("http_api_url") or "").strip()
    if not base_url.startswith(("http://", "https://")):
        return {"state": "offline", "detail": "HTTP API adresi ayarlanmamış."}
    mode = (settings.get("http_mode") or "generic").strip().lower()
    if mode == "waha":
        session = (settings.get("http_session") or "default").strip() or "default"
        url = f"{base_url.rstrip('/')}/api/sessions/{urllib.parse.quote(session, safe='')}"
    else:
        # Genel Baileys ağ geçidinin standart durum ucu yoktur: adres ulaşılabilir mi?
        url = base_url
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    token = (settings.get("http_api_token") or "").strip() or os.environ.get("ESNAF_HTTP_TOKEN", "")
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = getattr(response, "status", 200)
            raw = response.read(2000)
    except urllib.error.HTTPError as exc:
        if mode != "waha" and exc.code < 500:
            # Sunucu yanıt veriyor (ör. GET'e 404/405): ağ geçidi ayakta.
            return {"state": "online", "detail": f"Ağ geçidi yanıt veriyor (HTTP {exc.code})."}
        return {"state": "offline", "detail": f"Ağ geçidi HTTP {exc.code} döndürdü."}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return {"state": "offline", "detail": f"Ağ geçidine ulaşılamadı: {reason}"}

    if mode != "waha":
        return {"state": "online", "detail": f"Ağ geçidi yanıt veriyor (HTTP {status_code})."}
    try:
        session_status = str(json.loads(raw.decode("utf-8", "replace")).get("status") or "")
    except (ValueError, AttributeError):
        session_status = ""
    if session_status.upper() == "WORKING":
        return {"state": "online", "detail": "WhatsApp oturumu bağlı (WORKING)."}
    return {
        "state": "offline",
        "detail": f"WhatsApp oturumu bağlı değil (durum: {session_status or 'bilinmiyor'}). QR kodu tekrar okutun.",
    }


def send(phone: str, text: str, settings: dict[str, str]) -> dict[str, object]:
    """Mesajı seçili sağlayıcı ile gönderir.

    Returns:
        ``{"ok": bool, "provider": str, "simulated": bool, "detail": str}``
    """
    provider = (settings.get("whatsapp_provider") or "simulation").strip()
    body = clean_body(text)
    result: dict[str, object] = {
        "ok": False,
        "provider": provider,
        "simulated": provider == "simulation",
        "detail": "",
    }
    if not body:
        result["detail"] = "Mesaj metni boş olamaz."
        return result
    if not re.fullmatch(r"90\d{10}", str(phone or "")):
        result["detail"] = "Telefon numarası normalize edilmemiş."
        return result
    if provider != "simulation" and phone in DEMO_PHONES:
        result["detail"] = (
            "Bu bir demo (örnek) müşteri numarası; gerçek mesaj gönderilmez. "
            "Demo randevularını silip kendi müşterilerinizi ekleyin."
        )
        return result

    try:
        if provider == "simulation":
            detail = _send_simulation(phone, body)
        elif provider == "pywhatkit":
            detail = _send_pywhatkit(phone, body)
        elif provider == "http":
            detail = _send_http(phone, body, settings)
        else:
            raise WhatsAppError(f"Bilinmeyen WhatsApp sağlayıcısı: {provider}")
    except WhatsAppError as exc:
        logger.warning("WhatsApp gönderimi başarısız (%s): %s", provider, exc)
        result["detail"] = str(exc)
        return result
    except Exception as exc:  # beklenmeyen hata motoru durdurmasın
        logger.exception("WhatsApp gönderiminde beklenmeyen hata")
        result["detail"] = f"Beklenmeyen hata: {exc}"
        return result

    result["ok"] = True
    result["detail"] = detail
    return result


def _send_simulation(phone: str, body: str) -> str:
    # Kişisel veri günlüğe yazılmaz: numara maskelenir, mesaj metni yazılmaz.
    logger.info("[SİMÜLASYON] %s -> %d karakterlik mesaj", mask_phone(phone), len(body))
    return "Simülasyon: mesaj gerçekten gönderilmedi, günlüğe kaydedildi."


def _send_pywhatkit(phone: str, body: str) -> str:
    try:
        import pywhatkit
    except ImportError as exc:  # pragma: no cover - ortama bağlı
        raise WhatsAppError(
            "pywhatkit kurulu değil. 'pip install pywhatkit' komutunu çalıştırın."
        ) from exc
    try:  # pragma: no cover - masaüstü/WhatsApp Web gerektirir
        pywhatkit.sendwhatmsg_instantly(
            f"+{phone}",
            body,
            wait_time=20,
            tab_close=True,
            close_time=4,
        )
    except Exception as exc:  # pragma: no cover
        raise WhatsAppError(
            "pywhatkit gönderimi başarısız oldu. WhatsApp Web'in açık ve giriş yapılmış "
            f"olduğundan emin olun. Ayrıntı: {exc}"
        ) from exc
    return "pywhatkit ile WhatsApp Web üzerinden gönderim tetiklendi."


def _send_http(phone: str, body: str, settings: dict[str, str]) -> str:
    base_url = (settings.get("http_api_url") or "").strip()
    if not base_url.startswith(("http://", "https://")):
        raise WhatsAppError("HTTP API adresi geçersiz. Ayarlar sayfasından kontrol edin.")

    mode = (settings.get("http_mode") or "generic").strip().lower()
    if mode == "waha":
        endpoint = base_url.rstrip("/") + "/api/sendText"
        payload: dict[str, object] = {
            "session": (settings.get("http_session") or "default").strip() or "default",
            "chatId": wa_chat_id(phone),
            "text": body,
        }
    else:
        endpoint = base_url
        payload = {"to": phone, "phone": phone, "message": body}

    token = (settings.get("http_api_token") or "").strip() or os.environ.get(
        "ESNAF_HTTP_TOKEN", ""
    )
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            status = getattr(response, "status", 200)
            raw = response.read(500)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", "replace")
        except Exception:
            pass
        raise WhatsAppError(
            f"HTTP API {exc.code} döndürdü. {detail or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise WhatsAppError(
            f"HTTP API'ye ulaşılamadı: {exc.reason}. Ağ geçidinin çalıştığını kontrol edin."
        ) from exc
    except TimeoutError as exc:
        raise WhatsAppError("HTTP API 25 saniye içinde yanıt vermedi.") from exc

    if status >= 300:
        raise WhatsAppError(f"HTTP API beklenmeyen durum kodu döndürdü: {status}")
    snippet = raw.decode("utf-8", "replace").strip()
    return f"HTTP API yanıtı: {status} {snippet[:120]}".strip()
