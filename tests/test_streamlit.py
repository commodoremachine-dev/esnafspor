"""Streamlit paneli ve onun kullandığı ortak işlevlerin testleri."""

from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import booking
import db
import utils
import whatsapp
import worker
from tests.support import make_temp_dir, remove_temp_dir

ROOT = Path(__file__).resolve().parent.parent


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = make_temp_dir()
        self.addCleanup(remove_temp_dir, self._tmp)
        self.addCleanup(setattr, db, "_db_path", None)
        db.init_db(self._tmp / "test.db")

    def local_input(self, hours_ahead: float) -> str:
        when = utils.to_tz(utils.now_utc() + timedelta(hours=hours_ahead), "Europe/Istanbul")
        return when.strftime("%Y-%m-%dT%H:%M")

    def make_appointment(self, hours_ahead=3, status=None):
        result = booking.create_booking("Ayşe Yılmaz", "0555 111 22 33", self.local_input(hours_ahead))
        self.assertTrue(result.ok, result.message)
        if status:
            db.set_appointment_status(result.appointment_id, status)
        return result.appointment_id


class BookingTests(DbTestCase):
    def test_creates_customer_appointment_and_reminders(self):
        result = booking.create_booking("Ali Çelik", "+90 532 244 55 66", self.local_input(5), service="Sakal")
        self.assertTrue(result.ok)
        appointment = db.get_appointment(result.appointment_id)
        self.assertEqual(appointment["customer_phone"], "905322445566")
        self.assertEqual(appointment["service"], "Sakal")
        self.assertIn("confirmation", db.get_reminders_for([result.appointment_id])[result.appointment_id])
        self.assertIn("planlandı", result.message)

    def test_validation_errors_are_collected(self):
        result = booking.create_booking("", "123", "dün", duration=5)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.errors), 4)
        self.assertEqual(db.list_appointments(include_cancelled=True), [])

    def test_past_time_rejected(self):
        result = booking.create_booking("Ali", "05551112233", self.local_input(-2))
        self.assertFalse(result.ok)
        self.assertIn("Geçmiş", result.message)

    def test_conflict_does_not_rename_customer(self):
        self.make_appointment(hours_ahead=3)
        result = booking.create_booking("Başka İsim", "05551112233", self.local_input(3))
        self.assertFalse(result.ok)
        self.assertIn("aynı saatte", result.message)
        self.assertEqual(db.find_customer_by_phone("905551112233")["name"], "Ayşe Yılmaz")


class ManualReminderTests(DbTestCase):
    def test_simulated_send_is_logged_without_touching_reminders(self):
        appointment_id = self.make_appointment()
        before = db.get_reminders_for([appointment_id])[appointment_id]["confirmation"]["status"]

        result = worker.send_manual_reminder(appointment_id)

        self.assertTrue(result["ok"], result)
        self.assertIn("unutmayın", result["body"])
        self.assertIn("Ayşe Yılmaz", result["body"])
        message = db.recent_messages(1)[0]
        self.assertEqual((message["kind"], message["status"]), ("manual", "simulated"))
        after = db.get_reminders_for([appointment_id])[appointment_id]["confirmation"]["status"]
        self.assertEqual(before, after)

    def test_double_tap_guard(self):
        appointment_id = self.make_appointment()
        self.assertTrue(worker.send_manual_reminder(appointment_id)["ok"])

        second = worker.send_manual_reminder(appointment_id)
        self.assertFalse(second["ok"])
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(len(db.recent_messages(10)), 1)

        third = worker.send_manual_reminder(appointment_id, allow_repeat=True)
        self.assertTrue(third["ok"])
        self.assertEqual(len(db.recent_messages(10)), 2)

    def test_cancelled_appointment_not_messaged(self):
        appointment_id = self.make_appointment(status="cancelled")
        result = worker.send_manual_reminder(appointment_id)
        self.assertFalse(result["ok"])
        self.assertEqual(db.recent_messages(10), [])

    @patch("worker.whatsapp.send", return_value={"ok": False, "simulated": False, "detail": "bağlantı yok"})
    def test_failure_is_logged_and_reported(self, _send):
        appointment_id = self.make_appointment()
        result = worker.send_manual_reminder(appointment_id)
        self.assertFalse(result["ok"])
        self.assertIn("bağlantı yok", result["message"])
        self.assertEqual(db.recent_messages(1)[0]["status"], "failed")
        # Başarısız deneme tekrar korumasını tetiklememeli.
        self.assertIsNone(db.last_message_at(appointment_id, "manual"))


class _FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ConnectionCheckTests(unittest.TestCase):
    WAHA = {
        "whatsapp_provider": "http",
        "http_api_url": "http://127.0.0.1:3000",
        "http_mode": "waha",
        "http_session": "default",
        "http_api_token": "anahtar",
    }

    def test_simulation(self):
        self.assertEqual(whatsapp.check_connection({"whatsapp_provider": "simulation"})["state"], "simulation")

    def test_waha_working_is_online(self):
        body = json.dumps({"name": "default", "status": "WORKING"}).encode()
        with patch("whatsapp.urllib.request.urlopen", return_value=_FakeResponse(body)) as urlopen:
            state = whatsapp.check_connection(self.WAHA)
        self.assertEqual(state["state"], "online")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "http://127.0.0.1:3000/api/sessions/default")
        self.assertEqual(request.get_header("Authorization"), "Bearer anahtar")

    def test_waha_scan_qr_is_offline(self):
        body = json.dumps({"status": "SCAN_QR_CODE"}).encode()
        with patch("whatsapp.urllib.request.urlopen", return_value=_FakeResponse(body)):
            state = whatsapp.check_connection(self.WAHA)
        self.assertEqual(state["state"], "offline")
        self.assertIn("SCAN_QR_CODE", state["detail"])

    def test_unreachable_gateway_is_offline(self):
        error = urllib.error.URLError("bağlantı reddedildi")
        with patch("whatsapp.urllib.request.urlopen", side_effect=error):
            state = whatsapp.check_connection({**self.WAHA, "http_mode": "generic"})
        self.assertEqual(state["state"], "offline")

    def test_generic_gateway_404_counts_as_reachable(self):
        error = urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)
        with patch("whatsapp.urllib.request.urlopen", side_effect=error):
            state = whatsapp.check_connection({**self.WAHA, "http_mode": "generic"})
        self.assertEqual(state["state"], "online")


try:
    from streamlit.testing.v1 import AppTest
except ImportError:  # Streamlit isteğe bağlıdır
    AppTest = None


@unittest.skipIf(AppTest is None, "streamlit kurulu değil")
class StreamlitAppTests(DbTestCase):
    def setUp(self):
        super().setUp()
        env = patch.dict(
            os.environ,
            {
                "ESNAF_DB": str(self._tmp / "test.db"),
                "ESNAF_DATA_DIR": str(self._tmp),
                "ESNAF_STREAMLIT_SCHEDULER": "0",
                "ESNAF_PASSWORD": "",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        db.save_settings({"business_name": "Kuaför Ayşe"})

    def run_app(self, address="127.0.0.1"):
        app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=30)
        with patch("streamlit.get_option", side_effect=lambda key: address if key == "server.address" else None):
            app.run()
        return app

    def heartbeat(self, minutes_ago=0):
        tick = utils.now_utc() - timedelta(minutes=minutes_ago)
        db.set_meta("scheduler.last_tick_at", utils.to_iso(tick))

    def test_renders_on_loopback_without_password(self):
        self.make_appointment(hours_ahead=2)
        self.heartbeat()
        app = self.run_app()
        self.assertFalse(app.exception, app.exception)
        self.assertEqual(app.title[0].value, "💬 Esnaf Randevu")
        self.assertTrue(any("🟡 Simülasyon" in item.value for item in app.markdown))

    def test_status_offline_when_engine_silent(self):
        self.heartbeat(minutes_ago=30)
        app = self.run_app()
        self.assertTrue(any("🔴 Sistem Çevrimdışı" in item.value for item in app.markdown))

    @patch("whatsapp.check_connection", return_value={"state": "online", "detail": "WhatsApp oturumu bağlı (WORKING)."})
    def test_status_online(self, _check):
        db.save_settings({"whatsapp_provider": "http", "http_api_url": "http://127.0.0.1:3000"})
        self.heartbeat()
        app = self.run_app()
        self.assertTrue(any("🟢 Sistem Çevrimiçi" in item.value for item in app.markdown))

    def test_refuses_lan_without_password(self):
        app = self.run_app(address="0.0.0.0")
        self.assertTrue(any("parola tanımlı değil" in item.value for item in app.error))
        self.assertEqual(len(app.button), 0)

    def test_password_gate(self):
        os.environ["ESNAF_PASSWORD"] = "gizli-parola"
        app = self.run_app(address="0.0.0.0")
        self.assertFalse(any("Randevular" in item.value for item in app.subheader))
        app.text_input[0].input("yanlis")
        with patch("time.sleep"):
            app.button[0].click().run()
        self.assertTrue(any("Parola hatalı" in item.value for item in app.error))

        app.text_input[0].input("gizli-parola")
        app.button[0].click().run()
        self.assertFalse(app.exception, app.exception)
        self.assertEqual(app.title[0].value, "💬 Esnaf Randevu")
        self.assertNotIn("gizli-parola", str(app.session_state["auth"]))

    def test_quick_booking_form(self):
        app = self.run_app()
        app.text_input(key="rf_name").input("Mehmet Demir")
        app.text_input(key="rf_phone").input("0532 244 55 66")
        app.button(key="FormSubmitter:hizli_randevu-Randevuyu kaydet").click().run()
        self.assertFalse(app.exception, app.exception)
        self.assertTrue(any("Randevu eklendi" in item.value for item in app.success))
        self.assertEqual(app.text_input(key="rf_name").value, "", "başarıdan sonra form temizlenmeli")
        self.assertEqual(db.find_customer_by_phone("905322445566")["name"], "Mehmet Demir")

    # --- kart görünümü ---------------------------------------------------
    def make_today_appointment(self, name="Ayşe Yılmaz", phone="0555 111 22 33", status=None):
        """Bugün için gelecekte bir randevu (gece yarısına yakınsa yarına düşer)."""
        tz = "Europe/Istanbul"
        now_local = utils.local_now(tz)
        end_of_day = now_local.replace(hour=23, minute=59, second=0, microsecond=0)
        minutes_left = (end_of_day - now_local).total_seconds() / 60
        hours = min(2.0, max(minutes_left - 10, 10) / 60)
        result = booking.create_booking(name, phone, self.local_input(hours))
        self.assertTrue(result.ok, result.message)
        if status:
            db.set_appointment_status(result.appointment_id, status)
        starts = utils.to_tz(utils.parse_iso(db.get_appointment(result.appointment_id)["starts_at"]), tz)
        self.view = "Bugün" if starts.date() == now_local.date() else "Yarın"
        return result.appointment_id

    def run_cards(self):
        app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=30)
        app.session_state["day_view"] = getattr(self, "view", "Bugün")
        with patch("streamlit.get_option", side_effect=lambda key: "127.0.0.1" if key == "server.address" else None):
            app.run()
        self.assertFalse(app.exception, app.exception)
        return app

    def markdown_text(self, app):
        return "\n".join(item.value for item in app.markdown)

    def test_cards_replace_table(self):
        appointment_id = self.make_today_appointment()
        app = self.run_cards()
        self.assertEqual(len(app.dataframe), 0, "tablo yerine kart kullanılmalı")
        text = self.markdown_text(app)
        self.assertIn("Ayşe Yılmaz", text)
        self.assertIn(":orange-badge[⏳ Cevap yok]", text)
        self.assertIn("tel:+905551112233", text)
        self.assertEqual(app.button(key=f"remind_{appointment_id}").label, "📲 Hatırlat")
        self.assertEqual(app.button(key=f"confirm_{appointment_id}").label, "✅ Onayladı")

    def test_card_escapes_markdown_in_customer_data(self):
        self.make_today_appointment(name="[tıkla](http://kotu.example)")
        app = self.run_cards()
        text = self.markdown_text(app)
        self.assertNotIn("](http://kotu", text)
        self.assertIn(r"\[tıkla\]", text)

    def test_card_remind_button_and_double_tap_guard(self):
        appointment_id = self.make_today_appointment()
        app = self.run_cards()
        app.button(key=f"remind_{appointment_id}").click().run()
        self.assertTrue(any("simüle edildi" in item.value for item in app.toast))
        self.assertEqual(db.recent_messages(1)[0]["kind"], "manual")

        app.button(key=f"remind_{appointment_id}").click().run()
        self.assertTrue(any("az önce" in item.value for item in app.warning))
        self.assertEqual(len(db.recent_messages(10)), 1, "ikinci dokunuş mesaj göndermemeli")

        app.button(key=f"repeat_{appointment_id}").click().run()
        self.assertEqual(len(db.recent_messages(10)), 2)

    def test_card_confirm_button(self):
        appointment_id = self.make_today_appointment()
        app = self.run_cards()
        app.button(key=f"confirm_{appointment_id}").click().run()
        self.assertEqual(db.get_appointment(appointment_id)["status"], "confirmed")
        self.assertIn(":green-badge[✅ Onayladı]", self.markdown_text(app))
        self.assertNotIn(f"confirm_{appointment_id}", [button.key for button in app.button])
        self.assertIn(f"remind_{appointment_id}", [button.key for button in app.button])

    def test_cancelled_card_has_no_actions(self):
        appointment_id = self.make_today_appointment(status="cancelled")
        app = self.run_cards()
        self.assertIn(":red-badge[❌ İptal]", self.markdown_text(app))
        self.assertNotIn(f"remind_{appointment_id}", [button.key for button in app.button])

    def test_card_opt_out_disables_reminder_button(self):
        appointment_id = self.make_today_appointment()
        app = self.run_cards()
        app.button(key=f"optout_{appointment_id}").click().run()
        self.assertIn(":red-badge[🚫 Mesaj istemiyor]", self.markdown_text(app))
        self.assertTrue(app.button(key=f"remind_{appointment_id}").disabled)
        self.assertEqual(app.button(key=f"optout_{appointment_id}").label, "↩️ Mesaj iznini geri ver")

    def test_card_cancel_button(self):
        appointment_id = self.make_today_appointment()
        app = self.run_cards()
        app.button(key=f"cancel_{appointment_id}").click().run()
        self.assertEqual(db.get_appointment(appointment_id)["status"], "cancelled")
        self.assertIn(":red-badge[❌ İptal]", self.markdown_text(app))

    def test_tomorrow_view(self):
        tomorrow_noon = (utils.local_now("Europe/Istanbul") + timedelta(days=1)).strftime("%Y-%m-%dT12:00")
        result = booking.create_booking("Yarınki Müşteri", "05321112233", tomorrow_noon)
        self.assertTrue(result.ok, result.message)

        self.view = "Bugün"
        self.assertNotIn("Yarınki Müşteri", self.markdown_text(self.run_cards()))
        self.view = "Yarın"
        self.assertIn("Yarınki Müşteri", self.markdown_text(self.run_cards()))

if __name__ == "__main__":
    unittest.main()
