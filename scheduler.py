"""Arka plan zamanlayıcısı (scheduler).

Bu dosya, WhatsApp hatırlatmalarını periyodik olarak kontrol eden döngüyü
yönetir. Asıl gönderim/iş mantığı `worker.py` içindedir; burada yalnızca

* sonsuz döngü ve kontrol aralığı,
* tek örnek (single instance) kilidi — aynı veritabanına iki scheduler'ın
  aynı anda bağlanıp kaynak tüketmesini önler,
* heartbeat / son tur özeti (`scheduler.*` meta anahtarları),
* zarif kapanış (Ctrl+C, SIGTERM),
* `python scheduler.py` ile tek başına çalıştırma

vardır.

Kullanım
--------
    python scheduler.py                 # panelden bağımsız sürekli çalışır
    python scheduler.py --once          # bir tur çalıştırıp çıkar (cron)
    python scheduler.py --interval 15   # 15 saniyede bir kontrol
    python scheduler.py --force         # worker_enabled=0 olsa da çalıştır

Panel (`main.py`) açıldığında scheduler otomatik olarak içeride başlatılır.
Panel kapalıyken mesajların gitmeye devam etmesi için bu dosya ayrı bir
süreçte çalıştırılabilir.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import config
import db
import utils
import worker

logger = logging.getLogger("esnaf.scheduler")

_active_scheduler: "Scheduler | None" = None
LOCK_FILENAME = "scheduler.lock"


# ---------------------------------------------------------------------
# Tek örnek kilidi
# ---------------------------------------------------------------------
class SingleInstanceLock:
    """Aynı veritabanı için yalnızca bir scheduler sürecinin çalışmasını sağlar.

    Windows'ta `msvcrt.locking`, POSIX'te `fcntl.flock` kullanır. Kilit dosyası
    veritabanının yanında tutulur (`data/scheduler.lock`). Süreç çökse bile
    işletim sistemi kilidi serbest bırakır; dosyanın kalması sorun değildir.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else (db.get_db_path().parent / LOCK_FILENAME)
        self._handle: Any = None

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path, "a+", encoding="utf-8")
        except OSError:
            logger.exception("Kilit dosyası açılamadı: %s", self.path)
            return False

        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False

        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
        except OSError:
            pass
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            logger.debug("Kilit serbest bırakılırken hata oluştu (önemsiz).")
        finally:
            try:
                handle.close()
            except OSError:
                pass


class SchedulerAlreadyRunning(RuntimeError):
    """Aynı veritabanı için zaten çalışan bir scheduler var."""


# ---------------------------------------------------------------------
# Tek tur çalıştırma
# ---------------------------------------------------------------------
def run_once(
    now: Any = None,
    *,
    respect_enabled: bool = True,
    limit: int = 20,
) -> dict[str, Any]:
    """Zamanı gelen mesajları bir kez işler ve heartbeat yazar."""
    started_at = now or utils.now_utc()
    summary = worker.process_due_reminders(
        now=started_at,
        limit=limit,
        respect_enabled=respect_enabled,
    )
    payload = dict(summary)
    if summary.get("disabled"):
        payload["message"] = "Otomatik gönderim ayarlardan kapalı (worker_enabled=0)."

    db.set_meta("scheduler.last_tick_at", utils.to_iso(started_at))
    db.set_meta("scheduler.last_tick_summary", json.dumps(payload, ensure_ascii=False))
    if not summary.get("disabled"):
        db.set_meta("scheduler.last_error", "")
    daily_maintenance()
    return summary


# ---------------------------------------------------------------------
# Günlük bakım: yedek + eski mesajları silme
# ---------------------------------------------------------------------
def backup_dir() -> Path:
    custom = os.environ.get("ESNAF_BACKUP_DIR")
    return Path(custom).expanduser() if custom else db.get_db_path().parent / "backups"


def daily_maintenance(force: bool = False) -> dict[str, Any] | None:
    """Günde bir kez veritabanını yedekler ve saklama süresi dolan mesajları siler.

    Hata olursa mesaj gönderimini durdurmaz; yalnızca günlüğe ve meta'ya yazar.
    """
    settings = db.get_settings()
    today = utils.local_now(settings.get("timezone") or config.DEFAULT_TIMEZONE).date().isoformat()
    if not force and db.get_meta("maintenance.last_date") == today:
        return None
    result: dict[str, Any] = {"date": today}
    try:
        keep = max(1, db.to_int(settings.get("backup_keep_days"), 14))
        result["backup"] = str(db.backup_database(backup_dir(), keep=keep))
        retention = db.to_int(settings.get("message_retention_days"), 365)
        result["purged_messages"] = db.purge_old_messages(retention)
        db.set_meta("maintenance.last_error", "")
        logger.info(
            "Günlük bakım: yedek alındı (%s), %s eski mesaj silindi.",
            Path(result["backup"]).name,
            result["purged_messages"],
        )
    except Exception as exc:  # bakım hatası gönderimi durdurmasın
        logger.exception("Günlük bakım başarısız")
        db.set_meta("maintenance.last_error", str(exc)[:500])
        result["error"] = str(exc)
    # Hata olsa bile aynı gün her turda yeniden denenmesin (30 sn'de bir disk doldurmasın).
    db.set_meta("maintenance.last_date", today)
    return result


# ---------------------------------------------------------------------
# Thread
# ---------------------------------------------------------------------
class Scheduler(threading.Thread):
    """Periyodik olarak `run_once()` çağıran daemon thread."""

    def __init__(
        self,
        interval: int | None = None,
        *,
        respect_enabled: bool = True,
        lock_path: str | Path | None = None,
    ):
        super().__init__(name="esnaf-scheduler", daemon=True)
        self.interval = max(5, int(interval or config.worker_interval()))
        self.respect_enabled = respect_enabled
        self._stop_event = threading.Event()
        self._lock = SingleInstanceLock(lock_path)
        self._owns_lock = False

    # -- yaşam döngüsü -------------------------------------------------
    def start(self) -> None:  # type: ignore[override]
        if not self._lock.acquire():
            raise SchedulerAlreadyRunning(
                "Aynı veritabanı için zaten çalışan bir scheduler var "
                f"({self._lock.path})."
            )
        self._owns_lock = True
        db.set_meta("scheduler.external", "0")
        try:
            super().start()
        except Exception:
            self._owns_lock = False
            self._lock.release()
            raise

    def run(self) -> None:  # pragma: no cover - thread zamanlaması
        global _active_scheduler
        _active_scheduler = self
        db.set_meta("scheduler.started_at", utils.to_iso(utils.now_utc()))
        db.set_meta("scheduler.pid", str(os.getpid()))
        db.set_meta("scheduler.last_error", "")
        logger.info(
            "Scheduler başladı (kontrol aralığı: %s sn, pid=%s).",
            self.interval,
            os.getpid(),
        )
        try:
            while not self._stop_event.is_set():
                started = time.monotonic()
                try:
                    summary = run_once(respect_enabled=self.respect_enabled)
                    if summary.get("sent") or summary.get("failed"):
                        logger.info(
                            "Tur: %s kontrol, %s gönderim, %s hata, %s atlandı",
                            summary.get("checked", 0),
                            summary.get("sent", 0),
                            summary.get("failed", 0),
                            summary.get("skipped", 0),
                        )
                except Exception:
                    error = traceback.format_exc(limit=4)
                    logger.exception("Scheduler turunda hata")
                    try:
                        db.set_meta("scheduler.last_error", error[-1000:])
                    except Exception:
                        pass
                elapsed = time.monotonic() - started
                self._stop_event.wait(max(1.0, self.interval - elapsed))
        finally:
            try:
                db.set_meta("scheduler.stopped_at", utils.to_iso(utils.now_utc()))
            except Exception:
                pass
            _active_scheduler = None
            self._owns_lock = False
            self._lock.release()
            logger.info("Scheduler durdu.")

    def stop(self) -> None:
        self._stop_event.set()


# ---------------------------------------------------------------------
# Durum bilgisi (arayüz / sağlık ucu için)
# ---------------------------------------------------------------------
def status() -> dict[str, Any]:
    settings = db.get_settings()
    meta = db.get_meta_many(
        (
            "scheduler.last_tick_at",
            "scheduler.last_tick_summary",
            "scheduler.external",
            "scheduler.last_error",
            "maintenance.last_error",
        )
    )
    last_tick_raw = meta.get("scheduler.last_tick_at")
    last_tick = utils.parse_iso(last_tick_raw)
    summary: dict[str, Any] = {}
    try:
        summary = json.loads(meta.get("scheduler.last_tick_summary") or "{}")
    except (TypeError, ValueError):
        summary = {}

    scheduler = _active_scheduler
    running = bool(scheduler is not None and scheduler.is_alive())
    interval = scheduler.interval if scheduler is not None else config.worker_interval()
    stale = False
    if last_tick is not None:
        stale = (utils.now_utc() - last_tick).total_seconds() > max(90, interval * 3)
    return {
        "enabled": str(settings.get("worker_enabled", "1")) == "1",
        "running": running,
        "stale": stale,
        "external": meta.get("scheduler.external") == "1",
        "error": meta.get("scheduler.last_error") or "",
        "maintenance_error": meta.get("maintenance.last_error") or "",
        "last_tick_at": last_tick,
        "last_tick_raw": last_tick_raw,
        "summary": summary,
        "interval": interval,
        "provider": settings.get("whatsapp_provider") or "simulation",
        "pending": db.pending_reminder_counts(),
    }


# ---------------------------------------------------------------------
# Komut satırı
# ---------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scheduler.py",
        description="WhatsApp hatırlatmalarını arka planda periyodik olarak kontrol eder.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=config.worker_interval(),
        help="Kontrol aralığı, saniye (en az 5; varsayılan 30)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Bir tur çalıştırıp çıkar (Windows Görev Zamanlayıcı / cron için)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ayarlardaki 'otomatik gönderim kapalı' anahtarını yok say",
    )
    parser.add_argument("--debug", action="store_true", help="Ayrıntılı günlük kaydı")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config.load_dotenv()
    config.ensure_dirs()
    config.setup_logging(logging.DEBUG if args.debug else logging.INFO)
    db.init_db()

    if args.once:
        summary = run_once(respect_enabled=not args.force)
        if summary.get("disabled"):
            print(
                "  • Otomatik gönderim ayarlardan kapalı. Zorlamak için --force kullanın.",
                flush=True,
            )
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        return 0

    scheduler = Scheduler(interval=args.interval, respect_enabled=not args.force)
    try:
        scheduler.start()
    except SchedulerAlreadyRunning as exc:
        print(f"\n  • {exc}\n    Aynı anda yalnızca bir scheduler çalıştırın.\n", flush=True)
        return 3

    provider = db.get_setting("whatsapp_provider") or "simulation"
    print(
        "\n  ESNAF RANDEVU ASİSTANI — ZAMANLAYICI\n"
        f"    Aralık      : {scheduler.interval} sn\n"
        f"    WhatsApp    : {provider}"
        + (" (gerçek gönderim yok)" if provider == "simulation" else "")
        + "\n"
        f"    Veritabanı  : {db.get_db_path()}\n"
        f"    Kilit       : {scheduler._lock.path}\n"
        "\n    Durdurmak için: Ctrl+C\n",
        flush=True,
    )

    def _shutdown(signum, _frame):  # pragma: no cover - sinyal
        logger.info("Sinyal alındı (%s); scheduler durduruluyor.", signum)
        scheduler.stop()

    for signal_name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, signal_name, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, _shutdown)
        except (ValueError, OSError):
            pass

    try:
        while scheduler.is_alive():
            scheduler.join(timeout=0.5)
    except KeyboardInterrupt:  # pragma: no cover - elle durdurma
        pass
    finally:
        scheduler.stop()
        scheduler.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
