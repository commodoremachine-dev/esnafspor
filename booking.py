"""Randevu oluşturma kuralları (Flask ve Streamlit panelleri ortak kullanır).

Doğrulama, çakışma kontrolü ve kayıt tek yerde tutulur; böylece iki arayüz
aynı kurallarla çalışır.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import config
import db
import utils
import whatsapp

NAME_MAX = 80
SERVICE_MAX = 80
NOTES_MAX = 300
DURATION_MIN = 15
DURATION_MAX = 480


@dataclass
class BookingResult:
    ok: bool
    message: str = ""
    errors: list[str] = field(default_factory=list)
    appointment_id: int | None = None


def default_duration(settings: dict[str, str] | None = None) -> int:
    settings = settings if settings is not None else db.get_settings()
    return db.to_int(settings.get("default_duration_minutes") or 60, 60)


def create_booking(
    name: str,
    phone_raw: str,
    when_raw: str,
    *,
    duration: int | str | None = None,
    service: str = "",
    notes: str = "",
) -> BookingResult:
    """Formdan gelen ham değerlerle randevu ekler.

    `when_raw` işletmenin yerel saatidir (ör. ``2026-11-10T14:30``).
    """
    settings = db.get_settings()
    tz_name = settings.get("timezone") or config.DEFAULT_TIMEZONE

    name = (name or "").strip()
    service = (service or "").strip()
    notes = (notes or "").strip()

    errors: list[str] = []
    if not name:
        errors.append("Müşteri adı boş olamaz.")
    elif len(name) > NAME_MAX:
        errors.append(f"Müşteri adı {NAME_MAX} karakterden uzun olamaz.")
    if len(service) > SERVICE_MAX:
        errors.append(f"Hizmet adı {SERVICE_MAX} karakterden uzun olamaz.")
    if len(notes) > NOTES_MAX:
        errors.append(f"Not {NOTES_MAX} karakterden uzun olamaz.")

    phone = ""
    try:
        phone = whatsapp.normalize_phone(phone_raw)
    except ValueError as exc:
        errors.append(str(exc))

    starts_at = utils.parse_local_input((when_raw or "").strip(), tz_name)
    if starts_at is None:
        errors.append("Randevu tarihi ve saati geçersiz.")
    elif starts_at < utils.now_utc() - timedelta(minutes=5):
        errors.append("Geçmiş bir tarihe randevu eklenemez.")

    fallback = default_duration(settings)
    try:
        duration_value = int(duration) if duration not in (None, "") else fallback
    except (TypeError, ValueError):
        duration_value = fallback
    if not DURATION_MIN <= duration_value <= DURATION_MAX:
        errors.append(
            f"Randevu süresi {DURATION_MIN} ile {DURATION_MAX} dakika arasında olmalı."
        )

    if errors:
        return BookingResult(ok=False, message=" ".join(errors), errors=errors)

    # Çakışma kontrolü, müşteri ve randevu kaydı tek işlemde: reddedilen bir
    # istek hiçbir şey yazmaz, iki eşzamanlı istek çakışan randevu oluşturamaz.
    # Not yalnızca randevuya yazılır; müşterinin kalıcı notu değişmez.
    appointment_id, conflict, existing = db.book_appointment(
        name=name,
        phone=phone,
        starts_at=starts_at,
        duration_minutes=duration_value,
        service=service,
        notes=notes,
    )
    if conflict is not None:
        conflict_start = utils.parse_iso(conflict["starts_at"])
        when_text = (
            utils.fmt_datetime(utils.to_tz(conflict_start, tz_name))
            if conflict_start
            else conflict["starts_at"]
        )
        message = f"Bu müşterinin aynı saatte başka bir randevusu var: {when_text}."
        return BookingResult(ok=False, message=message, errors=[message])

    reminders = db.get_reminders_for([appointment_id]).get(appointment_id, {})
    due = utils.parse_iso(reminders.get("confirmation", {}).get("due_at"))
    if due is not None and due > utils.now_utc():
        message = (
            "Randevu eklendi. Onay mesajı "
            f"{utils.fmt_datetime(utils.to_tz(due, tz_name))} için planlandı."
        )
    else:
        message = (
            "Randevu eklendi. Onay mesajı zamanı geçtiği için motor bir sonraki "
            "kontrolünde göndermeyi deneyecek."
        )
    if existing is not None and existing["name"].strip().casefold() != name.casefold():
        message += f" Not: Bu numara “{existing['name']}” adıyla kayıtlı; kayıtlı isim korundu."
    if existing is not None and int(existing.get("opted_out") or 0):
        message += " Uyarı: Bu müşteri mesaj almak istemiyor; hatırlatma gönderilmeyecek."
    return BookingResult(ok=True, message=message, appointment_id=appointment_id)
