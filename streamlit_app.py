"""Esnaf Randevu Asistanı — telefondan hızlı kullanım için Streamlit paneli.

Çalıştırma (proje klasöründen):
    streamlit run streamlit_app.py                              # yalnızca bu bilgisayar
    streamlit run streamlit_app.py --server.address 0.0.0.0     # telefondan (parola zorunlu)

Flask paneliyle (`python main.py`) aynı SQLite veritabanını kullanır; ikisi
aynı anda açık olabilir. Ayarlar (şablonlar, WhatsApp bağlantısı) Flask
panelinin Ayarlar sayfasından yapılır.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import streamlit as st

import config

config.load_dotenv()

import auth  # noqa: E402  (yollar .env'den sonra çözülsün)
import booking  # noqa: E402
import db  # noqa: E402
import scheduler  # noqa: E402
import utils  # noqa: E402
import whatsapp  # noqa: E402
import worker  # noqa: E402

REFRESH_SECONDS = 15


st.set_page_config(
    page_title="Esnaf Randevu",
    page_icon="💬",
    layout="centered",
    initial_sidebar_state="collapsed",
)


# ---------------------------------------------------------------------
# Açılış: veritabanı ve arka plan motoru (süreç başına bir kez)
# ---------------------------------------------------------------------
@st.cache_resource
def _bootstrap() -> None:
    config.ensure_dirs()
    config.setup_logging()
    db.init_db()


@st.cache_resource
def _start_scheduler() -> str:
    """Hiç zamanlayıcı çalışmıyorsa bu süreçte başlatır.

    `python main.py` veya `python scheduler.py` zaten çalışıyorsa tek örnek
    kilidi sayesinde ikinci bir motor açılmaz; mesajları o süreç gönderir.
    """
    if os.environ.get("ESNAF_STREAMLIT_SCHEDULER", "1") != "1":
        return "disabled"
    thread = scheduler.Scheduler()
    try:
        thread.start()
    except scheduler.SchedulerAlreadyRunning:
        return "external"
    return "internal"


_bootstrap()
_start_scheduler()

# Dokunma dostu ölçüler: düğmeler en az 48 px; telefonda başlık tek satıra sığsın.
st.html(
    """
    <style>
      .stButton button, .stFormSubmitButton button { min-height: 48px; font-size: 1rem; }
      @media (max-width: 640px) { h1 { font-size: 1.75rem !important; } }
    </style>
    """
)


# ---------------------------------------------------------------------
# Parola
# ---------------------------------------------------------------------
@st.cache_resource
def _throttle() -> auth.LoginThrottle:
    return auth.LoginThrottle()


def _client_id() -> str:
    return str(getattr(st.context, "ip_address", None) or "streamlit")


def require_login() -> None:
    password = config.admin_password()
    address = str(st.get_option("server.address") or "")
    if not password:
        if config.is_loopback(address):
            return
        st.error(
            "Panel yerel ağa açık ama parola tanımlı değil. Güvenlik için durduruldu.\n\n"
            "`.env` dosyasına `ESNAF_PASSWORD=guclu-bir-parola` ekleyip paneli yeniden başlatın."
        )
        st.stop()
    if auth.session_is_valid(st.session_state.get("auth"), password):
        return

    st.title("💬 Esnaf Randevu")
    with st.form("giris"):
        supplied = st.text_input("Parola", type="password")
        submitted = st.form_submit_button("Giriş yap", type="primary", width="stretch")
    if submitted:
        client = _client_id()
        remaining = _throttle().locked_for(client)
        if remaining:
            st.error(auth.lock_message(remaining))
        elif auth.check_password(supplied, password):
            _throttle().reset(client)
            st.session_state["auth"] = auth.fingerprint(password)
            st.rerun()
        else:
            _throttle().record_failure(client)
            time.sleep(0.5)
            st.error("Parola hatalı.")
    st.stop()


# ---------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------
def _tz() -> str:
    return db.get_setting("timezone") or config.DEFAULT_TIMEZONE


@st.cache_data(ttl=20, show_spinner=False)
def _whatsapp_state(provider: str, url: str, mode: str, session: str, token: str) -> dict:
    return whatsapp.check_connection(
        {
            "whatsapp_provider": provider,
            "http_api_url": url,
            "http_mode": mode,
            "http_session": session,
            "http_api_token": token,
        },
        timeout=3.0,
    )


def system_status() -> tuple[str, str, list[str]]:
    """(rozet, başlık, ayrıntılar) döndürür."""
    settings = db.get_settings()
    wa = _whatsapp_state(
        settings.get("whatsapp_provider") or "simulation",
        settings.get("http_api_url") or "",
        settings.get("http_mode") or "generic",
        settings.get("http_session") or "default",
        settings.get("http_api_token") or "",
    )
    state = scheduler.status()
    last_tick = state.get("last_tick_at")
    engine_alive = last_tick is not None and not state.get("stale")
    if not state.get("enabled"):
        engine_text = "Otomatik gönderim ayarlardan kapalı."
    elif engine_alive:
        engine_text = f"Hatırlatma motoru çalışıyor (son kontrol {utils.fmt_time(utils.to_tz(last_tick, _tz()))})."
    else:
        engine_text = "Hatırlatma motoru yanıt vermiyor."
    details = [f"WhatsApp: {wa['detail']}", engine_text]
    if state.get("error"):
        details.append("Motor son turda hata verdi; ayrıntı için günlük dosyasına bakın.")

    engine_ok = engine_alive and state.get("enabled")
    if wa["state"] == "online" and engine_ok:
        return "🟢", "Sistem Çevrimiçi", details
    if wa["state"] == "simulation" and engine_ok:
        return "🟡", "Simülasyon modu (gerçek mesaj gitmiyor)", details
    if wa["state"] == "unknown" and engine_ok:
        return "🟡", "Sistem çalışıyor (WhatsApp durumu bilinmiyor)", details
    return "🔴", "Sistem Çevrimdışı", details


APPOINTMENT_BADGES = {
    "confirmed": ("green", "✅ Onayladı"),
    "completed": ("green", "✅ Geldi"),
    "scheduled": ("orange", "⏳ Cevap yok"),
    "cancelled": ("red", "❌ İptal"),
    "no_show": ("red", "❌ Gelmedi"),
}
REMINDER_BADGES = {
    "sent": ("green", "📩 Hatırlatma gitti"),
    "pending": ("orange", "⏳ Hatırlatma"),
    "failed": ("red", "⚠️ Hatırlatma gitmedi"),
    "skipped": ("gray", "➖ Hatırlatma atlandı"),
}
_MD_SPECIAL = set("\\`*_{}[]()#+-.!|<>~$:")


def md(text: object) -> str:
    """Kullanıcı verisini Markdown'da düz metin olarak gösterir (bağlantı/biçim enjeksiyonu yok)."""
    return "".join("\\" + char if char in _MD_SPECIAL else char for char in str(text or ""))


def _badge(color: str, text: str) -> str:
    return f":{color}-badge[{md(text)}]"


def appointments_for(day_offset: int, tz_name: str) -> tuple[list[dict], dict]:
    start, end = utils.day_bounds_utc(utils.now_utc() + timedelta(days=day_offset), tz_name)
    items = db.list_appointments(
        start_utc=start, end_utc=end, include_cancelled=True, limit=200, order="asc"
    )
    return items, db.get_reminders_for([item["id"] for item in items])


def _local_time(iso_value: str | None, tz_name: str) -> str:
    parsed = utils.parse_iso(iso_value)
    return utils.fmt_time(utils.to_tz(parsed, tz_name)) if parsed else ""


def card_badges(item: dict, confirmation: dict, tz_name: str) -> str:
    color, text = APPOINTMENT_BADGES.get(item["status"], ("gray", item["status"]))
    badges = [_badge(color, text)]
    status = confirmation.get("status")
    if status in REMINDER_BADGES:
        color, text = REMINDER_BADGES[status]
        if status == "sent":
            text += " " + _local_time(confirmation.get("sent_at"), tz_name)
        elif status == "pending":
            text += " " + _local_time(confirmation.get("due_at"), tz_name)
        badges.append(_badge(color, text.strip()))
    return " ".join(badges)


# --- Kart düğmeleri (callback: tıklama anında, sayfa yenilenmeden önce çalışır)
def _send_manual(appointment_id: int, allow_repeat: bool = False) -> None:
    st.session_state.pop("repeat_for", None)
    result = worker.send_manual_reminder(appointment_id, allow_repeat=allow_repeat)
    if result.get("duplicate"):
        st.session_state["repeat_for"] = appointment_id
        return
    st.session_state["toast"] = ("✅ " if result.get("ok") else "⚠️ ") + str(result.get("message"))


def _mark_confirmed(appointment_id: int, name: str) -> None:
    _set_status(appointment_id, "confirmed", f"✅ {name} onayladı olarak işaretlendi.")


def _set_status(appointment_id: int, status: str, message: str) -> None:
    db.set_appointment_status(appointment_id, status)
    st.session_state["toast"] = message


def _set_opt_out(customer_id: int, opted_out: bool, name: str) -> None:
    db.set_customer_opt_out(customer_id, opted_out)
    st.session_state["toast"] = (
        f"🚫 {name} artık mesaj almayacak." if opted_out else f"↩️ {name} yeniden mesaj alabilir."
    )


def appointment_card(item: dict, confirmation: dict, tz_name: str) -> None:
    appointment_id = int(item["id"])
    active = item["status"] in ("scheduled", "confirmed")
    opted_out = bool(int(item.get("customer_opted_out") or 0))
    name = item["customer_name"]
    with st.container(border=True, key=f"card_{appointment_id}"):
        st.markdown(f"**{_local_time(item['starts_at'], tz_name)}** · **{md(name)}**")
        badges = card_badges(item, confirmation, tz_name)
        if opted_out:
            badges += " " + _badge("red", "🚫 Mesaj istemiyor")
        st.markdown(badges)
        phone = str(item["customer_phone"] or "")
        lines = [f"📞 [{md(whatsapp.format_phone_display(phone))}](tel:+{phone})"] if phone.isdigit() else []
        if item.get("service"):
            lines.append(f"✂️ {md(item['service'])}")
        if lines:
            st.markdown(" · ".join(lines))
        if not active:
            return
        col_remind, col_confirm = st.columns(2, gap="small")
        col_remind.button(
            "📲 Hatırlat",
            key=f"remind_{appointment_id}",
            type="primary",
            width="stretch",
            on_click=_send_manual,
            args=(appointment_id,),
            disabled=opted_out,
            help="Müşteri mesaj istemiyor." if opted_out else None,
        )
        if item["status"] == "scheduled":
            col_confirm.button(
                "✅ Onayladı",
                key=f"confirm_{appointment_id}",
                width="stretch",
                on_click=_mark_confirmed,
                args=(appointment_id, item["customer_name"]),
            )
        if st.session_state.get("repeat_for") == appointment_id:
            st.warning("Bu müşteriye az önce hatırlatma gönderildi.")
            st.button(
                "Yine de tekrar gönder",
                key=f"repeat_{appointment_id}",
                width="stretch",
                on_click=_send_manual,
                args=(appointment_id, True),
            )
        with st.expander("⋯ Diğer"):
            st.button(
                "↩️ Mesaj iznini geri ver" if opted_out else "🚫 Müşteri mesaj istemiyor",
                key=f"optout_{appointment_id}",
                width="stretch",
                on_click=_set_opt_out,
                args=(int(item["customer_id"]), not opted_out, name),
            )
            st.button(
                "❌ Randevuyu iptal et",
                key=f"cancel_{appointment_id}",
                width="stretch",
                on_click=_set_status,
                args=(appointment_id, "cancelled", f"❌ {name} randevusu iptal edildi."),
            )


# ---------------------------------------------------------------------
# Bölümler
# ---------------------------------------------------------------------
@st.fragment(run_every=f"{REFRESH_SECONDS}s")
def live_section() -> None:
    toast = st.session_state.pop("toast", None)
    if toast:
        st.toast(toast)
    tz_name = _tz()
    badge, title, details = system_status()
    st.markdown(f"#### {badge} {md(title)}")
    st.caption(" · ".join(md(detail) for detail in details))

    st.subheader("Randevular")
    view = st.segmented_control(
        "Gün", ["Bugün", "Yarın"], default="Bugün", key="day_view", label_visibility="collapsed"
    )
    day_offset = 1 if view == "Yarın" else 0
    items, reminders = appointments_for(day_offset, tz_name)
    if not items:
        st.info("Bu gün için randevu yok.")
    for item in items:
        appointment_card(item, reminders.get(item["id"], {}).get("confirmation") or {}, tz_name)
    template = db.get_setting("msg_manual") or db.SETTINGS_DEFAULTS["msg_manual"]
    st.caption(f"📲 Hatırlat düğmesi şu mesajı gönderir: “{md(template)}” · Liste {REFRESH_SECONDS} sn'de bir yenilenir.")


def _submit_booking() -> None:
    """Form gönderilince çalışır; başarıda alanları temizler."""
    state = st.session_state
    when = datetime.combine(state["rf_day"], state["rf_time"]).strftime("%Y-%m-%dT%H:%M")
    result = booking.create_booking(
        state.get("rf_name", ""),
        state.get("rf_phone", ""),
        when,
        duration=state.get("rf_duration"),
        service=state.get("rf_service", ""),
    )
    state["flash"] = ("success" if result.ok else "error", result.message)
    if result.ok:
        for key in ("rf_name", "rf_phone", "rf_service"):
            state[key] = ""


def booking_form() -> None:
    tz_name = _tz()
    now_local = utils.local_now(tz_name)
    suggested = utils.next_quarter_hour(now_local + timedelta(hours=1))
    if st.session_state.get("rf_day") is None or st.session_state["rf_day"] < now_local.date():
        # Sayfa gece yarısını geçerek açık kaldıysa eski gün min_value'yu ihlal etmesin.
        st.session_state["rf_day"] = suggested.date()
    st.session_state.setdefault("rf_time", suggested.time().replace(tzinfo=None))
    default_duration = booking.default_duration()
    durations = sorted({15, 30, 45, 60, 90, 120, 180, default_duration})

    st.subheader("➕ Hızlı randevu")
    with st.form("hizli_randevu", border=True):
        st.text_input("Müşteri adı", key="rf_name", max_chars=booking.NAME_MAX, placeholder="Ayşe Yılmaz")
        st.text_input("Telefon", key="rf_phone", max_chars=20, placeholder="0555 111 22 33")
        col_day, col_time = st.columns(2)
        col_day.date_input("Gün", key="rf_day", min_value=now_local.date(), format="DD.MM.YYYY")
        col_time.time_input("Saat", key="rf_time", step=timedelta(minutes=15))
        with st.expander("Hizmet ve süre (isteğe bağlı)"):
            st.text_input("Hizmet", key="rf_service", max_chars=booking.SERVICE_MAX, placeholder="Saç kesimi")
            st.selectbox(
                "Süre (dk)",
                durations,
                index=durations.index(default_duration),
                key="rf_duration",
            )
        st.form_submit_button(
            "Randevuyu kaydet", type="primary", width="stretch", on_click=_submit_booking
        )

    flash = st.session_state.pop("flash", None)
    if flash:
        (st.success if flash[0] == "success" else st.error)(flash[1])


def sidebar() -> None:
    with st.sidebar:
        st.markdown(f"**{md(db.get_setting('business_name') or 'İşletme')}**")
        st.caption(
            "Mesaj şablonları ve WhatsApp bağlantısı Flask panelinin Ayarlar sayfasından "
            "(python main.py) değiştirilir."
        )
        if config.admin_password() and st.button("Çıkış yap", width="stretch"):
            st.session_state.clear()
            st.rerun()


# ---------------------------------------------------------------------
# Sayfa
# ---------------------------------------------------------------------
require_login()
sidebar()
st.title("💬 Esnaf Randevu")
live_section()
booking_form()
