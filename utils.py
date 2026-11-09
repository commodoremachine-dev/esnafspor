"""Tarih/saat ve Türkçe biçimlendirme yardımcıları.

Veritabanında tüm zamanlar **UTC** olarak ISO-8601 biçiminde saklanır
(ör. ``2026-11-09T13:39:00+00:00``). Kullanıcı arayüzünde ve mesajlarda
ise işletmenin saat dilimine (varsayılan ``Europe/Istanbul``) çevrilir.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

UTC = timezone.utc

TR_MONTHS = (
    "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
)

TR_WEEKDAYS = (
    "Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar",
)


# ---------------------------------------------------------------------
# Saat dilimi
# ---------------------------------------------------------------------
@lru_cache(maxsize=16)
def get_tz(name: str) -> ZoneInfo:
    """Saat dilimi nesnesini döndürür; geçersiz adda hataya düşmez."""
    try:
        return ZoneInfo(name or "Europe/Istanbul")
    except Exception:
        return ZoneInfo("Europe/Istanbul")


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_utc(dt: datetime) -> datetime:
    """Naif datetime'ı UTC kabul ederek, farkındalı olanı UTC'ye çevirir."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_tz(dt: datetime, tz_name: str) -> datetime:
    return to_utc(dt).astimezone(get_tz(tz_name))


def local_now(tz_name: str) -> datetime:
    return now_utc().astimezone(get_tz(tz_name))


# ---------------------------------------------------------------------
# ISO dönüşümleri (veritabanı biçimi)
# ---------------------------------------------------------------------
def to_iso(dt: datetime) -> str:
    """UTC'ye normalize edilmiş ISO-8601 metni (saniye hassasiyetinde)."""
    return to_utc(dt).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    """ISO-8601 metnini farkındalı UTC datetime'a çevirir; hatalıysa None."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_local_input(text: str, tz_name: str) -> datetime | None:
    """HTML form girdisini (yerel saat) UTC datetime'a çevirir.

    Kabul edilen biçimler: ``2026-11-10T14:30``, ``2026-11-10T14:30:00``,
    ``10.11.2026 14:30`` ve ``10.11.2026`` (gün başı).
    """
    if not text:
        return None
    raw = str(text).strip()
    formats = (
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
    )
    parsed: datetime | None = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    return to_utc(parsed.replace(tzinfo=get_tz(tz_name)))


# ---------------------------------------------------------------------
# Türkçe biçimlendirme
# ---------------------------------------------------------------------
def fmt_time(dt: datetime) -> str:
    """16:05"""
    return dt.strftime("%H:%M")


def fmt_date(dt: datetime) -> str:
    """09.11.2026"""
    return dt.strftime("%d.%m.%Y")


def fmt_datetime(dt: datetime) -> str:
    """09.11.2026 16:05"""
    return f"{fmt_date(dt)} {fmt_time(dt)}"


def weekday_tr(dt: datetime) -> str:
    return TR_WEEKDAYS[dt.weekday()]


def day_bounds_utc(day: datetime, tz_name: str) -> tuple[datetime, datetime]:
    """Verilen yerel günün başlangıcını ve bitişini UTC olarak döndürür."""
    local = to_tz(day, tz_name)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def next_quarter_hour(dt: datetime) -> datetime:
    """Bir sonraki 15 dakikalık dilime yuvarlar (form varsayılanı için)."""
    dt = dt.replace(second=0, microsecond=0)
    remainder = dt.minute % 15
    if remainder:
        dt = dt.replace(minute=dt.minute - remainder) + timedelta(minutes=15)
    return dt
