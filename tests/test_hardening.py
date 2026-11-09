"""Optimizasyon / sağlamlaştırma turunun testleri.

Kapsam: şema göçü, atomik kayıt, gönderim kirası, pywhatkit sırası, mesaj izni
(KVKK), {ne_zaman}, günlük bakım (yedek + saklama süresi), log maskeleme,
gizli anahtar, panel küçük düzeltmeleri.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import booking
import config
import db
import scheduler
import utils
import whatsapp
import worker
from tests.support import make_temp_dir, remove_temp_dir

TZ = "Europe/Istanbul"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = make_temp_dir()
        self.addCleanup(remove_temp_dir, self._tmp)
        self.addCleanup(setattr, db, "_db_path", None)
        db.init_db(self._tmp / "test.db")

    def local_input(self, hours_ahead: float) -> str:
        return utils.to_tz(utils.now_utc() + timedelta(hours=hours_ahead), TZ).strftime("%Y-%m-%dT%H:%M")

    def book(self, hours_ahead=3, name="Ayşe Yılmaz", phone="0555 111 22 33"):
        result = booking.create_booking(name, phone, self.local_input(hours_ahead))
        self.assertTrue(result.ok, result.message)
        return result.appointment_id

    def reminder(self, appointment_id, kind="confirmation"):
        return db.get_reminders_for([appointment_id])[appointment_id][kind]


# ---------------------------------------------------------------------
# Şema göçü
# ---------------------------------------------------------------------
class MigrationTests(unittest.TestCase):
    def test_old_database_is_upgraded(self):
        tmp = make_temp_dir()
        self.addCleanup(remove_temp_dir, tmp)
        self.addCleanup(setattr, db, "_db_path", None)
        path = tmp / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE customers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE TABLE appointments (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL,
                starts_at TEXT NOT NULL, duration_minutes INTEGER NOT NULL DEFAULT 60,
                service TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'scheduled', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, appointment_id INTEGER NOT NULL,
                kind TEXT NOT NULL, due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, last_attempt_at TEXT, sent_at TEXT,
                error TEXT NOT NULL DEFAULT '', message_id INTEGER, created_at TEXT NOT NULL,
                UNIQUE (appointment_id, kind));
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO customers VALUES (1, 'Ali', '905551112233', '', 'x', 'x');
            INSERT INTO appointments VALUES (1, 1, '2030-01-01T10:00:00+00:00', 60, '', '', 'scheduled', 'x', 'x');
            INSERT INTO reminders (appointment_id, kind, due_at, status, error, created_at)
                VALUES (1, 'review', '2030-01-01T12:00:00+00:00', 'skipped',
                        'Google Yorum Linki ayarlanmamış. Ayarlar sayfasından ekleyin.', 'x');
            """
        )
        conn.execute(
            "INSERT INTO settings VALUES ('msg_confirmation', ?)", (db._OLD_CONFIRMATION_DEFAULT,)
        )
        conn.commit()
        conn.close()

        db.init_db(path)
        db.init_db(path)  # iki kez çalışmak zararsız olmalı

        self.assertEqual(db.get_customer(1)["opted_out"], 0)
        review = db.get_reminders_for([1])[1]["review"]
        self.assertEqual(review["skip_code"], db.SKIP_NO_REVIEW_LINK)
        self.assertIsNone(review["lease_until"])
        self.assertIn("{ne_zaman}", db.get_setting("msg_confirmation"))
        self.assertEqual(db.reopen_skipped_reviews(), 1)

    def test_customised_template_is_not_overwritten(self):
        tmp = make_temp_dir()
        self.addCleanup(remove_temp_dir, tmp)
        self.addCleanup(setattr, db, "_db_path", None)
        db.init_db(tmp / "t.db")
        db.save_settings({"msg_confirmation": "Kendi metnim {isim}"})
        db.init_db(tmp / "t.db")
        self.assertEqual(db.get_setting("msg_confirmation"), "Kendi metnim {isim}")


# ---------------------------------------------------------------------
# Atomik kayıt ve müşteri bilgisi
# ---------------------------------------------------------------------
class AtomicBookingTests(Base):
    def test_appointment_rolled_back_if_reminders_fail(self):
        customer_id = db.upsert_customer("Ali", "905551112233")
        with patch("db._sync_reminders", side_effect=RuntimeError("disk dolu")):
            with self.assertRaises(RuntimeError):
                db.create_appointment(customer_id, utils.now_utc() + timedelta(hours=3), 60)
        self.assertEqual(db.list_appointments(include_cancelled=True), [])

    def test_existing_customer_name_and_notes_are_kept(self):
        db.upsert_customer("Ayşe Yılmaz", "905551112233", "kalıcı not")
        result = booking.create_booking(
            "Ayse Y", "05551112233", self.local_input(3), notes="bu randevuya özel"
        )
        self.assertTrue(result.ok)
        self.assertIn("“Ayşe Yılmaz” adıyla kayıtlı", result.message)
        customer = db.find_customer_by_phone("905551112233")
        self.assertEqual((customer["name"], customer["notes"]), ("Ayşe Yılmaz", "kalıcı not"))
        self.assertEqual(db.get_appointment(result.appointment_id)["notes"], "bu randevuya özel")

    def test_concurrent_bookings_cannot_double_book(self):
        when = self.local_input(4)
        results = []

        def attempt():
            results.append(booking.create_booking("Ali", "05551112233", when))

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for r in results if r.ok), 1)
        self.assertEqual(len(db.list_appointments()), 1)

    def test_dashboard_today_excludes_no_show(self):
        appointment_id = self.book(hours_ahead=0.5)
        start, end = utils.day_bounds_utc(utils.now_utc(), TZ)
        before = db.dashboard_stats(start, end)["today_total"]
        db.set_appointment_status(appointment_id, "no_show")
        self.assertEqual(db.dashboard_stats(start, end)["today_total"], before - 1)


# ---------------------------------------------------------------------
# Gönderim kirası ve sıra
# ---------------------------------------------------------------------
class LeaseTests(Base):
    def test_send_now_does_not_steal_in_flight_reminder(self):
        appointment_id = self.book(hours_ahead=1)
        reminder_id = self.reminder(appointment_id)["id"]
        self.assertTrue(db.claim_reminder(reminder_id))  # motor şu an gönderiyor

        with patch("worker.whatsapp.send") as send:
            result = worker.send_reminder_now(reminder_id)
        self.assertFalse(result["ok"])
        self.assertIn("işleniyor", result["message"])
        send.assert_not_called()

    def test_send_now_after_failure_still_works(self):
        appointment_id = self.book(hours_ahead=1)
        reminder_id = self.reminder(appointment_id)["id"]
        db.claim_reminder(reminder_id)
        db.mark_reminder_failed(reminder_id, error="x", attempts=3, final=True)
        self.assertTrue(worker.send_reminder_now(reminder_id)["ok"])
        self.assertEqual(db.get_reminder(reminder_id)["status"], "sent")

    def test_busy_screen_does_not_burn_attempts(self):
        db.save_settings({"whatsapp_provider": "pywhatkit"})
        appointment_id = self.book(hours_ahead=1)
        reminder_id = self.reminder(appointment_id)["id"]
        worker._screen_lock.acquire()
        try:
            with patch.object(worker, "_SCREEN_LOCK_TIMEOUT", 0.05), patch("worker.whatsapp.send") as send:
                summary = worker.process_due_reminders()
        finally:
            worker._screen_lock.release()
        send.assert_not_called()
        self.assertTrue(summary["busy"])
        reminder = db.get_reminder(reminder_id)
        self.assertEqual((reminder["status"], reminder["attempts"]), ("pending", 0))
        self.assertIsNone(reminder["lease_until"])

    def test_http_sends_do_not_wait_for_screen_lock(self):
        db.save_settings({"whatsapp_provider": "http", "http_api_url": "http://127.0.0.1:9/x"})
        self.book(hours_ahead=1)
        ok = {"ok": True, "simulated": False, "provider": "http", "detail": "200"}
        worker._screen_lock.acquire()
        try:
            with patch.object(worker, "_SCREEN_LOCK_TIMEOUT", 0.05), patch("worker.whatsapp.send", return_value=ok):
                summary = worker.process_due_reminders()
        finally:
            worker._screen_lock.release()
        self.assertEqual(summary["sent"], 1)

    def test_timestamps_use_send_time_not_tick_start(self):
        appointment_id = self.book(hours_ahead=1)
        old_tick = utils.now_utc() - timedelta(minutes=10)
        worker.process_due_reminders(now=old_tick + timedelta(minutes=10))
        sent_at = utils.parse_iso(self.reminder(appointment_id)["sent_at"])
        self.assertLess(abs((utils.now_utc() - sent_at).total_seconds()), 5)


# ---------------------------------------------------------------------
# Mesaj izni (KVKK / İYS)
# ---------------------------------------------------------------------
class OptOutTests(Base):
    def test_opt_out_skips_pending_and_blocks_all_sends(self):
        appointment_id = self.book(hours_ahead=1)
        customer_id = db.get_appointment(appointment_id)["customer_id"]
        self.assertTrue(db.set_customer_opt_out(customer_id, True))

        reminder = self.reminder(appointment_id)
        self.assertEqual((reminder["status"], reminder["skip_code"]), ("skipped", db.SKIP_OPTED_OUT))
        self.assertEqual(worker.process_due_reminders()["sent"], 0)
        self.assertFalse(worker.send_manual_reminder(appointment_id)["ok"])
        self.assertFalse(worker.send_reminder_now(reminder["id"])["ok"])
        self.assertEqual(db.recent_messages(10), [])

    def test_opt_back_in_reopens_future_reminders(self):
        appointment_id = self.book(hours_ahead=5)
        customer_id = db.get_appointment(appointment_id)["customer_id"]
        db.set_customer_opt_out(customer_id, True)
        db.set_customer_opt_out(customer_id, False)
        self.assertEqual(self.reminder(appointment_id)["status"], "pending")

    def test_reopening_appointment_keeps_opt_out(self):
        appointment_id = self.book(hours_ahead=5)
        customer_id = db.get_appointment(appointment_id)["customer_id"]
        db.set_customer_opt_out(customer_id, True)
        db.set_appointment_status(appointment_id, "cancelled")
        db.set_appointment_status(appointment_id, "scheduled")
        self.assertEqual(self.reminder(appointment_id)["skip_code"], db.SKIP_OPTED_OUT)

    def test_booking_warns_when_customer_opted_out(self):
        appointment_id = self.book(hours_ahead=5)
        db.set_customer_opt_out(db.get_appointment(appointment_id)["customer_id"], True)
        result = booking.create_booking("Ayşe Yılmaz", "05551112233", self.local_input(30))
        self.assertIn("mesaj almak istemiyor", result.message)


# ---------------------------------------------------------------------
# Mesaj metni
# ---------------------------------------------------------------------
class RelativeDayTests(unittest.TestCase):
    def test_relative_day(self):
        now = utils.local_now(TZ).replace(hour=10, minute=0)
        self.assertEqual(worker._relative_day(now.replace(hour=15), now), "bugün")
        self.assertEqual(worker._relative_day(now + timedelta(days=1), now), "yarın")
        later = now + timedelta(days=3)
        self.assertEqual(
            worker._relative_day(later, now), f"{utils.fmt_date(later)} {utils.weekday_tr(later)}"
        )


# ---------------------------------------------------------------------
# Günlük bakım
# ---------------------------------------------------------------------
class MaintenanceTests(Base):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"ESNAF_BACKUP_DIR": str(self._tmp / "yedek")})
        env.start()
        self.addCleanup(env.stop)

    def test_backup_is_consistent_and_rotated(self):
        self.book()
        base = utils.now_utc()
        for day in range(4):
            db.backup_database(self._tmp / "yedek", keep=2, now=base + timedelta(days=day))
        files = sorted((self._tmp / "yedek").glob("esnaf-*.db"))
        self.assertEqual(len(files), 2)
        copy = sqlite3.connect(files[-1])
        self.assertEqual(copy.execute("SELECT COUNT(*) FROM appointments").fetchone()[0], 1)
        copy.close()

    def test_purge_old_messages(self):
        old = utils.now_utc() - timedelta(days=400)
        db.log_message(phone="905551112233", body="eski", kind="test", provider="x", status="sent", created_at=old)
        db.log_message(phone="905551112233", body="yeni", kind="test", provider="x", status="sent")
        self.assertEqual(db.purge_old_messages(365), 1)
        self.assertEqual([m["body"] for m in db.recent_messages(10)], ["yeni"])

    def test_daily_maintenance_runs_once_per_day(self):
        first = scheduler.daily_maintenance()
        self.assertIn("backup", first)
        self.assertIsNone(scheduler.daily_maintenance())
        self.assertEqual(len(list((self._tmp / "yedek").glob("esnaf-*.db"))), 1)

    def test_maintenance_failure_does_not_break_tick(self):
        with patch("db.backup_database", side_effect=OSError("disk dolu")):
            summary = scheduler.run_once()
        self.assertIn("checked", summary)
        self.assertIn("disk dolu", db.get_meta("maintenance.last_error"))


# ---------------------------------------------------------------------
# Kişisel verinin günlüğe yazılmaması
# ---------------------------------------------------------------------
class LogPrivacyTests(unittest.TestCase):
    def test_mask_phone(self):
        self.assertEqual(whatsapp.mask_phone("905551112233"), "+90 555 *** ** 33")

    def test_simulation_log_has_no_phone_or_body(self):
        with self.assertLogs("whatsapp", level=logging.INFO) as logs:
            whatsapp.send("905551112233", "Gizli içerik Ayşe", {"whatsapp_provider": "simulation"})
        text = "\n".join(logs.output)
        self.assertNotIn("905551112233", text)
        self.assertNotIn("Ayşe", text)


# ---------------------------------------------------------------------
# Gizli anahtar
# ---------------------------------------------------------------------
class SecretKeyTests(unittest.TestCase):
    def test_unreadable_key_file_is_not_overwritten(self):
        tmp = make_temp_dir()
        self.addCleanup(remove_temp_dir, tmp)
        key_file = tmp / ".secret_key"
        key_file.write_text("kalici-anahtar", encoding="utf-8")
        with patch.dict(os.environ, {"ESNAF_DATA_DIR": str(tmp)}):
            with patch.object(Path, "read_text", side_effect=OSError("erişim yok")):
                temporary = config.secret_key()
            self.assertNotEqual(temporary, "kalici-anahtar")
            self.assertEqual(key_file.read_text(encoding="utf-8"), "kalici-anahtar")
            self.assertEqual(config.secret_key(), "kalici-anahtar")


if __name__ == "__main__":
    unittest.main()
