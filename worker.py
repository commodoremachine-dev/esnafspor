"""Hatırlatma motorunun iş mantığı.

İki iş türü vardır (randevu başına birer kez):

1. ``confirmation`` — randevudan **2 saat önce** (ayarlanabilir):
   "Merhaba {isim}, {ne_zaman} saat {saat} randevunuzu onaylıyor musunuz?"
2. ``review`` — randevu **bittikten 1 saat sonra** (ayarlanabilir):
   "Hizmetimizi değerlendirmek için 1 dakikanızı ayırır mısınız? Google Yorum Linki: {link}"

Bu modül yalnızca "bir tur çalıştır / tek mesaj gönder" işlevlerini içerir.
Döngü, zamanlama, tekil örnek kilidi ve heartbeat `scheduler.py` dosyasındadır.

Çift gönderim koruması: `db.claim_reminder` ile atomik sahiplenme (kira /
`lease_until`). Aynı mesajı panel motoru, harici scheduler ve "Şimdi gönder"
düğmesi aynı anda göndermeye çalışsa bile yalnızca biri devam eder. (Süreç tam
gönderim anında çökerse mesaj en fazla bir kez daha denenebilir; WhatsApp
tarafında idempotency anahtarı yoktur.)

pywhatkit ekrandaki tarayıcıyı ve klavyeyi kullandığı için yalnızca o sağlayıcıda
gönderimler tek sıraya alınır; HTTP ve simülasyon gönderimleri birbirini beklemez.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any

import config
import db
import utils
import whatsapp

logger = logging.getLogger(__name__)

# pywhatkit aynı anda tek gönderim kaldırabilir (ekran/klavye paylaşılır).
_screen_lock = threading.Lock()
_SCREEN_LOCK_TIMEOUT = 45  # saniye
SERIALIZED_PROVIDERS = ("pywhatkit",)

MANUAL_RESEND_GUARD_SECONDS = 120
_manual_inflight: set[int] = set()
_manual_inflight_lock = threading.Lock()

KIND_LABELS = {
    "confirmation": "Randevu onay mesajı",
    "review": "Google yorum mesajı",
}


# ---------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------
def _relative_day(local_start: datetime, now_local: datetime) -> str:
    """'bugün', 'yarın' veya '12.11.2026 Perşembe'."""
    delta_days = (local_start.date() - now_local.date()).days
    if delta_days == 0:
        return "bugün"
    if delta_days == 1:
        return "yarın"
    return f"{utils.fmt_date(local_start)} {utils.weekday_tr(local_start)}"


def _build_context(item: dict[str, Any], settings: dict[str, str]) -> dict[str, str]:
    """Mesaj şablonundaki yer tutucuların değerlerini hazırlar."""
    tz_name = settings.get("timezone") or config.DEFAULT_TIMEZONE
    now_local = utils.local_now(tz_name)
    starts_at = utils.parse_iso(item.get("starts_at"))
    local_start = utils.to_tz(starts_at, tz_name) if starts_at else now_local
    review_link = (settings.get("google_review_url") or "").strip()
    if not review_link and (settings.get("whatsapp_provider") or "simulation") == "simulation":
        review_link = "https://g.page/r/ORNEK-LINK (Ayarlar'dan güncelleyin)"
    return {
        "isim": item.get("customer_name") or "Değerli müşterimiz",
        "saat": utils.fmt_time(local_start),
        "tarih": utils.fmt_date(local_start),
        "gun": utils.weekday_tr(local_start),
        "ne_zaman": _relative_day(local_start, now_local),
        "isletme": (settings.get("business_name") or "İşletmemiz").strip(),
        "link": review_link,
        "hizmet": item.get("service") or "randevunuz",
        "telefon": whatsapp.format_phone_display(str(item.get("customer_phone") or "")),
    }


def _template_for(kind: str, settings: dict[str, str]) -> str:
    key = {"confirmation": "msg_confirmation", "review": "msg_review", "manual": "msg_manual"}[kind]
    return settings.get(key) or db.SETTINGS_DEFAULTS[key]


def _send(phone: str, body: str, settings: dict[str, str]) -> dict[str, Any]:
    """Mesajı gönderir; yalnızca ekran kullanan sağlayıcıda sıraya girer."""
    provider = (settings.get("whatsapp_provider") or "simulation").strip()
    if provider not in SERIALIZED_PROVIDERS:
        return dict(whatsapp.send(phone, body, settings))
    if not _screen_lock.acquire(timeout=_SCREEN_LOCK_TIMEOUT):
        return {
            "ok": False,
            "busy": True,
            "provider": provider,
            "simulated": False,
            "detail": "Başka bir mesaj gönderiliyor; sıra gelince tekrar denenecek.",
        }
    try:
        return dict(whatsapp.send(phone, body, settings))
    finally:
        _screen_lock.release()


def _skip_reason(
    reminder: dict[str, Any], settings: dict[str, str], now: datetime, force: bool
) -> tuple[str, str] | None:
    """Gönderim yapılmaması gerekiyorsa (kod, Türkçe gerekçe) döndürür."""
    appointment_status = reminder.get("appointment_status") or "scheduled"
    if appointment_status in db.INACTIVE_STATUSES:
        return db.SKIP_INACTIVE, "Randevu iptal edilmiş veya müşteri gelmemiş."
    if int(reminder.get("customer_opted_out") or 0):
        return db.SKIP_OPTED_OUT, "Müşteri mesaj almak istemiyor."

    if reminder.get("kind") == "confirmation":
        starts_at = utils.parse_iso(reminder.get("starts_at"))
        if starts_at is None:
            return db.SKIP_BAD_DATA, "Randevu saati okunamadı."
        if appointment_status in ("confirmed", "completed"):
            return db.SKIP_ALREADY_CONFIRMED, "Randevu zaten onaylanmış/tamamlanmış, onay mesajı gerekmiyor."
        if now > starts_at and not force:
            return db.SKIP_TOO_LATE, "Randevu saati geçtiği için onay mesajı gönderilmedi."
        return None

    # review
    link = (settings.get("google_review_url") or "").strip()
    if not link and (settings.get("whatsapp_provider") or "simulation") != "simulation":
        return db.SKIP_NO_REVIEW_LINK, "Google Yorum Linki ayarlanmamış. Ayarlar sayfasından ekleyin."
    due_at = utils.parse_iso(reminder.get("due_at"))
    if due_at is not None and now > due_at + timedelta(days=7) and not force:
        return db.SKIP_TOO_LATE, "Yorum mesajı için çok geç (7 günden fazla)."
    return None


def _log(item: dict[str, Any], kind: str, body: str, settings: dict[str, str], result: dict[str, Any]) -> int:
    simulated = bool(result.get("simulated")) and bool(result.get("ok"))
    status = ("simulated" if simulated else "sent") if result.get("ok") else "failed"
    return db.log_message(
        appointment_id=int(item["appointment_id"]) if item.get("appointment_id") else None,
        customer_id=int(item["customer_id"]) if item.get("customer_id") else None,
        kind=kind,
        phone=str(item.get("customer_phone") or ""),
        body=body,
        provider=(settings.get("whatsapp_provider") or "simulation").strip(),
        status=status,
        simulated=simulated,
        error="" if result.get("ok") else str(result.get("detail") or ""),
        created_at=utils.now_utc(),
    )


def _deliver(
    reminder: dict[str, Any],
    settings: dict[str, str],
    now: datetime,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Sahiplenilmiş (claim) tek bir hatırlatmayı göndermeyi dener."""
    reminder_id = int(reminder["id"])
    kind = reminder.get("kind") or "confirmation"

    # Gönderimden hemen önce durumu tazele: bu arada randevu iptal edilmiş olabilir.
    reminder = db.get_reminder(reminder_id) or reminder
    if reminder.get("status") == "sent":
        return {"outcome": "skipped", "detail": "Bu mesaj zaten gönderilmiş."}
    skip = _skip_reason(reminder, settings, now, force)
    if skip:
        code, reason = skip
        db.skip_reminder(reminder_id, reason, code)
        logger.info("Hatırlatma atlandı (#%s, %s): %s", reminder_id, kind, reason)
        return {"outcome": "skipped", "detail": reason}

    body = whatsapp.render_message(_template_for(kind, settings), _build_context(reminder, settings))
    result = _send(str(reminder.get("customer_phone") or ""), body, settings)

    if result.get("busy"):
        # Hiç gönderilmedi: kirayı bırak, deneme hakkı yanmasın.
        db.release_claim(reminder_id)
        return {"outcome": "busy", "detail": str(result.get("detail") or "")}

    message_id = _log(reminder, kind, body, settings, result)
    finished_at = utils.now_utc()
    if result.get("ok"):
        db.mark_reminder_sent(reminder_id, message_id, finished_at)
        logger.info("Hatırlatma gönderildi (#%s, %s, %s)", reminder_id, kind, result.get("provider"))
        return {
            "outcome": "sent",
            "detail": str(result.get("detail") or ""),
            "simulated": bool(result.get("simulated")),
            "body": body,
        }

    # --- başarısız: yeniden deneme planla ---
    attempts = int(reminder.get("attempts") or 1)  # claim sırasında artırıldı
    max_attempts = max(1, db.to_int(settings.get("max_send_attempts"), 3))
    retry_minutes = db.to_float(settings.get("retry_delay_minutes"), 5.0)
    final = attempts >= max_attempts
    next_attempt_at = None if final else finished_at + timedelta(minutes=retry_minutes * attempts)
    error = str(result.get("detail") or "Bilinmeyen gönderim hatası")
    db.mark_reminder_failed(
        reminder_id,
        error=error,
        attempts=attempts,
        now=finished_at,
        next_attempt_at=next_attempt_at,
        final=final,
    )
    logger.warning(
        "Hatırlatma gönderilemedi (#%s, %s, deneme %s/%s): %s",
        reminder_id, kind, attempts, max_attempts, error,
    )
    return {"outcome": "failed", "detail": error, "final": final, "attempts": attempts}


# ---------------------------------------------------------------------
# Dışa açık işlemler
# ---------------------------------------------------------------------
def process_due_reminders(
    now: datetime | None = None,
    *,
    limit: int = 20,
    respect_enabled: bool = True,
) -> dict[str, Any]:
    """Zamanı gelen tüm hatırlatmaları işler (motorun tek turu)."""
    now = now or utils.now_utc()
    settings = db.get_settings()
    summary: dict[str, Any] = {
        "checked": 0,
        "sent": 0,
        "simulated": 0,
        "failed": 0,
        "skipped": 0,
        "busy": False,
        "disabled": False,
        "errors": [],
    }

    if respect_enabled and str(settings.get("worker_enabled", "1")) != "1":
        summary["disabled"] = True
        return summary

    reminders = db.due_reminders(now, limit=limit)
    summary["checked"] = len(reminders)
    for reminder in reminders:
        reminder_id = int(reminder["id"])
        try:
            # Atomik sahiplenme: başka bir süreç aldıysa bu turda dokunma.
            if not db.claim_reminder(reminder_id, now=now):
                summary["skipped"] += 1
                continue
            outcome = _deliver(reminder, settings, now, force=False)
        except Exception as exc:  # tek kayıt tüm turu bozmasın
            logger.exception("Hatırlatma işlenirken hata (#%s)", reminder_id)
            summary["failed"] += 1
            summary["errors"].append(str(exc))
            continue

        result = outcome["outcome"]
        if result == "sent":
            summary["sent"] += 1
            if outcome.get("simulated"):
                summary["simulated"] += 1
        elif result == "failed":
            summary["failed"] += 1
            summary["errors"].append(str(outcome.get("detail") or ""))
        elif result == "busy":
            summary["busy"] = True
            break  # sıra boşalınca sonraki turda devam edilir
        else:
            summary["skipped"] += 1

    summary["errors"] = summary["errors"][:5]
    if summary["sent"] or summary["failed"]:
        logger.info(
            "Motor turu: %s kontrol, %s gönderim, %s hata, %s atlandı",
            summary["checked"], summary["sent"], summary["failed"], summary["skipped"],
        )
    return summary


def send_reminder_now(reminder_id: int) -> dict[str, Any]:
    """Paneldeki "Şimdi gönder" düğmesi: zamanı beklemeden gönderir."""
    reminder = db.get_reminder(int(reminder_id))
    if reminder is None:
        return {"ok": False, "message": "Hatırlatma kaydı bulunamadı."}
    if reminder.get("status") == "sent":
        return {"ok": False, "message": "Bu mesaj zaten gönderilmiş."}
    if reminder.get("appointment_status") in db.INACTIVE_STATUSES:
        return {"ok": False, "message": "Randevu iptal edilmiş veya müşteri gelmemiş; mesaj gönderilemez."}
    if int(reminder.get("customer_opted_out") or 0):
        return {"ok": False, "message": "Bu müşteri mesaj almak istemiyor; mesaj gönderilmez."}

    busy_message = (
        "Bu mesaj şu anda başka bir gönderim tarafından işleniyor. Birkaç saniye sonra tekrar deneyin."
    )
    # Manuel gönderimde bekleme süresi/atlanmış durum engel olmasın; ama şu an
    # gönderilmekte olan bir işe dokunulmaz (reset_reminder kirayı kontrol eder).
    if reminder.get("status") in ("skipped", "failed") or reminder.get("next_attempt_at"):
        if not db.reset_reminder(int(reminder_id)):
            return {"ok": False, "message": busy_message}

    now = utils.now_utc()
    if not db.claim_reminder(int(reminder_id), now=now):
        return {"ok": False, "message": busy_message}

    outcome = _deliver(reminder, db.get_settings(), now, force=True)
    kind_label = KIND_LABELS.get(str(reminder.get("kind")), "Mesaj")
    if outcome["outcome"] == "sent":
        if outcome.get("simulated"):
            return {"ok": True, "message": f"{kind_label} simüle edildi (gerçek WhatsApp mesajı gönderilmedi)."}
        return {"ok": True, "message": f"{kind_label} gönderildi."}
    if outcome["outcome"] in ("skipped", "busy"):
        return {"ok": False, "message": str(outcome.get("detail") or "Mesaj atlandı.")}
    return {"ok": False, "message": f"Gönderim başarısız: {outcome.get('detail')}"}


def send_manual_reminder(appointment_id: int, *, allow_repeat: bool = False) -> dict[str, Any]:
    """Esnafın tek tıkla gönderdiği "Randevunuzu unutmayın" mesajı.

    Otomatik onay/yorum işlerinden bağımsızdır; onların durumunu değiştirmez.
    Telefonda yanlışlıkla iki kez dokunmaya karşı, son 2 dakikada aynı randevuya
    gönderildiyse (ya da gönderim sürüyorsa) `allow_repeat=True` verilmedikçe
    tekrar göndermez.
    """
    appointment_id = int(appointment_id)
    appointment = db.get_appointment(appointment_id)
    if appointment is None:
        return {"ok": False, "message": "Randevu bulunamadı."}
    if appointment.get("status") not in ("scheduled", "confirmed"):
        return {"ok": False, "message": "Bu randevu aktif değil; hatırlatma gönderilmez."}
    if int(appointment.get("customer_opted_out") or 0):
        return {"ok": False, "message": "Bu müşteri mesaj almak istemiyor; hatırlatma gönderilmez."}

    duplicate = {
        "ok": False,
        "duplicate": True,
        "message": "Bu müşteriye az önce hatırlatma gönderildi. Tekrar göndermek için onaylayın.",
    }
    with _manual_inflight_lock:
        if appointment_id in _manual_inflight:
            return {**duplicate, "message": "Bu müşteriye şu anda hatırlatma gönderiliyor."}
        last = db.last_message_at(appointment_id, "manual")
        if (
            last is not None
            and not allow_repeat
            and (utils.now_utc() - last).total_seconds() < MANUAL_RESEND_GUARD_SECONDS
        ):
            return duplicate
        _manual_inflight.add(appointment_id)
    try:
        settings = db.get_settings()
        body = whatsapp.render_message(_template_for("manual", settings), _build_context(appointment, settings))
        result = _send(str(appointment.get("customer_phone") or ""), body, settings)
        if result.get("busy"):
            return {"ok": False, "message": str(result.get("detail"))}
        _log({**appointment, "appointment_id": appointment_id}, "manual", body, settings, result)
    finally:
        with _manual_inflight_lock:
            _manual_inflight.discard(appointment_id)

    if not result.get("ok"):
        return {"ok": False, "message": f"Gönderilemedi: {result.get('detail')}"}
    name = appointment.get("customer_name") or "Müşteri"
    if result.get("simulated"):
        return {"ok": True, "message": f"{name} için hatırlatma simüle edildi (gerçek mesaj gitmedi).", "body": body}
    return {"ok": True, "message": f"{name} için hatırlatma gönderildi.", "body": body}


def send_test_message(phone: str, message: str) -> dict[str, Any]:
    """Ayarlar sayfasındaki test mesajı (randevuya bağlı değildir)."""
    settings = db.get_settings()
    body = whatsapp.clean_body(message)
    result = _send(phone, body, settings)
    _log({"customer_phone": phone}, "test", body, settings, result)
    return result
