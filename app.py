"""Flask arayüzü — iki sayfa: Panel (/) ve Ayarlar (/ayarlar).

Tek kullanıcılı, yerel çalışan bir admin panelidir; üyelik/JWT yoktur.
Sunucu varsayılan olarak yalnızca 127.0.0.1'e bağlanır. `ESNAF_PASSWORD`
tanımlıysa tüm sayfalar tek bir parola ile korunur (yerel ağda zorunlu).
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.exceptions import SecurityError

import auth
import booking
import config
import db
import scheduler
import utils
import whatsapp
import worker

logger = logging.getLogger(__name__)

panel = Blueprint("panel", __name__)

STATUS_META: dict[str, tuple[str, str]] = {
    "scheduled": ("Bekliyor", "bg-amber-50 text-amber-700 ring-amber-200"),
    "confirmed": ("Onaylandı", "bg-emerald-50 text-emerald-700 ring-emerald-200"),
    "completed": ("Tamamlandı", "bg-sky-50 text-sky-700 ring-sky-200"),
    "cancelled": ("İptal edildi", "bg-slate-100 text-slate-500 ring-slate-200"),
    "no_show": ("Gelmedi", "bg-rose-50 text-rose-700 ring-rose-200"),
}

DURATION_OPTIONS = (15, 30, 45, 60, 90, 120, 180)
LIST_LIMIT = 120

REMINDER_KIND_LABELS = {
    "confirmation": "Onay mesajı",
    "review": "Yorum mesajı",
}


# ---------------------------------------------------------------------
# Küçük yardımcılar
# ---------------------------------------------------------------------
def _settings() -> dict[str, str]:
    """Ayarlar istek başına bir kez okunur (her şablon/filtre için ayrı sorgu yok)."""
    cached = g.get("settings")
    if cached is None:
        cached = db.get_settings()
        g.settings = cached
    return cached


def _tz_name() -> str:
    value = _settings().get("timezone") or config.DEFAULT_TIMEZONE
    try:
        ZoneInfo(value)
        return value
    except Exception:
        return config.DEFAULT_TIMEZONE


def _safe_next(default_endpoint: str = "panel.index") -> str:
    """Formdan gelen `next` adresini yalnızca site içi ise kabul eder."""
    target = request.form.get("next") or request.args.get("next")
    if not target:
        return url_for(default_endpoint)
    # Kontrol karakterleri (CRLF, sekme vb.) ve şema/alan adı içeren hedefleri reddet.
    if any(ord(char) < 32 or ord(char) == 127 for char in target):
        return url_for(default_endpoint)
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return url_for(default_endpoint)
    if not target.startswith("/") or target.startswith("//") or target.startswith("/\\"):
        return url_for(default_endpoint)
    return target


def _local(iso_value: str | None) -> datetime | None:
    parsed = utils.parse_iso(iso_value)
    if parsed is None:
        return None
    return utils.to_tz(parsed, getattr(g, "tz_name", None) or _tz_name())


def _reminder_view(reminder: dict | None, appointment: dict) -> dict | None:
    """Hatırlatmayı şablonda göstermek için zenginleştirir."""
    if not reminder:
        return None
    status = reminder.get("status") or "pending"
    kind = reminder.get("kind") or "confirmation"
    due_local = _local(reminder.get("due_at"))
    sent_local = _local(reminder.get("sent_at"))
    status_text = {
        "pending": "bekliyor",
        "sent": "gönderildi",
        "failed": "başarısız",
        "skipped": "atlandı",
    }.get(status, status)
    classes = {
        "pending": "bg-slate-50 text-slate-600 ring-slate-200",
        "sent": "bg-emerald-50 text-emerald-700 ring-emerald-200",
        "failed": "bg-rose-50 text-rose-700 ring-rose-200",
        "skipped": "bg-slate-100 text-slate-500 ring-slate-200",
    }.get(status, "bg-slate-50 text-slate-600 ring-slate-200")

    when = ""
    if status == "sent" and sent_local:
        when = f" · {utils.fmt_time(sent_local)}"
    elif due_local:
        when = f" · {utils.fmt_time(due_local)}"

    active_appointment = appointment.get("status") not in ("cancelled", "no_show")
    can_send = active_appointment and status in ("pending", "failed", "skipped")
    return {
        "id": reminder.get("id"),
        "kind": kind,
        "label": REMINDER_KIND_LABELS.get(kind, kind),
        "status": status,
        "status_text": status_text,
        "classes": classes,
        "short": f"{status_text}{when}",
        "due_local": due_local,
        "can_send": can_send,
        "tooltip": reminder.get("error") or (
            f"Planlanan gönderim: {utils.fmt_datetime(due_local)}" if due_local else ""
        ),
    }


def _appointment_row(appointment: dict, reminders: dict) -> dict:
    starts_local = _local(appointment["starts_at"])
    status_label, status_classes = STATUS_META.get(
        str(appointment.get("status")), ("Bilinmiyor", "bg-slate-100 text-slate-600 ring-slate-200")
    )
    confirmation = _reminder_view(reminders.get("confirmation"), appointment)
    review = _reminder_view(reminders.get("review"), appointment)
    return {
        "a": appointment,
        "starts_local": starts_local,
        "status_label": status_label,
        "status_classes": status_classes,
        "confirmation": confirmation,
        "review": review,
        "can_act": appointment.get("status") not in ("cancelled", "no_show"),
    }


def _suggested_datetime(tz_name: str) -> tuple[str, str]:
    now_local = utils.local_now(tz_name)
    suggested = utils.next_quarter_hour(now_local + timedelta(hours=1))
    return (
        suggested.strftime("%Y-%m-%dT%H:%M"),
        now_local.strftime("%Y-%m-%dT%H:%M"),
    )


# ---------------------------------------------------------------------
# Parola ile giriş (ESNAF_PASSWORD tanımlıysa)
# ---------------------------------------------------------------------
_login_throttle = auth.LoginThrottle()


def _auth_required() -> bool:
    return bool(current_app.config.get("ADMIN_PASSWORD"))


def _is_authenticated() -> bool:
    return auth.session_is_valid(session.get("auth"), current_app.config.get("ADMIN_PASSWORD") or "")


def _plain_error(code: int, title: str, message: str):
    """Şablona/veritabanına dokunmayan sade hata sayfası."""
    return (
        "<!doctype html><html lang='tr'><meta charset='utf-8'>"
        f"<title>{code} · {title}</title>"
        "<body style='font-family:system-ui;padding:3rem;background:#f8fafc;color:#0f172a'>"
        f"<h1>{code}</h1><p>{message}</p>"
        "</body></html>",
        code,
    )


def _database_display() -> str:
    """Veritabanı yolunu proje köküne göre kısa biçimde gösterir."""
    path = db.get_db_path()
    try:
        return str(path.relative_to(config.BASE_DIR))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------
# Uygulama fabrikası
# ---------------------------------------------------------------------
def create_app() -> Flask:
    config.load_dotenv()
    config.ensure_dirs()
    app = Flask(__name__)
    trusted_hosts = [
        host.strip()
        for host in os.environ.get("ESNAF_TRUSTED_HOSTS", "127.0.0.1,localhost").split(",")
        if host.strip()
    ]
    app.config.update(
        SECRET_KEY=config.secret_key(),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # DNS rebinding / yabancı Host başlıklarına karşı: yalnızca yerel adlar.
        TRUSTED_HOSTS=trusted_hosts,
        ADMIN_PASSWORD=config.admin_password(),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    db.init_db()
    app.register_blueprint(panel)

    @app.before_request
    def _load_timezone() -> None:
        g.tz_name = _tz_name()

    @app.before_request
    def _csrf_protect() -> None:
        """Form tabanlı POST isteklerinde oturuma bağlı CSRF anahtarı arar."""
        if request.method != "POST":
            return
        expected = session.get("csrf_token")
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not expected or not supplied or not hmac.compare_digest(str(supplied), str(expected)):
            abort(
                400,
                description="Form anahtarı geçersiz veya süresi dolmuş. Sayfayı yenileyip tekrar deneyin.",
            )

    @app.before_request
    def _require_login():
        """Parola tanımlıysa giriş yapılmamış istekleri giriş sayfasına yönlendirir."""
        if request.endpoint in ("panel.login", "static") or _is_authenticated():
            return None
        if request.path.startswith("/api/") and request.method == "GET":
            return jsonify({"status": "unauthorized"}), 401
        target = request.full_path if request.query_string else request.path
        if request.method != "GET":
            target = "/"
        return redirect(url_for("panel.login", next=target))

    @app.after_request
    def _security_headers(response):
        # Panel başka sitelerin içine (iframe) gömülemesin; yalnızca kendi
        # dosyalarımızdan script/stil yüklensin.
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; object-src 'none'",
        )
        return response

    # --- Jinja filtreleri -------------------------------------------------
    @app.template_filter("local_time")
    def _filter_local_time(value: str | None) -> str:
        parsed = _local(value)
        return utils.fmt_time(parsed) if parsed else "—"

    @app.template_filter("local_date")
    def _filter_local_date(value: str | None) -> str:
        parsed = _local(value)
        return utils.fmt_date(parsed) if parsed else "—"

    @app.template_filter("local_datetime")
    def _filter_local_datetime(value: str | None) -> str:
        parsed = _local(value)
        return utils.fmt_datetime(parsed) if parsed else "—"

    @app.template_filter("local_day_short")
    def _filter_local_day_short(value: str | None) -> str:
        parsed = _local(value)
        if not parsed:
            return ""
        if parsed.date() == utils.local_now(getattr(g, "tz_name", config.DEFAULT_TIMEZONE)).date():
            return "Bugün"
        return utils.TR_WEEKDAYS[parsed.weekday()][:3]

    @app.template_filter("phone")
    def _filter_phone(value: str | None) -> str:
        return whatsapp.format_phone_display(str(value or ""))

    # --- Şablon değişkenleri ---------------------------------------------
    @app.context_processor
    def _inject_globals() -> dict:
        csrf_token = session.get("csrf_token")
        if not csrf_token:
            csrf_token = secrets.token_urlsafe(32)
            session["csrf_token"] = csrf_token
        if not _is_authenticated():
            # Giriş sayfası: işletme verisi veya ayar sızdırma.
            return {"csrf_token": csrf_token, "app_name": config.APP_NAME}
        settings = dict(_settings())
        # API anahtarı hiçbir şablona gitmez; yalnızca kayıtlı olup olmadığı bilinir.
        http_token_set = bool(settings.pop("http_api_token", ""))
        tz_name = getattr(g, "tz_name", None) or _tz_name()
        provider = settings.get("whatsapp_provider") or "simulation"
        return {
            "settings": settings,
            "http_token_set": http_token_set,
            "auth_enabled": _auth_required(),
            "worker_state": scheduler.status(),
            "tz_name": tz_name,
            "now_local": utils.local_now(tz_name),
            "provider_label": whatsapp.provider_label(provider),
            "csrf_token": csrf_token,
            "app_name": config.APP_NAME,
            "database_display": _database_display(),
            "current_url": request.full_path if request.query_string else request.path,
        }

    # --- Hata sayfaları ---------------------------------------------------
    @app.errorhandler(400)
    def _bad_request(error):  # pragma: no cover - basit sayfa
        if isinstance(error, SecurityError):
            # Güvenilmeyen Host başlığında şablon/url_for çalışmaz: statik yanıt ver.
            return _plain_error(
                400,
                "Geçersiz adres",
                "Bu adres bu panel tarafından kabul edilmiyor. "
                "Panele <code>http://127.0.0.1:5000</code> adresinden erişin.",
            )
        message = getattr(error, "description", None) or "Geçersiz istek."
        if not _is_authenticated():
            flash(message, "error")
            return redirect(url_for("panel.login"))
        return render_template("error.html", code=400, message=message), 400

    @app.errorhandler(404)
    def _not_found(_error):  # pragma: no cover - basit sayfa
        return render_template("error.html", code=404, message="Sayfa bulunamadı."), 404

    @app.errorhandler(500)
    def _server_error(_error):  # pragma: no cover - basit sayfa
        # Flask istisnayı zaten günlüğe yazar. Hata veritabanından kaynaklanıyor
        # olabileceği için şablon (ve dolayısıyla veritabanı) kullanılmaz.
        return _plain_error(500, "Hata", "Beklenmeyen bir hata oluştu. Sayfayı yenileyip tekrar deneyin.")

    return app


# ---------------------------------------------------------------------
# Sayfa: Panel
# ---------------------------------------------------------------------
@panel.get("/")
def index():
    settings = _settings()
    tz_name = settings.get("timezone") or config.DEFAULT_TIMEZONE
    now = utils.now_utc()
    today_start, today_end = utils.day_bounds_utc(now, tz_name)

    active_filter = request.args.get("filter", "upcoming")
    if active_filter not in ("upcoming", "today", "past", "all"):
        active_filter = "upcoming"

    start_utc: datetime | None
    end_utc: datetime | None
    include_cancelled = False
    order = "asc"
    if active_filter == "today":
        start_utc, end_utc = today_start, today_end
    elif active_filter == "past":
        start_utc, end_utc = None, now - timedelta(minutes=30)
        include_cancelled, order = True, "desc"
    elif active_filter == "all":
        start_utc = end_utc = None
        include_cancelled, order = True, "desc"
    else:
        start_utc, end_utc = now - timedelta(minutes=30), None

    appointments = db.list_appointments(
        start_utc=start_utc,
        end_utc=end_utc,
        include_cancelled=include_cancelled,
        limit=LIST_LIMIT + 1,
        order=order,
    )
    truncated = len(appointments) > LIST_LIMIT
    appointments = appointments[:LIST_LIMIT]
    reminders_map = db.get_reminders_for([item["id"] for item in appointments])
    rows = [
        _appointment_row(item, reminders_map.get(item["id"], {}))
        for item in appointments
    ]

    stats = db.dashboard_stats(today_start, today_end)
    messages = db.recent_messages(12)
    default_datetime, min_datetime = _suggested_datetime(tz_name)
    default_duration = booking.default_duration(settings)
    # Ayar 75 dk gibi listede olmayan bir değerse formda görünmesini sağla.
    duration_options = tuple(sorted({*DURATION_OPTIONS, default_duration}))

    return render_template(
        "index.html",
        rows=rows,
        stats=stats,
        messages=messages,
        active_filter=active_filter,
        default_datetime=default_datetime,
        min_datetime=min_datetime,
        duration_options=duration_options,
        default_duration=default_duration,
        active_filter_label={
            "upcoming": "Yaklaşan randevular",
            "today": "Bugünkü randevular",
            "past": "Geçmiş randevular",
            "all": "Tüm randevular",
        }[active_filter],
        confirmed_count=sum(1 for row in rows if row["a"]["status"] == "confirmed"),
        truncated=truncated,
        list_limit=LIST_LIMIT,
    )


# ---------------------------------------------------------------------
# Sayfa: Ayarlar
# ---------------------------------------------------------------------
@panel.get("/ayarlar")
def settings_page():
    settings = _settings()
    provider_ready, provider_message = whatsapp.provider_status(settings)
    return render_template(
        "settings.html",
        provider_ready=provider_ready,
        provider_message=provider_message,
        providers=whatsapp.PROVIDERS,
    )


# ---------------------------------------------------------------------
# Randevu işlemleri
# ---------------------------------------------------------------------
@panel.post("/api/appointments")
def create_appointment():
    result = booking.create_booking(
        request.form.get("name") or "",
        request.form.get("phone") or "",
        request.form.get("starts_at") or "",
        duration=request.form.get("duration_minutes"),
        service=request.form.get("service") or "",
        notes=request.form.get("notes") or "",
    )
    flash(result.message, "success" if result.ok else "error")
    return redirect(url_for("panel.index"))


@panel.post("/api/appointments/<int:appointment_id>/status")
def update_appointment_status(appointment_id: int):
    status = (request.form.get("status") or "").strip()
    labels = {
        "confirmed": "Randevu onaylandı olarak işaretlendi.",
        "completed": "Randevu tamamlandı olarak işaretlendi.",
        "cancelled": "Randevu iptal edildi.",
        "no_show": "Randevu 'müşteri gelmedi' olarak işaretlendi.",
        "scheduled": "Randevu yeniden planlandı olarak işaretlendi.",
    }
    if status not in db.APPOINTMENT_STATUSES:
        abort(400)
    if not db.set_appointment_status(appointment_id, status):
        flash("Randevu bulunamadı.", "error")
        return redirect(_safe_next())
    flash(labels.get(status, "Randevu durumu güncellendi."), "success")
    return redirect(_safe_next())


@panel.post("/api/customers/<int:customer_id>/opt-out")
def set_customer_opt_out(customer_id: int):
    opted_out = request.form.get("opted_out") == "1"
    if not db.set_customer_opt_out(customer_id, opted_out):
        flash("Müşteri bulunamadı.", "error")
    elif opted_out:
        flash("Müşteri mesaj istemiyor olarak işaretlendi; bekleyen mesajları iptal edildi.", "success")
    else:
        flash("Müşteriye yeniden mesaj gönderilebilir; gelecekteki hatırlatmalar açıldı.", "success")
    return redirect(_safe_next())


@panel.post("/api/appointments/<int:appointment_id>/delete")
def delete_appointment(appointment_id: int):
    if db.delete_appointment(appointment_id):
        flash("Randevu silindi.", "success")
    else:
        flash("Randevu bulunamadı.", "error")
    return redirect(_safe_next())


# ---------------------------------------------------------------------
# Hatırlatma işlemleri
# ---------------------------------------------------------------------
@panel.post("/api/reminders/<int:reminder_id>/send")
def send_reminder(reminder_id: int):
    result = worker.send_reminder_now(reminder_id)
    flash(str(result.get("message") or "İşlem tamamlandı."), "success" if result.get("ok") else "error")
    return redirect(_safe_next())


@panel.post("/api/worker/tick")
def run_worker_tick():
    summary = scheduler.run_once(respect_enabled=False)
    if summary.get("busy"):
        flash("Motor şu anda meşgul, birkaç saniye sonra tekrar deneyin.", "info")
    elif summary.get("disabled"):
        flash("Otomatik gönderim ayarlardan kapatılmış.", "info")
    else:
        flash(
            f"Kontrol tamamlandı: {summary.get('checked', 0)} iş incelendi, "
            f"{summary.get('sent', 0)} mesaj gönderildi, "
            f"{summary.get('failed', 0)} hata, {summary.get('skipped', 0)} atlandı.",
            "success" if not summary.get("failed") else "info",
        )
    return redirect(_safe_next())


@panel.post("/api/test-message")
def send_test_message():
    phone_raw = (request.form.get("phone") or "").strip()
    message = (request.form.get("message") or "").strip() or "Merhaba, bu bir test mesajıdır."
    try:
        phone = whatsapp.normalize_phone(phone_raw)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("panel.settings_page"))
    result = worker.send_test_message(phone, message)
    if result.get("ok"):
        flash(str(result.get("detail") or "Test mesajı gönderildi."), "success")
    else:
        flash(f"Test mesajı gönderilemedi: {result.get('detail')}", "error")
    return redirect(url_for("panel.settings_page"))


# ---------------------------------------------------------------------
# Ayarları kaydet
# ---------------------------------------------------------------------
@panel.post("/api/settings")
def save_settings():
    current = db.get_settings()
    errors: list[str] = []
    updates: dict[str, str] = {}

    def text_field(key: str, label: str, *, required: bool = False, max_len: int = 500) -> str:
        value = (request.form.get(key) or "").strip()
        if required and not value:
            errors.append(f"{label} boş olamaz.")
        if len(value) > max_len:
            errors.append(f"{label} en fazla {max_len} karakter olabilir.")
        return value

    def float_field(key: str, label: str, low: float, high: float) -> float:
        raw = (request.form.get(key) or "").strip().replace(",", ".")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            errors.append(f"{label} sayısal bir değer olmalı.")
            return float(current.get(key) or low)
        if not low <= value <= high:
            errors.append(f"{label} {low:g} ile {high:g} arasında olmalı.")
        return value

    def int_field(key: str, label: str, low: int, high: int) -> int:
        raw = (request.form.get(key) or "").strip()
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            errors.append(f"{label} sayısal bir değer olmalı.")
            return int(float(current.get(key) or low))
        if not low <= value <= high:
            errors.append(f"{label} {low} ile {high} arasında olmalı.")
        return value

    updates["business_name"] = text_field("business_name", "İşletme adı", required=True, max_len=80)

    review_url = text_field("google_review_url", "Google yorum linki", max_len=500)
    if review_url and not review_url.startswith(("http://", "https://")):
        errors.append("Google yorum linki http:// veya https:// ile başlamalı.")
    updates["google_review_url"] = review_url

    timezone_name = (request.form.get("timezone") or "").strip() or config.DEFAULT_TIMEZONE
    try:
        ZoneInfo(timezone_name)
    except Exception:
        errors.append("Saat dilimi geçersiz. Örnek: Europe/Istanbul")
        timezone_name = current.get("timezone") or config.DEFAULT_TIMEZONE
    updates["timezone"] = timezone_name

    updates["confirmation_lead_hours"] = f"{float_field('confirmation_lead_hours', 'Onay mesajı öne alması (saat)', 0, 72):g}"
    updates["review_delay_hours"] = f"{float_field('review_delay_hours', 'Yorum mesajı gecikmesi (saat)', 0, 168):g}"
    updates["default_duration_minutes"] = str(
        int_field("default_duration_minutes", "Varsayılan randevu süresi (dk)", 15, 480)
    )

    provider = (request.form.get("whatsapp_provider") or "simulation").strip()
    if provider not in whatsapp.PROVIDERS:
        errors.append("Geçersiz WhatsApp sağlayıcısı.")
        provider = current.get("whatsapp_provider") or "simulation"
    updates["whatsapp_provider"] = provider

    http_url = text_field("http_api_url", "HTTP API adresi", max_len=500)
    if http_url and not http_url.startswith(("http://", "https://")):
        errors.append("HTTP API adresi http:// veya https:// ile başlamalı.")
    if provider == "http" and not http_url:
        errors.append("HTTP sağlayıcısı için API adresi zorunludur.")
    updates["http_api_url"] = http_url
    submitted_token = (request.form.get("http_api_token") or "").strip()
    saved_token = current.get("http_api_token") or ""
    url_changed = http_url != (current.get("http_api_url") or "").strip()
    token_dropped = False
    if request.form.get("clear_http_token"):
        updates["http_api_token"] = ""
    elif submitted_token:
        updates["http_api_token"] = submitted_token
    elif url_changed and saved_token:
        # Adres değiştiyse eski anahtar yeni sunucuya gönderilmesin.
        updates["http_api_token"] = ""
        token_dropped = True
    else:
        # Adres aynı ve alan boşsa kayıtlı anahtar korunur (anahtar ekrana basılmaz).
        updates["http_api_token"] = saved_token
    http_mode = (request.form.get("http_mode") or "generic").strip()
    if http_mode not in ("generic", "waha"):
        http_mode = "generic"
    updates["http_mode"] = http_mode
    updates["http_session"] = text_field("http_session", "WAHA oturum adı", max_len=60) or "default"

    msg_confirmation = text_field("msg_confirmation", "Onay mesajı şablonu", required=True, max_len=900)
    msg_review = text_field("msg_review", "Yorum mesajı şablonu", required=True, max_len=900)
    updates["msg_confirmation"] = msg_confirmation
    updates["msg_review"] = msg_review
    updates["msg_manual"] = (
        text_field("msg_manual", "Manuel hatırlatma şablonu", max_len=900)
        or db.SETTINGS_DEFAULTS["msg_manual"]
    )

    updates["worker_enabled"] = "1" if request.form.get("worker_enabled") else "0"
    updates["max_send_attempts"] = str(
        int_field("max_send_attempts", "Maksimum gönderim denemesi", 1, 10)
    )
    updates["retry_delay_minutes"] = str(
        int_field("retry_delay_minutes", "Tekrar deneme aralığı (dk)", 1, 1440)
    )

    if errors:
        for message in errors:
            flash(message, "error")
        return redirect(url_for("panel.settings_page"))

    db.save_settings(updates)
    resynced = db.resync_all_reminders()
    reopened = 0
    if review_url:
        # Link sonradan eklendiyse, bu nedenle atlanmış yorum mesajlarını yeniden planla.
        reopened = db.reopen_skipped_reviews()
    message = f"Ayarlar kaydedildi. {resynced} yaklaşan randevunun hatırlatma saatleri güncellendi."
    if reopened:
        message += f" {reopened} atlanmış yorum mesajı yeniden planlandı."
    flash(message, "success")
    if token_dropped:
        flash(
            "HTTP API adresi değiştiği için kayıtlı API anahtarı silindi. "
            "Yeni adres için anahtarı tekrar girin.",
            "warning",
        )
    if provider == "simulation":
        flash("Simülasyon modu açık: mesajlar gerçek WhatsApp'a gönderilmez.", "info")
    return redirect(url_for("panel.settings_page"))


# ---------------------------------------------------------------------
# Giriş / çıkış
# ---------------------------------------------------------------------
@panel.route("/giris", methods=["GET", "POST"])
def login():
    if not _auth_required() or _is_authenticated():
        return redirect(_safe_next())
    error = ""
    if request.method == "POST":
        client = request.remote_addr or "?"
        remaining = _login_throttle.locked_for(client)
        if remaining:
            error = auth.lock_message(remaining)
        else:
            expected = current_app.config.get("ADMIN_PASSWORD") or ""
            if auth.check_password(request.form.get("password") or "", expected):
                _login_throttle.reset(client)
                next_url = _safe_next()
                session.clear()  # oturum sabitleme (fixation) önlemi
                session.permanent = True
                session["auth"] = auth.fingerprint(expected)
                logger.info("Panele giriş yapıldı (%s).", client)
                return redirect(next_url)
            _login_throttle.record_failure(client)
            logger.warning("Hatalı panel parolası (%s).", client)
            time.sleep(0.5)
            error = "Parola hatalı."
    return render_template("login.html", error=error, next_url=_safe_next()), (401 if error else 200)


@panel.post("/cikis")
def logout():
    session.clear()
    flash("Çıkış yapıldı.", "info")
    return redirect(url_for("panel.login"))


# ---------------------------------------------------------------------
# Sağlık kontrolü (izleme / test için)
# ---------------------------------------------------------------------
@panel.get("/api/health")
def health():
    state = scheduler.status()
    pending = state.get("pending", {})
    return jsonify(
        {
            "status": "ok",
            "time_utc": utils.to_iso(utils.now_utc()),
            "database": db.get_db_path().name,  # klasör yapısı dışarı verilmez
            "timezone": _tz_name(),
            "provider": state.get("provider"),
            "worker": {
                "running": state.get("running"),
                "enabled": state.get("enabled"),
                "external": state.get("external"),
                "last_tick_at": state.get("last_tick_raw"),
                "stale": state.get("stale"),
                "error": state.get("error"),
            },
            "pending_reminders": pending,
        }
    )
