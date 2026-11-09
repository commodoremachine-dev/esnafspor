"""SQLite katmanı testleri: şema, randevu, hatırlatma planlama, iptal."""

from __future__ import annotations

import unittest
from datetime import timedelta

import db
import utils
from tests.support import make_temp_dir, remove_temp_dir


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = make_temp_dir()
        self.db_path = self._tmp / "test.db"
        db.init_db(self.db_path)

    def tearDown(self):
        db._db_path = None
        remove_temp_dir(self._tmp)

    def make_customer(self, name="Ayşe Yılmaz", phone="905551112233"):
        return db.upsert_customer(name, phone)

    def make_appointment(self, minutes_from_now=180, duration=60, **kwargs):
        customer_id = self.make_customer(**kwargs)
        return db.create_appointment(
            customer_id,
            utils.now_utc() + timedelta(minutes=minutes_from_now),
            duration,
            "Saç Kesimi",
        )


class SettingsTests(DatabaseTestCase):
    def test_defaults_present(self):
        settings = db.get_settings()
        self.assertEqual(settings["confirmation_lead_hours"], "2")
        self.assertEqual(settings["review_delay_hours"], "1")
        self.assertEqual(settings["whatsapp_provider"], "simulation")
        self.assertIn("{isim}", settings["msg_confirmation"])
        self.assertIn("{link}", settings["msg_review"])

    def test_save_settings(self):
        db.save_settings({"business_name": "Kuaför Deniz", "confirmation_lead_hours": "3"})
        self.assertEqual(db.get_setting("business_name"), "Kuaför Deniz")
        self.assertEqual(db.get_float_setting("confirmation_lead_hours", 2), 3.0)


class AppointmentTests(DatabaseTestCase):
    def test_create_and_list(self):
        appointment_id = self.make_appointment(minutes_from_now=120)
        appointment = db.get_appointment(appointment_id)
        self.assertIsNotNone(appointment)
        self.assertEqual(appointment["customer_name"], "Ayşe Yılmaz")
        self.assertEqual(appointment["status"], "scheduled")
        self.assertEqual(db.find_customer_by_phone("905551112233")["name"], "Ayşe Yılmaz")
        rows = db.list_appointments()
        self.assertEqual(len(rows), 1)

    def test_reminders_are_scheduled_two_hours_before_and_after_end(self):
        appointment_id = self.make_appointment(minutes_from_now=240, duration=60)
        reminders = db.get_reminders_for([appointment_id])[appointment_id]
        starts_at = utils.parse_iso(db.get_appointment(appointment_id)["starts_at"])
        confirmation_due = utils.parse_iso(reminders["confirmation"]["due_at"])
        review_due = utils.parse_iso(reminders["review"]["due_at"])
        self.assertEqual(confirmation_due, starts_at - timedelta(hours=2))
        self.assertEqual(review_due, starts_at + timedelta(hours=1, minutes=60))
        self.assertEqual(reminders["confirmation"]["status"], "pending")

    def test_update_appointment_reschedules_pending_reminders(self):
        appointment_id = self.make_appointment(minutes_from_now=240)
        old_due = utils.parse_iso(
            db.get_reminders_for([appointment_id])[appointment_id]["confirmation"]["due_at"]
        )
        new_start = utils.now_utc() + timedelta(hours=8)
        db.update_appointment(appointment_id, starts_at=new_start, duration_minutes=90)
        reminders = db.get_reminders_for([appointment_id])[appointment_id]
        new_due = utils.parse_iso(reminders["confirmation"]["due_at"])
        self.assertNotEqual(old_due, new_due)
        self.assertEqual(reminders["review"]["status"], "pending")

    def test_conflict_detection(self):
        customer_id = self.make_customer()
        start = utils.now_utc() + timedelta(hours=5)
        db.create_appointment(customer_id, start, 60, "İşlem 1")
        self.assertIsNotNone(db.find_conflict(customer_id, start + timedelta(minutes=30), 60))
        self.assertIsNone(db.find_conflict(customer_id, start + timedelta(hours=2), 60))

    def test_cancel_skips_pending_reminders(self):
        appointment_id = self.make_appointment(minutes_from_now=240)
        db.set_appointment_status(appointment_id, "cancelled")
        reminders = db.get_reminders_for([appointment_id])[appointment_id]
        self.assertEqual(reminders["confirmation"]["status"], "skipped")
        self.assertEqual(reminders["review"]["status"], "skipped")

    def test_confirm_skips_confirmation_only(self):
        appointment_id = self.make_appointment(minutes_from_now=240)
        db.set_appointment_status(appointment_id, "confirmed")
        reminders = db.get_reminders_for([appointment_id])[appointment_id]
        self.assertEqual(reminders["confirmation"]["status"], "skipped")
        self.assertEqual(reminders["review"]["status"], "pending")

    def test_reopening_cancelled_appointment_restores_reminders(self):
        appointment_id = self.make_appointment(minutes_from_now=240)
        db.set_appointment_status(appointment_id, "cancelled")
        db.set_appointment_status(appointment_id, "scheduled")
        reminders = db.get_reminders_for([appointment_id])[appointment_id]
        self.assertEqual(reminders["confirmation"]["status"], "pending")
        self.assertEqual(reminders["review"]["status"], "pending")

    def test_claim_reminder_is_atomic(self):
        appointment_id = self.make_appointment(minutes_from_now=60)
        reminder_id = db.get_reminders_for([appointment_id])[appointment_id]["confirmation"]["id"]
        now = utils.now_utc()
        self.assertTrue(db.claim_reminder(reminder_id, now=now))
        # İkinci süreç/thread aynı işi sahiplenemez (kira süresi dolana kadar).
        self.assertFalse(db.claim_reminder(reminder_id, now=now))
        self.assertEqual(db.get_reminder(reminder_id)["attempts"], 1)
        # Kira süresi dolunca yeniden denenebilir.
        self.assertTrue(db.claim_reminder(reminder_id, now=now + timedelta(minutes=6)))

    def test_upsert_customer_updates_name(self):
        customer_id = self.make_customer()
        again = self.make_customer(name="Ayşe Yıldız")
        self.assertEqual(customer_id, again)
        self.assertEqual(db.get_customer(customer_id)["name"], "Ayşe Yıldız")

    def test_due_reminders_respects_review_delay(self):
        self.make_appointment(minutes_from_now=60)
        # Onay mesajı 2 saat önce olacağı için şu an vadesi gelmiş olmalı
        due_kinds = {row["kind"] for row in db.due_reminders()}
        self.assertIn("confirmation", due_kinds)
        self.assertNotIn("review", due_kinds)

    def test_dashboard_stats(self):
        self.make_appointment(minutes_from_now=120)
        tz_name = db.get_setting("timezone")
        now = utils.now_utc()
        start, end = utils.day_bounds_utc(now, tz_name)
        stats = db.dashboard_stats(start, end)
        self.assertGreaterEqual(stats["today_total"], 1)
        self.assertGreaterEqual(stats["pending_confirmation"], 1)


class SeedTests(DatabaseTestCase):
    def test_seed_demo_is_idempotent(self):
        created = db.seed_demo()
        self.assertEqual(created, 5)
        self.assertEqual(db.seed_demo(), 0)
        self.assertEqual(len(db.list_appointments(include_cancelled=True, limit=100)), 5)


class ResyncSafetyTests(DatabaseTestCase):
    """Ayarları kaydetmek (resync) mesajları yeniden kuyruğa sokmamalı."""

    def confirmation(self, appointment_id):
        return db.get_reminders_for([appointment_id])[appointment_id]["confirmation"]

    def fail_permanently(self, reminder_id):
        db.mark_reminder_failed(reminder_id, error="ağ hatası", attempts=3, final=True)

    def test_resync_keeps_permanently_failed_reminder(self):
        appointment_id = self.make_appointment(minutes_from_now=30)
        reminder = self.confirmation(appointment_id)
        self.fail_permanently(reminder["id"])

        db.resync_all_reminders()

        reminder = self.confirmation(appointment_id)
        self.assertEqual(reminder["status"], "failed")
        self.assertEqual(reminder["attempts"], 3)

    def test_resync_does_not_release_in_flight_claim(self):
        appointment_id = self.make_appointment(minutes_from_now=30)
        reminder_id = self.confirmation(appointment_id)["id"]
        self.assertTrue(db.claim_reminder(reminder_id))
        # Gönderim sürerken saat de değişse kira korunmalı.
        db.save_settings({"confirmation_lead_hours": "1"})

        db.resync_all_reminders()

        self.assertIsNotNone(db.get_reminder(reminder_id)["lease_until"])
        self.assertFalse(db.claim_reminder(reminder_id), "ikinci süreç aynı işi alamamalı")

    def test_changed_due_time_resets_failed_reminder(self):
        appointment_id = self.make_appointment(minutes_from_now=30)
        reminder = self.confirmation(appointment_id)
        self.fail_permanently(reminder["id"])

        db.update_appointment(
            appointment_id, starts_at=utils.now_utc() + timedelta(hours=6)
        )

        updated = self.confirmation(appointment_id)
        self.assertEqual(updated["status"], "pending")
        self.assertEqual(updated["attempts"], 0)
        self.assertNotEqual(updated["due_at"], reminder["due_at"])


if __name__ == "__main__":
    unittest.main()
