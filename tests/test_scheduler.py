"""Scheduler testleri: tek tur, heartbeat, tekil örnek kilidi, thread döngüsü."""

from __future__ import annotations

import time
import unittest
from datetime import timedelta

import db
import scheduler
import utils
from tests.support import make_temp_dir, remove_temp_dir


class SchedulerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = make_temp_dir()
        self.db_path = self._tmp / "scheduler.db"
        db.init_db(self.db_path)
        db.save_settings(
            {
                "whatsapp_provider": "simulation",
                "google_review_url": "https://g.page/r/test/review",
                "worker_enabled": "1",
                "max_send_attempts": "2",
                "retry_delay_minutes": "1",
            }
        )
        self._schedulers: list[scheduler.Scheduler] = []

    def tearDown(self):
        for item in self._schedulers:
            item.stop()
            if item.is_alive():
                item.join(timeout=5)
        scheduler._active_scheduler = None
        db._db_path = None
        remove_temp_dir(self._tmp)

    def create_due_appointment(self):
        customer_id = db.upsert_customer("Ayşe Yılmaz", "905551112233")
        # Randevu 90 dk sonra: onay mesajının vakti (2 saat önce) geçmiş olur.
        start = utils.now_utc() + timedelta(minutes=90)
        return db.create_appointment(customer_id, start, 60, "Saç Kesimi")

    # --- tek tur -------------------------------------------------------
    def test_run_once_sends_and_writes_heartbeat(self):
        self.create_due_appointment()
        summary = scheduler.run_once()
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(len(db.recent_messages(5)), 1)

        state = scheduler.status()
        self.assertIsNotNone(state["last_tick_raw"])
        self.assertEqual(state["last_tick_at"].tzinfo is not None, True)
        self.assertEqual(state["summary"].get("sent"), 1)
        self.assertEqual(state["error"], "")
        self.assertFalse(state["external"])

    def test_run_once_respects_disabled_setting(self):
        db.save_settings({"worker_enabled": "0"})
        self.create_due_appointment()
        summary = scheduler.run_once(respect_enabled=True)
        self.assertTrue(summary["disabled"])
        self.assertEqual(len(db.recent_messages(5)), 0)
        # Heartbeat yine yazılmalı (panel 'son kontrol' gösterebilsin)
        self.assertIsNotNone(scheduler.status()["last_tick_raw"])

    def test_force_run_even_if_disabled(self):
        db.save_settings({"worker_enabled": "0"})
        self.create_due_appointment()
        summary = scheduler.run_once(respect_enabled=False)
        self.assertFalse(summary["disabled"])
        self.assertEqual(summary["sent"], 1)

    # --- kilit ---------------------------------------------------------
    def test_single_instance_lock(self):
        lock_path = self._tmp / "test.lock"
        first = scheduler.SingleInstanceLock(lock_path)
        second = scheduler.SingleInstanceLock(lock_path)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.release()
        self.assertTrue(second.acquire())
        second.release()

    def test_scheduler_start_raises_when_locked(self):
        lock_path = self.db_path.parent / scheduler.LOCK_FILENAME
        blocker = scheduler.SingleInstanceLock(lock_path)
        self.assertTrue(blocker.acquire())
        try:
            instance = scheduler.Scheduler(interval=5)
            self._schedulers.append(instance)
            with self.assertRaises(scheduler.SchedulerAlreadyRunning):
                instance.start()
        finally:
            blocker.release()

    # --- thread döngüsü ------------------------------------------------
    def test_scheduler_thread_runs_and_stops(self):
        self.create_due_appointment()
        instance = scheduler.Scheduler(interval=5)
        self._schedulers.append(instance)
        instance.start()

        deadline = time.time() + 8
        while time.time() < deadline and not db.recent_messages(5):
            time.sleep(0.1)
        self.assertGreaterEqual(len(db.recent_messages(5)), 1)

        instance.stop()
        instance.join(timeout=5)
        self.assertFalse(instance.is_alive())
        self.assertIsNone(scheduler._active_scheduler)
        self.assertFalse(scheduler.status()["running"])


if __name__ == "__main__":
    unittest.main()
