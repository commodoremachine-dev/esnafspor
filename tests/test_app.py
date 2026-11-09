"""Flask arayüzü testleri: sayfalar, form doğrulama, ayarlar, manuel gönderim."""

from __future__ import annotations

import os
import unittest
from datetime import timedelta
from unittest.mock import patch

import db
import utils
from app import create_app
from tests.support import make_temp_dir, remove_temp_dir


class PanelTestBase(unittest.TestCase):
    """Ortak kurulum ve yardımcılar (test içermez)."""

    def setUp(self):
        self._tmp = make_temp_dir()
        self.addCleanup(remove_temp_dir, self._tmp)
        env_patch = patch.dict(os.environ, {"ESNAF_DATA_DIR": str(self._tmp)})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.addCleanup(self._reset_db_path)

        db.init_db(self._tmp / "app.db")
        db.save_settings(
            {
                "business_name": "Kuaför Ayşe",
                "google_review_url": "https://g.page/r/test/review",
                "whatsapp_provider": "simulation",
                "worker_enabled": "1",
            }
        )
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def _reset_db_path(self):
        db._db_path = None

    # --- yardımcılar -------------------------------------------------
    def csrf_token(self):
        with self.client.session_transaction() as session:
            token = session.get("csrf_token")
        if token:
            return token
        self.client.get("/")
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def post(self, url, data=None, **kwargs):
        payload = dict(data or {})
        payload.setdefault("csrf_token", self.csrf_token())
        return self.client.post(url, data=payload, **kwargs)

    def local_now(self):
        return utils.local_now(db.get_setting("timezone") or "Europe/Istanbul")

    def create_appointment(self, hours_ahead=3, name="Ayşe Yılmaz", phone="905551112233"):
        customer_id = db.upsert_customer(name, phone)
        start = utils.now_utc() + timedelta(hours=hours_ahead)
        return db.create_appointment(customer_id, start, 60, "Saç Kesimi")


class PanelTestCase(PanelTestBase):
    # --- sayfalar ----------------------------------------------------
    def test_panel_page_renders(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Yeni randevu", body)
        self.assertIn("Son mesajlar", body)
        self.assertIn("Kuaför Ayşe", body)

    def test_settings_page_renders(self):
        response = self.client.get("/ayarlar")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Google yorum linki", body)
        self.assertIn("Mesaj şablonları", body)

    def test_health_endpoint(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("pending_reminders", data)
        self.assertNotIn(str(self._tmp), data["database"], "mutlak yol sızmamalı")

    def test_404_page(self):
        response = self.client.get("/olmayan-sayfa")
        self.assertEqual(response.status_code, 404)

    # --- randevu formu ------------------------------------------------
    def test_create_appointment_from_form(self):
        starts_at = (self.local_now() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
        response = self.post(
            "/api/appointments",
            data={
                "name": "Mehmet Demir",
                "phone": "0555 111 22 33",
                "starts_at": starts_at,
                "duration_minutes": "60",
                "service": "Sakal Tıraşı",
                "notes": "Kapıda bekler",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Randevu eklendi", response.get_data(as_text=True))
        appointments = db.list_appointments()
        self.assertEqual(len(appointments), 1)
        self.assertEqual(appointments[0]["customer_name"], "Mehmet Demir")
        reminders = db.get_reminders_for([appointments[0]["id"]])[appointments[0]["id"]]
        self.assertEqual(set(reminders), {"confirmation", "review"})

    def test_invalid_phone_shows_error(self):
        response = self.post(
            "/api/appointments",
            data={
                "name": "Mehmet Demir",
                "phone": "123",
                "starts_at": (self.local_now() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M"),
            },
            follow_redirects=True,
        )
        self.assertIn("Telefon numarası", response.get_data(as_text=True))
        self.assertEqual(db.list_appointments(), [])

    def test_past_appointment_rejected(self):
        response = self.post(
            "/api/appointments",
            data={
                "name": "Mehmet Demir",
                "phone": "0555 111 22 33",
                "starts_at": (self.local_now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
            },
            follow_redirects=True,
        )
        self.assertIn("Geçmiş bir tarihe", response.get_data(as_text=True))
        self.assertEqual(db.list_appointments(), [])

    def test_conflicting_appointment_rejected(self):
        appointment_id = self.create_appointment(hours_ahead=5)
        appointment = db.get_appointment(appointment_id)
        starts_at = utils.to_tz(
            utils.parse_iso(appointment["starts_at"]), "Europe/Istanbul"
        ).strftime("%Y-%m-%dT%H:%M")
        response = self.post(
            "/api/appointments",
            data={
                "name": "Ayşe Yılmaz",
                "phone": "0555 111 22 33",
                "starts_at": starts_at,
                "duration_minutes": "60",
            },
            follow_redirects=True,
        )
        self.assertIn("başka bir randevusu var", response.get_data(as_text=True))
        self.assertEqual(len(db.list_appointments()), 1)

    # --- durum / silme -------------------------------------------------
    def test_status_update(self):
        appointment_id = self.create_appointment()
        response = self.post(
            f"/api/appointments/{appointment_id}/status",
            data={"status": "confirmed", "next": "/"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.get_appointment(appointment_id)["status"], "confirmed")
        reminders = db.get_reminders_for([appointment_id])[appointment_id]
        self.assertEqual(reminders["confirmation"]["status"], "skipped")

    def test_delete_appointment(self):
        appointment_id = self.create_appointment()
        response = self.post(
            f"/api/appointments/{appointment_id}/delete",
            data={"next": "/"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(db.get_appointment(appointment_id))

    # --- manuel gönderim -------------------------------------------------
    def test_manual_reminder_send(self):
        appointment_id = self.create_appointment(hours_ahead=10)
        confirmation = db.get_reminders_for([appointment_id])[appointment_id]["confirmation"]
        response = self.post(
            f"/api/reminders/{confirmation['id']}/send",
            data={"next": "/"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("gönderildi", response.get_data(as_text=True).lower())
        reminder = db.get_reminders_for([appointment_id])[appointment_id]["confirmation"]
        self.assertEqual(reminder["status"], "sent")
        self.assertEqual(len(db.recent_messages(5)), 1)

    def test_test_message_form(self):
        response = self.post(
            "/api/test-message",
            data={"phone": "0555 111 22 33", "message": "Merhaba test"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        messages = db.recent_messages(5)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["kind"], "test")
        self.assertEqual(messages[0]["status"], "simulated")

    # --- ayarlar --------------------------------------------------------
    def _settings_payload(self, **overrides):
        payload = {
            "business_name": "Kuaför Ayşe",
            "google_review_url": "https://g.page/r/test/review",
            "timezone": "Europe/Istanbul",
            "confirmation_lead_hours": "2",
            "review_delay_hours": "1",
            "default_duration_minutes": "60",
            "whatsapp_provider": "simulation",
            "http_api_url": "",
            "http_api_token": "",
            "http_mode": "generic",
            "http_session": "default",
            "msg_confirmation": "Merhaba {isim}, bugün saat {saat} randevunuzu onaylıyor musunuz?",
            "msg_review": "Hizmetimizi değerlendirir misiniz? {link}",
            "worker_enabled": "1",
            "max_send_attempts": "3",
            "retry_delay_minutes": "5",
        }
        payload.update(overrides)
        return payload

    def test_save_settings(self):
        response = self.post(
            "/api/settings",
            data=self._settings_payload(
                business_name="Kuaför Deniz",
                confirmation_lead_hours="3",
                google_review_url="https://g.page/r/yeni/review",
            ),
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Ayarlar kaydedildi", response.get_data(as_text=True))
        self.assertEqual(db.get_setting("business_name"), "Kuaför Deniz")
        self.assertEqual(db.get_setting("confirmation_lead_hours"), "3")

    def test_invalid_settings_rejected(self):
        response = self.post(
            "/api/settings",
            data=self._settings_payload(
                business_name="", confirmation_lead_hours="999", timezone="Mars/Olympus"
            ),
            follow_redirects=True,
        )
        body = response.get_data(as_text=True)
        self.assertIn("İşletme adı boş olamaz", body)
        self.assertIn("Saat dilimi geçersiz", body)
        self.assertEqual(db.get_setting("business_name"), "Kuaför Ayşe")

    def test_worker_tick_route(self):
        self.create_appointment(hours_ahead=1)
        response = self.post("/api/worker/tick", data={"next": "/"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Kontrol tamamlandı", response.get_data(as_text=True))
        self.assertGreaterEqual(len(db.recent_messages(5)), 1)

    # --- güvenlik ------------------------------------------------------
    def test_post_without_csrf_token_is_rejected(self):
        starts_at = (self.local_now() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(
            "/api/appointments",
            data={
                "name": "CSRF Kurbanı",
                "phone": "0555 111 22 33",
                "starts_at": starts_at,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Form anahtarı", response.get_data(as_text=True))
        self.assertEqual(db.list_appointments(), [])

    def test_invalid_csrf_token_is_rejected(self):
        response = self.client.post(
            "/api/appointments",
            data={"csrf_token": "yanlis", "name": "X", "phone": "0555 111 22 33"},
        )
        self.assertEqual(response.status_code, 400)

    def test_untrusted_host_header_rejected(self):
        response = self.client.get("/", headers={"Host": "evil.example.com"})
        self.assertEqual(response.status_code, 400)
        response = self.client.get("/", headers={"Host": "localhost"})
        self.assertEqual(response.status_code, 200)

    def test_open_redirect_is_blocked(self):
        appointment_id = self.create_appointment()
        response = self.post(
            f"/api/appointments/{appointment_id}/status",
            data={"status": "confirmed", "next": "//evil.example.com"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

        response = self.post(
            f"/api/appointments/{appointment_id}/status",
            data={"status": "confirmed", "next": "/\t/evil.example.com"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

    # --- form varsayılanları / doğrulama --------------------------------
    def test_custom_default_duration_is_used(self):
        db.save_settings({"default_duration_minutes": "75"})
        starts_at = (self.local_now() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
        response = self.post(
            "/api/appointments",
            data={
                "name": "Süre Testi",
                "phone": "0555 111 22 33",
                "starts_at": starts_at,
            },
            follow_redirects=True,
        )
        self.assertIn("Randevu eklendi", response.get_data(as_text=True))
        self.assertEqual(db.list_appointments()[0]["duration_minutes"], 75)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('value="75" selected', page)

    def test_conflict_does_not_overwrite_customer_notes(self):
        customer_id = db.upsert_customer("Ayşe Yılmaz", "905551112233", "orijinal not")
        start = utils.now_utc() + timedelta(hours=5)
        db.create_appointment(customer_id, start, 60, "Saç Kesimi")
        when_local = utils.to_tz(start, "Europe/Istanbul").strftime("%Y-%m-%dT%H:%M")
        response = self.post(
            "/api/appointments",
            data={
                "name": "Ayşe Yılmaz",
                "phone": "0555 111 22 33",
                "starts_at": when_local,
                "duration_minutes": "60",
                "notes": "çakışma denemesi",
            },
            follow_redirects=True,
        )
        self.assertIn("başka bir randevusu var", response.get_data(as_text=True))
        self.assertEqual(db.get_customer(customer_id)["notes"], "orijinal not")

    def test_service_and_notes_length_are_validated(self):
        starts_at = (self.local_now() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
        response = self.post(
            "/api/appointments",
            data={
                "name": "Uzun Alan",
                "phone": "0555 111 22 33",
                "starts_at": starts_at,
                "service": "x" * 81,
                "notes": "y" * 301,
            },
            follow_redirects=True,
        )
        body = response.get_data(as_text=True)
        self.assertIn("Hizmet adı 80", body)
        self.assertIn("Not 300", body)
        self.assertEqual(db.list_appointments(), [])

    def test_http_token_is_preserved_and_can_be_cleared(self):
        db.save_settings({"http_api_token": "gizli123"})
        self.post(
            "/api/settings",
            data=self._settings_payload(http_api_token=""),
            follow_redirects=True,
        )
        self.assertEqual(db.get_setting("http_api_token"), "gizli123")
        self.post(
            "/api/settings",
            data=self._settings_payload(http_api_token="", clear_http_token="1"),
            follow_redirects=True,
        )
        self.assertEqual(db.get_setting("http_api_token"), "")

    def test_skipped_review_reopened_when_link_added(self):
        db.save_settings(
            {
                "whatsapp_provider": "http",
                "http_api_url": "http://127.0.0.1:9/send",
                "google_review_url": "",
            }
        )
        appointment_id = self.create_appointment(hours_ahead=-2)
        self.post("/api/worker/tick", data={"next": "/"}, follow_redirects=True)
        reminder = db.get_reminders_for([appointment_id])[appointment_id]["review"]
        self.assertEqual(reminder["status"], "skipped")

        self.post(
            "/api/settings",
            data=self._settings_payload(
                whatsapp_provider="http",
                http_api_url="http://127.0.0.1:9/send",
                google_review_url="https://g.page/r/yeni/review",
            ),
            follow_redirects=True,
        )
        reminder = db.get_reminders_for([appointment_id])[appointment_id]["review"]
        self.assertEqual(reminder["status"], "pending")


    # --- güvenlik ---------------------------------------------------------
    def test_manual_message_template_saved(self):
        self.post(
            "/api/settings",
            data=self._settings_payload(msg_manual="Selam {isim}, yarın {saat} sizi bekliyoruz."),
        )
        self.assertEqual(db.get_setting("msg_manual"), "Selam {isim}, yarın {saat} sizi bekliyoruz.")
        self.assertIn("yarın {saat} sizi bekliyoruz", self.client.get("/ayarlar").get_data(as_text=True))

        self.post("/api/settings", data=self._settings_payload(msg_manual="   "))
        self.assertEqual(db.get_setting("msg_manual"), db.SETTINGS_DEFAULTS["msg_manual"])

    def test_opt_out_from_panel_menu(self):
        appointment_id = self.create_appointment(hours_ahead=5)
        customer_id = db.get_appointment(appointment_id)["customer_id"]
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Müşteri mesaj istemiyor", page)

        response = self.post(
            f"/api/customers/{customer_id}/opt-out",
            data={"opted_out": "1", "next": "/"},
            follow_redirects=True,
        )
        body = response.get_data(as_text=True)
        self.assertIn("🚫 Mesaj istemiyor", body)
        self.assertIn("Mesaj iznini geri ver", body)
        self.assertEqual(db.get_customer(customer_id)["opted_out"], 1)

        self.post(f"/api/customers/{customer_id}/opt-out", data={"opted_out": "0"})
        self.assertEqual(db.get_customer(customer_id)["opted_out"], 0)

    def test_long_list_shows_truncation_notice(self):
        import app as app_module

        with patch.object(app_module, "LIST_LIMIT", 2):
            for index in range(3):
                self.create_appointment(hours_ahead=3 + index * 2, phone=f"90555111223{index}")
            body = self.client.get("/").get_data(as_text=True)
        self.assertIn("İlk 2 randevu gösteriliyor", body)

    def test_default_datetime_passes_browser_validation(self):
        """Tarayıcı, value değeri min + k*step değilse formu göndermez."""
        import re
        from datetime import datetime

        body = self.client.get("/").get_data(as_text=True)
        tag = re.search(r'<input[^>]*id="starts_at"[^>]*>', body, re.S).group(0)
        value = datetime.strptime(re.search(r'value="([^"]+)"', tag).group(1), "%Y-%m-%dT%H:%M")
        minimum = datetime.strptime(re.search(r'data-min="([^"]+)"', tag).group(1), "%Y-%m-%dT%H:%M")
        step = int(re.search(r'step="(\d+)"', tag).group(1))
        self.assertGreaterEqual(value, minimum)
        self.assertEqual((value - minimum).total_seconds() % step, 0)

    def test_settings_page_lists_relative_day_placeholder(self):
        self.assertIn("{ne_zaman}", self.client.get("/ayarlar").get_data(as_text=True))

    def test_http_token_dropped_when_url_changes(self):
        db.save_settings({"http_api_url": "http://127.0.0.1:3000/send", "http_api_token": "gizli123"})
        response = self.post(
            "/api/settings",
            data=self._settings_payload(
                whatsapp_provider="http", http_api_url="http://baska-sunucu.example/send"
            ),
            follow_redirects=True,
        )
        self.assertEqual(db.get_setting("http_api_token"), "")
        self.assertIn("API anahtarı silindi", response.get_data(as_text=True))

    def test_http_token_kept_when_new_token_given_with_new_url(self):
        db.save_settings({"http_api_url": "http://127.0.0.1:3000/send", "http_api_token": "eski"})
        self.post(
            "/api/settings",
            data=self._settings_payload(
                whatsapp_provider="http",
                http_api_url="http://127.0.0.1:4000/send",
                http_api_token="yeni",
            ),
        )
        self.assertEqual(db.get_setting("http_api_token"), "yeni")

    def test_http_token_never_rendered(self):
        db.save_settings({"http_api_token": "cok-gizli-anahtar"})
        body = self.client.get("/ayarlar").get_data(as_text=True)
        self.assertNotIn("cok-gizli-anahtar", body)
        self.assertIn("Kayıtlı anahtar var", body)

    def test_security_headers_and_no_third_party_script(self):
        response = self.client.get("/")
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        body = response.get_data(as_text=True)
        self.assertNotIn("cdn.tailwindcss.com", body)
        self.assertIn("/static/app.css", body)
        self.assertNotIn("<script>", body)


class LoginTestCase(PanelTestBase):
    PASSWORD = "dogru-parola"

    def setUp(self):
        super().setUp()
        self.app.config["ADMIN_PASSWORD"] = self.PASSWORD
        import app as app_module

        app_module._login_throttle.clear()
        self.addCleanup(app_module._login_throttle.clear)

    def login(self, password):
        self.client.get("/giris")
        return self.post("/giris", data={"password": password, "next": "/ayarlar"})

    def test_pages_require_login(self):
        response = self.client.get("/ayarlar")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/giris", response.headers["Location"])
        self.assertEqual(self.client.get("/api/health").status_code, 401)

    def test_login_page_leaks_no_business_data(self):
        body = self.client.get("/giris").get_data(as_text=True)
        self.assertNotIn("Kuaför Ayşe", body)
        self.assertNotIn("esnaf.db", body)

    def test_post_without_login_is_rejected(self):
        appointment_id = self.create_appointment()
        self.client.get("/giris")
        response = self.post(f"/api/appointments/{appointment_id}/delete")
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(db.get_appointment(appointment_id))

    def test_wrong_password(self):
        response = self.login("yanlis")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Parola hatalı", response.get_data(as_text=True))
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_correct_password_and_logout(self):
        response = self.login(self.PASSWORD)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/ayarlar"))
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/health").status_code, 200)

        self.post("/cikis")
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_password_change_invalidates_session(self):
        self.login(self.PASSWORD)
        self.app.config["ADMIN_PASSWORD"] = "yeni-parola"
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_lockout_after_repeated_failures(self):
        import app as app_module

        with patch.object(app_module.time, "sleep"):
            for _ in range(app_module._login_throttle.max_failures):
                self.login("yanlis")
            response = self.login(self.PASSWORD)
        self.assertIn("Çok fazla hatalı deneme", response.get_data(as_text=True))
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_open_redirect_blocked_after_login(self):
        self.client.get("/giris")
        response = self.post("/giris", data={"password": self.PASSWORD, "next": "//evil.example"})
        self.assertEqual(response.headers["Location"], "/")


if __name__ == "__main__":
    unittest.main()
