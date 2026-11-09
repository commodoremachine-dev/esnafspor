"""Otomatik mesaj motoru testleri: gönderim, tekrar koruması, hata, iptal."""

from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import patch

import db
import utils
import worker
from tests.support import make_temp_dir, remove_temp_dir


class WorkerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = make_temp_dir()
        self.db_path = self._tmp / "worker.db"
        db.init_db(self.db_path)
        db.save_settings(
            {
                "whatsapp_provider": "simulation",
                "google_review_url": "https://g.page/r/test/review",
                "confirmation_lead_hours": "2",
                "review_delay_hours": "1",
                "max_send_attempts": "2",
                "retry_delay_minutes": "1",
                "worker_enabled": "1",
            }
        )
        self.now = utils.now_utc()

    def tearDown(self):
        db._db_path = None
        remove_temp_dir(self._tmp)

    def make_appointment(self, *, start: object = None, duration: int = 60):
        customer_id = db.upsert_customer("Ayşe Yılmaz", "905551112233")
        start = start or (self.now + timedelta(minutes=90))
        return db.create_appointment(customer_id, start, duration, "Saç Kesimi")

    def reminder(self, appointment_id: int, kind: str):
        return db.get_reminders_for([appointment_id])[appointment_id][kind]

    def test_confirmation_is_sent_once(self):
        appointment_id = self.make_appointment()
        first = worker.process_due_reminders(self.now, respect_enabled=False)
        self.assertEqual(first["sent"], 1)
        self.assertEqual(first["simulated"], 1)
        self.assertEqual(self.reminder(appointment_id, "confirmation")["status"], "sent")

        second = worker.process_due_reminders(self.now, respect_enabled=False)
        self.assertEqual(second["checked"], 0)
        self.assertEqual(len(db.recent_messages(10)), 1)

    def test_review_is_sent_after_appointment_end(self):
        appointment_id = self.make_appointment()
        worker.process_due_reminders(self.now, respect_enabled=False)
        # Onay mesajı gitti; yorum mesajı henüz zamanı gelmedi
        self.assertEqual(self.reminder(appointment_id, "review")["status"], "pending")

        later = self.now + timedelta(hours=4)
        result = worker.process_due_reminders(later, respect_enabled=False)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(self.reminder(appointment_id, "review")["status"], "sent")
        message = db.recent_messages(5)[0]
        self.assertEqual(message["kind"], "review")
        self.assertIn("https://g.page/r/test/review", message["body"])

    def test_confirmation_is_skipped_if_appointment_started(self):
        # Randevu 30 dk önce başladı: onay mesajı atlanır, yorum mesajı henüz vadesi gelmedi.
        appointment_id = self.make_appointment(start=self.now - timedelta(minutes=30))
        result = worker.process_due_reminders(self.now, respect_enabled=False)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.reminder(appointment_id, "confirmation")["status"], "skipped")
        self.assertEqual(self.reminder(appointment_id, "review")["status"], "pending")

    def test_cancelled_appointment_is_not_processed(self):
        appointment_id = self.make_appointment()
        db.set_appointment_status(appointment_id, "cancelled")
        result = worker.process_due_reminders(self.now, respect_enabled=False)
        self.assertEqual(result["checked"], 0)
        self.assertEqual(len(db.recent_messages(10)), 0)

    def test_manual_send_before_due(self):
        appointment_id = self.make_appointment(start=self.now + timedelta(hours=10))
        confirmation = self.reminder(appointment_id, "confirmation")
        result = worker.send_reminder_now(confirmation["id"])
        self.assertTrue(result["ok"])
        self.assertEqual(self.reminder(appointment_id, "confirmation")["status"], "sent")

    def test_manual_send_rejects_already_sent(self):
        appointment_id = self.make_appointment()
        worker.process_due_reminders(self.now, respect_enabled=False)
        confirmation = self.reminder(appointment_id, "confirmation")
        result = worker.send_reminder_now(confirmation["id"])
        self.assertFalse(result["ok"])
        self.assertIn("zaten", result["message"])

    @patch("worker.whatsapp.send")
    def test_retries_then_marks_failed(self, send_mock):
        send_mock.return_value = {
            "ok": False,
            "provider": "http",
            "simulated": False,
            "detail": "bağlantı hatası",
        }
        appointment_id = self.make_appointment()
        first = worker.process_due_reminders(self.now, respect_enabled=False)
        self.assertEqual(first["failed"], 1)
        reminder = self.reminder(appointment_id, "confirmation")
        self.assertEqual(reminder["status"], "pending")
        self.assertEqual(reminder["attempts"], 1)
        self.assertIsNotNone(reminder["next_attempt_at"])

        # next_attempt_at gelmeden tekrar denenmez
        second = worker.process_due_reminders(self.now, respect_enabled=False)
        self.assertEqual(second["checked"], 0)

        # ikinci deneme başarısız -> kalıcı "failed"
        third = worker.process_due_reminders(
            self.now + timedelta(minutes=5), respect_enabled=False
        )
        self.assertEqual(third["failed"], 1)
        self.assertEqual(self.reminder(appointment_id, "confirmation")["status"], "failed")
        self.assertEqual(len(db.recent_messages(10)), 2)

    def test_review_skipped_without_google_link(self):
        db.save_settings(
            {
                "whatsapp_provider": "http",
                "http_api_url": "http://127.0.0.1:9/send",
                "google_review_url": "",
            }
        )
        appointment_id = self.make_appointment(start=self.now - timedelta(hours=2), duration=30)
        result = worker.process_due_reminders(self.now, respect_enabled=False)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(self.reminder(appointment_id, "review")["status"], "skipped")

    def test_disabled_worker(self):
        db.save_settings({"worker_enabled": "0"})
        result = worker.process_due_reminders(self.now, respect_enabled=True)
        self.assertTrue(result["disabled"])
        # Panelden zorla çalıştırma yine çalışır
        result_forced = worker.process_due_reminders(self.now, respect_enabled=False)
        self.assertFalse(result_forced["disabled"])

    def test_test_message_logs_simulated(self):
        result = worker.send_test_message("905551112233", "Merhaba test")
        self.assertTrue(result["ok"])
        message = db.recent_messages(1)[0]
        self.assertEqual(message["kind"], "test")
        self.assertEqual(message["status"], "simulated")


if __name__ == "__main__":
    unittest.main()
