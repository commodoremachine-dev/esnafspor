"""SQLite veritabanı katmanı.

Tablolar
--------
customers     : müşteriler (telefon benzersiz, normalize edilmiş, mesaj izni)
appointments  : randevular (başlangıç UTC, süre, durum)
reminders     : randevu başına 2 otomatik mesaj işi (onay + yorum)
messages      : gönderilen mesajların günlüğü (hata/başarı dahil)
settings      : tek kullanıcılı panel ayarları (anahtar/değer)
meta          : motor durumu gibi çalışma zamanı bilgileri

Tüm zamanlar UTC ISO-8601 metni olarak saklanır; görüntüleme katmanı
(`utils.to_tz`) işletmenin saat dilimine çevirir.

Birden fazla tabloya dokunan işlemler (randevu + hatırlatmaları gibi) tek bir
bağlantı ve tek işlem (transaction) içinde yapılır; yarıda kalırsa hepsi geri
alınır.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

import config
import utils

# ---------------------------------------------------------------------
# Şema
# ---------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    phone       TEXT    NOT NULL UNIQUE,          -- 90XXXXXXXXXX
    notes       TEXT    NOT NULL DEFAULT '',
    opted_out   INTEGER NOT NULL DEFAULT 0,       -- 1: mesaj almak istemiyor (KVKK/İYS)
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS appointments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id       INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    starts_at         TEXT    NOT NULL,           -- UTC ISO-8601
    duration_minutes  INTEGER NOT NULL DEFAULT 60,
    service           TEXT    NOT NULL DEFAULT '',
    notes             TEXT    NOT NULL DEFAULT '',
    status            TEXT    NOT NULL DEFAULT 'scheduled'
                      CHECK (status IN ('scheduled','confirmed','completed','cancelled','no_show')),
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_appointments_starts_at ON appointments(starts_at);
CREATE INDEX IF NOT EXISTS idx_appointments_customer  ON appointments(customer_id);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id  INTEGER REFERENCES appointments(id) ON DELETE SET NULL,
    customer_id     INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    kind            TEXT    NOT NULL,             -- confirmation | review | manual | test
    phone           TEXT    NOT NULL,
    body            TEXT    NOT NULL,
    provider        TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'sent',   -- sent | simulated | failed
    simulated       INTEGER NOT NULL DEFAULT 0,
    error           TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_messages_appointment ON messages(appointment_id, kind);

CREATE TABLE IF NOT EXISTS reminders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id   INTEGER NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    kind             TEXT    NOT NULL CHECK (kind IN ('confirmation','review')),
    due_at           TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','sent','failed','skipped')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  TEXT,                        -- başarısız denemeden sonraki deneme zamanı
    lease_until      TEXT,                        -- bir süreç şu an gönderiyorsa kira bitişi
    last_attempt_at  TEXT,
    sent_at          TEXT,
    error            TEXT    NOT NULL DEFAULT '',
    skip_code        TEXT    NOT NULL DEFAULT '', -- atlama nedeni (makine okunur)
    message_id       INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    created_at       TEXT    NOT NULL,
    UNIQUE (appointment_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_at);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Eski veritabanlarına sonradan eklenen sütunlar (tablo, sütun, tanım).
_MIGRATIONS = (
    ("customers", "opted_out", "INTEGER NOT NULL DEFAULT 0"),
    ("reminders", "lease_until", "TEXT"),
    ("reminders", "skip_code", "TEXT NOT NULL DEFAULT ''"),
)

# ---------------------------------------------------------------------
# Varsayılan ayarlar
# ---------------------------------------------------------------------
# Eski sürümün varsayılan onay metni ("bugün" her zaman doğru değildi).
_OLD_CONFIRMATION_DEFAULT = "Merhaba {isim}, bugün saat {saat} randevunuzu onaylıyor musunuz?"

SETTINGS_DEFAULTS: dict[str, str] = {
    "business_name": "Esnaf Kuaför",
    "google_review_url": "",
    "timezone": config.DEFAULT_TIMEZONE,
    "confirmation_lead_hours": "2",       # randevudan kaç saat önce onay mesajı
    "review_delay_hours": "1",             # randevu bitiminden kaç saat sonra yorum mesajı
    "default_duration_minutes": "60",
    "whatsapp_provider": "simulation",     # simulation | pywhatkit | http
    "http_api_url": "",
    "http_api_token": "",
    "http_mode": "generic",                # generic | waha
    "http_session": "default",
    "msg_confirmation": (
        "Merhaba {isim}, {ne_zaman} saat {saat} randevunuzu onaylıyor musunuz?"
    ),
    "msg_review": (
        "Merhaba {isim}, hizmetimizi değerlendirmek için 1 dakikanızı ayırır mısınız? "
        "Google Yorum Linki: {link}"
    ),
    "msg_manual": (
        "Merhaba {isim}, {ne_zaman} saat {saat} randevunuzu unutmayın. Görüşmek üzere! — {isletme}"
    ),
    "worker_enabled": "1",
    "max_send_attempts": "3",
    "retry_delay_minutes": "5",
    "message_retention_days": "365",       # mesaj günlüğü kaç gün saklanır (KVKK)
    "backup_keep_days": "14",              # günlük yedeklerden kaç tanesi tutulur
}

APPOINTMENT_STATUSES = ("scheduled", "confirmed", "completed", "cancelled", "no_show")
INACTIVE_STATUSES = ("cancelled", "no_show")
REMINDER_KINDS = ("confirmation", "review")
_INACTIVE_SQL = "('cancelled','no_show')"

# Atlama kodları (iş mantığı metne değil bu kodlara bakar).
SKIP_INACTIVE = "inactive"
SKIP_ALREADY_CONFIRMED = "already_confirmed"
SKIP_TOO_LATE = "too_late"
SKIP_NO_REVIEW_LINK = "no_review_link"
SKIP_OPTED_OUT = "opted_out"
SKIP_BAD_DATA = "bad_data"

_db_path: Path | None = None


# ---------------------------------------------------------------------
# Bağlantı yönetimi
# ---------------------------------------------------------------------
def configure(path: str | Path | None = None) -> Path:
    """Veritabanı dosyasını ayarlar (testler ve `ESNAF_DB` için)."""
    global _db_path
    _db_path = Path(path).expanduser() if path else config.db_path()
    return _db_path


def get_db_path() -> Path:
    global _db_path
    if _db_path is None:
        _db_path = config.db_path()
    return _db_path


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    """Kısa ömürlü, thread-safe SQLite bağlantısı; blok sonunda commit/rollback."""
    conn = sqlite3.connect(str(get_db_path()), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, definition in _MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    # Eski sürümde atlama nedeni yalnızca metin olarak tutuluyordu: koda çevir.
    for code, pattern in (
        (SKIP_NO_REVIEW_LINK, "Google Yorum Linki%"),
        (SKIP_INACTIVE, "Randevu iptal%"),
        (SKIP_ALREADY_CONFIRMED, "Randevu panelden onaylandı%"),
    ):
        conn.execute(
            "UPDATE reminders SET skip_code = ? WHERE status = 'skipped' AND skip_code = '' AND error LIKE ?",
            (code, pattern),
        )
    # Eski varsayılan onay metni değiştirilmemişse yenisine geçir.
    conn.execute(
        "UPDATE settings SET value = ? WHERE key = 'msg_confirmation' AND value = ?",
        (SETTINGS_DEFAULTS["msg_confirmation"], _OLD_CONFIRMATION_DEFAULT),
    )


def init_db(path: str | Path | None = None) -> Path:
    """Şemayı oluşturur/günceller, eksik ayarları varsayılanlarla doldurur."""
    if path is not None or _db_path is None:
        configure(path)
    get_db_path().parent.mkdir(parents=True, exist_ok=True)
    with connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.executemany(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            list(SETTINGS_DEFAULTS.items()),
        )
    return get_db_path()


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _iso(value: datetime) -> str:
    return utils.to_iso(value)


def _now_iso() -> str:
    return _iso(utils.now_utc())


# ---------------------------------------------------------------------
# Ayarlar
# ---------------------------------------------------------------------
def _settings(conn: sqlite3.Connection) -> dict[str, str]:
    merged = dict(SETTINGS_DEFAULTS)
    for row in conn.execute("SELECT key, value FROM settings"):
        merged[row["key"]] = row["value"]
    return merged


def get_settings() -> dict[str, str]:
    """Tüm ayarları varsayılanlarla birleşmiş halde döndürür."""
    with connection() as conn:
        return _settings(conn)


def get_setting(key: str, default: str | None = None) -> str | None:
    with connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return row["value"]
    return SETTINGS_DEFAULTS.get(key, default)


def save_settings(values: dict[str, Any]) -> None:
    """Verilen ayarları tek işlemde kaydeder (yalnızca metin değerler)."""
    now = _now_iso()
    with connection() as conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(key, "" if value is None else str(value)) for key, value in values.items()],
        )
        _set_meta(conn, "settings.updated_at", now, now)


def to_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_float_setting(key: str, default: float) -> float:
    return to_float(get_setting(key) or default, default)


def get_int_setting(key: str, default: int) -> int:
    return to_int(get_setting(key) or default, default)


# ---------------------------------------------------------------------
# Çalışma zamanı meta bilgileri
# ---------------------------------------------------------------------
def _set_meta(conn: sqlite3.Connection, key: str, value: str, now: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, str(value), now),
    )


def set_meta(key: str, value: str) -> None:
    with connection() as conn:
        _set_meta(conn, key, value, _now_iso())


def get_meta(key: str, default: str | None = None) -> str | None:
    with connection() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def get_meta_many(keys: Iterable[str]) -> dict[str, str]:
    """Birden çok meta anahtarını tek sorguda okur (bulunmayanlar dönmez)."""
    key_list = list(keys)
    if not key_list:
        return {}
    placeholders = ",".join("?" for _ in key_list)
    with connection() as conn:
        rows = conn.execute(
            f"SELECT key, value FROM meta WHERE key IN ({placeholders})", key_list
        ).fetchall()
    return {row["key"]: row["value"] for row in rows}


# ---------------------------------------------------------------------
# Müşteriler
# ---------------------------------------------------------------------
def find_customer_by_phone(phone: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM customers WHERE phone = ?", (phone,)).fetchone()
    return _dict(row)


def _upsert_customer(
    conn: sqlite3.Connection, name: str, phone: str, notes: str, update_name: bool
) -> int:
    now = _now_iso()
    row = conn.execute("SELECT id, notes FROM customers WHERE phone = ?", (phone,)).fetchone()
    if row is not None:
        merged_notes = notes.strip() or row["notes"]
        if update_name:
            conn.execute(
                "UPDATE customers SET name = ?, notes = ?, updated_at = ? WHERE id = ?",
                (name.strip(), merged_notes, now, row["id"]),
            )
        elif merged_notes != row["notes"]:
            conn.execute(
                "UPDATE customers SET notes = ?, updated_at = ? WHERE id = ?",
                (merged_notes, now, row["id"]),
            )
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO customers (name, phone, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (name.strip(), phone, notes.strip(), now, now),
    )
    return int(cursor.lastrowid)


def upsert_customer(name: str, phone: str, notes: str = "", *, update_name: bool = True) -> int:
    """Aynı telefon varsa müşteriyi günceller, yoksa oluşturur.

    `update_name=False` ise kayıtlı müşterinin adı değiştirilmez.
    Boş not, kayıtlı notu silmez.
    """
    with connection() as conn:
        return _upsert_customer(conn, name, phone, notes, update_name)


def get_customer(customer_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return _dict(row)


def set_customer_opt_out(customer_id: int, opted_out: bool) -> bool:
    """Müşterinin mesaj almak istemediğini kaydeder; bekleyen mesajları atlar."""
    with connection() as conn:
        cursor = conn.execute(
            "UPDATE customers SET opted_out = ?, updated_at = ? WHERE id = ?",
            (1 if opted_out else 0, _now_iso(), int(customer_id)),
        )
        if cursor.rowcount == 0:
            return False
        if opted_out:
            conn.execute(
                "UPDATE reminders SET status = 'skipped', skip_code = ?, error = ?, lease_until = NULL "
                "WHERE status IN ('pending','failed') AND (lease_until IS NULL OR lease_until <= ?) "
                "AND appointment_id IN (SELECT id FROM appointments WHERE customer_id = ?)",
                (SKIP_OPTED_OUT, "Müşteri mesaj almak istemiyor.", _now_iso(), int(customer_id)),
            )
        else:
            conn.execute(
                "UPDATE reminders SET status = 'pending', attempts = 0, next_attempt_at = NULL, "
                "last_attempt_at = NULL, error = '', skip_code = '' "
                "WHERE status = 'skipped' AND skip_code = ? AND due_at > ? "
                "AND appointment_id IN (SELECT id FROM appointments WHERE customer_id = ?)",
                (SKIP_OPTED_OUT, _now_iso(), int(customer_id)),
            )
    return True


# ---------------------------------------------------------------------
# Randevular
# ---------------------------------------------------------------------
_APPOINTMENT_SELECT = """
SELECT a.*,
       c.name      AS customer_name,
       c.phone     AS customer_phone,
       c.notes     AS customer_notes,
       c.opted_out AS customer_opted_out
FROM appointments a
JOIN customers c ON c.id = a.customer_id
"""


def _get_appointment(conn: sqlite3.Connection, appointment_id: int) -> dict[str, Any] | None:
    row = conn.execute(_APPOINTMENT_SELECT + " WHERE a.id = ?", (appointment_id,)).fetchone()
    return _dict(row)


def get_appointment(appointment_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        return _get_appointment(conn, appointment_id)


def list_appointments(
    *,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
    include_cancelled: bool = False,
    limit: int = 100,
    order: str = "asc",
) -> list[dict[str, Any]]:
    """Randevuları (müşteri bilgisiyle) listeler."""
    clauses: list[str] = []
    params: list[Any] = []
    if start_utc is not None:
        clauses.append("a.starts_at >= ?")
        params.append(_iso(start_utc))
    if end_utc is not None:
        clauses.append("a.starts_at < ?")
        params.append(_iso(end_utc))
    if not include_cancelled:
        clauses.append(f"a.status NOT IN {_INACTIVE_SQL}")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    direction = "DESC" if str(order).lower() == "desc" else "ASC"
    params.append(max(1, min(int(limit), 501)))
    with connection() as conn:
        rows = conn.execute(
            f"{_APPOINTMENT_SELECT}{where} ORDER BY a.starts_at {direction} LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def _find_conflict(
    conn: sqlite3.Connection,
    customer_id: int,
    starts_at: datetime,
    duration_minutes: int,
    exclude_id: int | None = None,
) -> dict[str, Any] | None:
    new_start = utils.to_utc(starts_at)
    new_end = new_start + timedelta(minutes=max(1, duration_minutes))
    query = _APPOINTMENT_SELECT + (
        f" WHERE a.customer_id = ? AND a.status NOT IN {_INACTIVE_SQL}"
        " AND a.starts_at >= ? AND a.starts_at < ?"
    )
    params: list[Any] = [customer_id, _iso(new_start - timedelta(days=1)), _iso(new_end)]
    if exclude_id is not None:
        query += " AND a.id != ?"
        params.append(int(exclude_id))
    for row in conn.execute(query, params).fetchall():
        existing_start = utils.parse_iso(row["starts_at"])
        if existing_start is None:
            continue
        existing_end = existing_start + timedelta(minutes=int(row["duration_minutes"] or 60))
        if existing_start < new_end and existing_end > new_start:
            return dict(row)
    return None


def find_conflict(
    customer_id: int,
    starts_at: datetime,
    duration_minutes: int,
    exclude_id: int | None = None,
) -> dict[str, Any] | None:
    """Aynı müşterinin çakışan randevusu var mı? Varsa onu döndürür."""
    with connection() as conn:
        return _find_conflict(conn, customer_id, starts_at, duration_minutes, exclude_id)


def _insert_appointment(
    conn: sqlite3.Connection,
    customer_id: int,
    starts_at: datetime,
    duration_minutes: int,
    service: str,
    notes: str,
) -> int:
    now = _now_iso()
    cursor = conn.execute(
        "INSERT INTO appointments "
        "(customer_id, starts_at, duration_minutes, service, notes, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?)",
        (int(customer_id), _iso(starts_at), int(duration_minutes), service.strip(), notes.strip(), now, now),
    )
    appointment_id = int(cursor.lastrowid)
    _sync_reminders(conn, appointment_id, _settings(conn))
    return appointment_id


def create_appointment(
    customer_id: int,
    starts_at: datetime,
    duration_minutes: int = 60,
    service: str = "",
    notes: str = "",
) -> int:
    """Randevuyu ekler ve iki hatırlatma işini aynı işlemde planlar."""
    with connection() as conn:
        return _insert_appointment(conn, customer_id, starts_at, duration_minutes, service, notes)


def book_appointment(
    *,
    name: str,
    phone: str,
    starts_at: datetime,
    duration_minutes: int,
    service: str = "",
    notes: str = "",
) -> tuple[int | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Çakışma kontrolü + müşteri + randevu + hatırlatmalar tek işlemde.

    Returns:
        ``(appointment_id, conflict, existing_customer)`` — çakışma varsa
        `appointment_id` None olur ve hiçbir şey yazılmaz.
    """
    with connection() as conn:
        # Yazma kilidini en başta al: kontrol ile kayıt arasında başka bir
        # istek aynı müşteriye çakışan randevu ekleyemesin.
        conn.execute("BEGIN IMMEDIATE")
        existing = _dict(conn.execute("SELECT * FROM customers WHERE phone = ?", (phone,)).fetchone())
        if existing is not None:
            conflict = _find_conflict(conn, int(existing["id"]), starts_at, duration_minutes)
            if conflict is not None:
                return None, conflict, existing
        customer_id = _upsert_customer(conn, name, phone, "", update_name=False)
        appointment_id = _insert_appointment(conn, customer_id, starts_at, duration_minutes, service, notes)
        return appointment_id, None, existing


def update_appointment(
    appointment_id: int,
    *,
    starts_at: datetime | None = None,
    duration_minutes: int | None = None,
    service: str | None = None,
    notes: str | None = None,
) -> bool:
    """Randevuyu günceller; saat/süre değiştiyse hatırlatmaları yeniden planlar."""
    with connection() as conn:
        current = _get_appointment(conn, appointment_id)
        if current is None:
            return False
        new_start = starts_at if starts_at is not None else utils.parse_iso(current["starts_at"])
        if new_start is None:
            return False
        new_duration = int(duration_minutes) if duration_minutes is not None else int(current["duration_minutes"])
        new_service = service.strip() if service is not None else current["service"]
        new_notes = notes.strip() if notes is not None else current["notes"]
        conn.execute(
            "UPDATE appointments SET starts_at = ?, duration_minutes = ?, service = ?, "
            "notes = ?, updated_at = ? WHERE id = ?",
            (_iso(new_start), new_duration, new_service, new_notes, _now_iso(), appointment_id),
        )
        if _iso(new_start) != current["starts_at"] or new_duration != int(current["duration_minutes"]):
            _sync_reminders(conn, appointment_id, _settings(conn))
    return True


def set_appointment_status(appointment_id: int, status: str) -> bool:
    """Durumu günceller ve buna uygun hatırlatmaları aynı işlemde atlar/açar."""
    if status not in APPOINTMENT_STATUSES:
        raise ValueError(f"Geçersiz randevu durumu: {status}")
    with connection() as conn:
        cursor = conn.execute(
            "UPDATE appointments SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now_iso(), appointment_id),
        )
        if cursor.rowcount == 0:
            return False
        if status in INACTIVE_STATUSES:
            _skip_reminders(
                conn, appointment_id, None, SKIP_INACTIVE, "Randevu iptal edildi / müşteri gelmedi"
            )
        elif status in ("confirmed", "completed"):
            _skip_reminders(
                conn,
                appointment_id,
                ("confirmation",),
                SKIP_ALREADY_CONFIRMED,
                "Randevu panelden onaylandı, onay mesajı gerekmiyor",
            )
        elif status == "scheduled":
            # İptal edilmiş bir randevu yeniden açıldığında gelecekteki işleri geri al.
            _reopen_reminders(conn, appointment_id)
    return True


def _reopen_reminders(conn: sqlite3.Connection, appointment_id: int) -> int:
    cursor = conn.execute(
        "UPDATE reminders SET status = 'pending', attempts = 0, next_attempt_at = NULL, "
        "last_attempt_at = NULL, error = '', skip_code = '' "
        "WHERE appointment_id = ? AND status = 'skipped' AND skip_code IN (?, ?) AND due_at > ?",
        (appointment_id, SKIP_INACTIVE, SKIP_ALREADY_CONFIRMED, _now_iso()),
    )
    return cursor.rowcount


def reopen_reminders(appointment_id: int) -> int:
    """İptal/onay nedeniyle atlanmış, gelecekteki hatırlatmaları yeniden beklemeye alır."""
    with connection() as conn:
        return _reopen_reminders(conn, appointment_id)


def delete_appointment(appointment_id: int) -> bool:
    with connection() as conn:
        cursor = conn.execute("DELETE FROM appointments WHERE id = ?", (appointment_id,))
        return cursor.rowcount > 0


# ---------------------------------------------------------------------
# Hatırlatmalar (otomatik mesaj işleri)
# ---------------------------------------------------------------------
def calculate_due_times(
    starts_at: datetime, duration_minutes: int, settings: dict[str, str] | None = None
) -> dict[str, datetime]:
    """Onay ve yorum mesajlarının UTC gönderim zamanlarını hesaplar."""
    settings = settings or get_settings()
    lead_hours = _clamp_hours(settings.get("confirmation_lead_hours"), 2.0)
    review_delay_hours = _clamp_hours(settings.get("review_delay_hours"), 1.0)
    start = utils.to_utc(starts_at)
    end = start + timedelta(minutes=max(1, int(duration_minutes or 60)))
    return {
        "confirmation": start - timedelta(hours=lead_hours),
        "review": end + timedelta(hours=review_delay_hours),
    }


def _clamp_hours(value: Any, default: float) -> float:
    return max(0.0, min(to_float(value, default), 720.0))


def _sync_reminders(conn: sqlite3.Connection, appointment_id: int, settings: dict[str, str]) -> None:
    """Randevu için hatırlatma kayıtlarını oluşturur/günceller.

    Gönderilmiş (sent) veya atlanmış (skipped) işler korunur. Bekleyen/başarısız
    bir iş yalnızca gönderim saati gerçekten değiştiyse sıfırlanır; böylece
    ayarları kaydetmek kalıcı olarak başarısız olmuş mesajları yeniden kuyruğa
    sokmaz. Şu anda gönderilmekte olan (kirası süren) işlere dokunulmaz.
    """
    row = conn.execute(
        "SELECT starts_at, duration_minutes FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    if row is None:
        return
    starts_at = utils.parse_iso(row["starts_at"])
    if starts_at is None:
        return
    due_times = calculate_due_times(starts_at, int(row["duration_minutes"]), settings)
    now = _now_iso()
    existing = {
        item["kind"]: item
        for item in conn.execute(
            "SELECT id, kind, due_at FROM reminders WHERE appointment_id = ?", (appointment_id,)
        )
    }
    for kind, due_at in due_times.items():
        due_iso = _iso(due_at)
        current = existing.get(kind)
        if current is None:
            conn.execute(
                "INSERT INTO reminders (appointment_id, kind, due_at, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (appointment_id, kind, due_iso, now),
            )
            continue
        if current["due_at"] == due_iso:
            continue
        conn.execute(
            "UPDATE reminders SET status = 'pending', attempts = 0, next_attempt_at = NULL, "
            "last_attempt_at = NULL, error = '' "
            "WHERE id = ? AND status IN ('pending','failed') "
            "AND (lease_until IS NULL OR lease_until <= ?)",
            (current["id"], now),
        )
        conn.execute("UPDATE reminders SET due_at = ? WHERE id = ?", (due_iso, current["id"]))


def sync_reminders(appointment_id: int) -> None:
    with connection() as conn:
        _sync_reminders(conn, appointment_id, _settings(conn))


def resync_all_reminders(now: datetime | None = None) -> int:
    """Yaklaşan tüm randevuların hatırlatma saatlerini tek işlemde yeniden hesaplar."""
    now = now or utils.now_utc()
    with connection() as conn:
        settings = _settings(conn)
        ids = [
            int(row["id"])
            for row in conn.execute(
                f"SELECT id FROM appointments WHERE status NOT IN {_INACTIVE_SQL} AND starts_at >= ?",
                (_iso(now - timedelta(hours=12)),),
            )
        ]
        for appointment_id in ids:
            _sync_reminders(conn, appointment_id, settings)
    return len(ids)


def _skip_reminders(
    conn: sqlite3.Connection,
    appointment_id: int,
    kinds: Iterable[str] | None,
    code: str,
    reason: str,
) -> int:
    kind_list = list(kinds) if kinds else list(REMINDER_KINDS)
    placeholders = ",".join("?" for _ in kind_list)
    cursor = conn.execute(
        f"UPDATE reminders SET status = 'skipped', skip_code = ?, error = ? "
        f"WHERE appointment_id = ? AND status IN ('pending','failed') "
        f"AND (lease_until IS NULL OR lease_until <= ?) AND kind IN ({placeholders})",
        [code, reason, appointment_id, _now_iso(), *kind_list],
    )
    return cursor.rowcount


def skip_reminders(
    appointment_id: int,
    *,
    kinds: Iterable[str] | None = None,
    reason: str = "",
    code: str = SKIP_INACTIVE,
) -> int:
    """Bekleyen hatırlatmaları 'skipped' durumuna alır."""
    with connection() as conn:
        return _skip_reminders(conn, appointment_id, kinds, code, reason)


def reset_reminder(reminder_id: int) -> bool:
    """Manuel gönderim öncesi işi sıfırlar. Şu an gönderiliyorsa dokunmaz (False)."""
    with connection() as conn:
        cursor = conn.execute(
            "UPDATE reminders SET status = 'pending', attempts = 0, error = '', skip_code = '', "
            "next_attempt_at = NULL, last_attempt_at = NULL "
            "WHERE id = ? AND status != 'sent' AND (lease_until IS NULL OR lease_until <= ?)",
            (reminder_id, _now_iso()),
        )
        return cursor.rowcount > 0


def claim_reminder(
    reminder_id: int, now: datetime | None = None, lease_minutes: int = 5
) -> bool:
    """Bir hatırlatmayı göndermek üzere atomik olarak sahiplenir.

    `attempts` bir artırılır ve `lease_until` kısa bir süre ileri alınır.
    Böylece aynı hatırlatmayı iki süreç (ör. panel motoru + cron) aynı anda
    göndermeye çalışsa bile yalnızca biri devam eder. Süreç gönderim ortasında
    çökerse kira süresi dolunca iş yeniden denenebilir.
    """
    now = now or utils.now_utc()
    now_iso = _iso(now)
    lease_until = _iso(now + timedelta(minutes=max(1, int(lease_minutes))))
    with connection() as conn:
        cursor = conn.execute(
            "UPDATE reminders SET attempts = attempts + 1, last_attempt_at = ?, lease_until = ? "
            "WHERE id = ? AND status = 'pending' "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "AND (lease_until IS NULL OR lease_until <= ?)",
            (now_iso, lease_until, reminder_id, now_iso, now_iso),
        )
        return cursor.rowcount == 1


def release_claim(reminder_id: int) -> None:
    """Gönderim hiç denenmeden bırakılan bir sahiplenmeyi geri alır (deneme hakkı yanmaz)."""
    with connection() as conn:
        conn.execute(
            "UPDATE reminders SET lease_until = NULL, attempts = MAX(attempts - 1, 0) "
            "WHERE id = ? AND status = 'pending'",
            (reminder_id,),
        )


def reopen_skipped_reviews(now: datetime | None = None) -> int:
    """Google yorum linki sonradan eklendiğinde, link yüzünden atlanan yorum mesajlarını açar."""
    now = now or utils.now_utc()
    with connection() as conn:
        cursor = conn.execute(
            "UPDATE reminders SET status = 'pending', attempts = 0, next_attempt_at = NULL, "
            "last_attempt_at = NULL, error = '', skip_code = '' "
            "WHERE kind = 'review' AND status = 'skipped' AND skip_code = ? AND due_at > ?",
            (SKIP_NO_REVIEW_LINK, _iso(now - timedelta(days=7))),
        )
        return cursor.rowcount


_REMINDER_SELECT = (
    "SELECT r.*, a.starts_at, a.duration_minutes, a.status AS appointment_status, "
    "a.service, a.customer_id, c.name AS customer_name, c.phone AS customer_phone, "
    "c.opted_out AS customer_opted_out "
    "FROM reminders r "
    "JOIN appointments a ON a.id = r.appointment_id "
    "JOIN customers c ON c.id = a.customer_id "
)


def get_reminder(reminder_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        return _dict(conn.execute(_REMINDER_SELECT + "WHERE r.id = ?", (reminder_id,)).fetchone())


def get_reminders_for(appointment_ids: Iterable[int]) -> dict[int, dict[str, dict[str, Any]]]:
    """Randevu id -> {kind: hatırlatma} sözlüğü döndürür (arayüz için)."""
    ids = [int(item) for item in appointment_ids]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM reminders WHERE appointment_id IN ({placeholders})", ids
        ).fetchall()
    result: dict[int, dict[str, dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        result.setdefault(int(item["appointment_id"]), {})[item["kind"]] = item
    return result


def due_reminders(now: datetime | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Gönderim zamanı gelmiş, beklemede ve sahiplenilmemiş işleri getirir."""
    now_iso = _iso(now or utils.now_utc())
    with connection() as conn:
        rows = conn.execute(
            _REMINDER_SELECT
            + "WHERE r.status = 'pending' AND r.due_at <= ? "
            "AND (r.next_attempt_at IS NULL OR r.next_attempt_at <= ?) "
            "AND (r.lease_until IS NULL OR r.lease_until <= ?) "
            f"AND a.status NOT IN {_INACTIVE_SQL} "
            "ORDER BY r.due_at ASC LIMIT ?",
            (now_iso, now_iso, now_iso, max(1, min(int(limit), 200))),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_reminder_sent(reminder_id: int, message_id: int, sent_at: datetime | None = None) -> None:
    sent_iso = _iso(sent_at or utils.now_utc())
    with connection() as conn:
        conn.execute(
            "UPDATE reminders SET status = 'sent', sent_at = ?, message_id = ?, last_attempt_at = ?, "
            "next_attempt_at = NULL, lease_until = NULL, error = '', skip_code = '' WHERE id = ?",
            (sent_iso, int(message_id), sent_iso, reminder_id),
        )


def mark_reminder_failed(
    reminder_id: int,
    *,
    error: str,
    attempts: int,
    now: datetime | None = None,
    next_attempt_at: datetime | None = None,
    final: bool = False,
) -> None:
    now = now or utils.now_utc()
    with connection() as conn:
        conn.execute(
            "UPDATE reminders SET status = ?, attempts = ?, last_attempt_at = ?, "
            "next_attempt_at = ?, lease_until = NULL, error = ? WHERE id = ?",
            (
                "failed" if final else "pending",
                int(attempts),
                _iso(now),
                _iso(next_attempt_at) if next_attempt_at else None,
                (error or "")[:500],
                reminder_id,
            ),
        )


def skip_reminder(reminder_id: int, reason: str, code: str = "") -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE reminders SET status = 'skipped', skip_code = ?, error = ?, lease_until = NULL "
            "WHERE id = ?",
            (code, (reason or "")[:500], reminder_id),
        )


def pending_reminder_counts() -> dict[str, int]:
    """Paneldeki 'bekleyen iş' sayaçları."""
    with connection() as conn:
        rows = conn.execute(
            "SELECT r.kind, COUNT(*) AS total FROM reminders r "
            "JOIN appointments a ON a.id = r.appointment_id "
            "WHERE r.status IN ('pending','failed') "
            f"AND a.status NOT IN {_INACTIVE_SQL} GROUP BY r.kind"
        ).fetchall()
    counts = {"confirmation": 0, "review": 0}
    for row in rows:
        counts[row["kind"]] = int(row["total"])
    return counts


# ---------------------------------------------------------------------
# Mesaj günlüğü
# ---------------------------------------------------------------------
def log_message(
    *,
    phone: str,
    body: str,
    kind: str,
    provider: str,
    status: str,
    appointment_id: int | None = None,
    customer_id: int | None = None,
    simulated: bool = False,
    error: str = "",
    created_at: datetime | None = None,
) -> int:
    created_at = created_at or utils.now_utc()
    with connection() as conn:
        cursor = conn.execute(
            "INSERT INTO messages (appointment_id, customer_id, kind, phone, body, provider, "
            "status, simulated, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                appointment_id,
                customer_id,
                kind,
                phone,
                body,
                provider,
                status,
                1 if simulated else 0,
                (error or "")[:500],
                _iso(created_at),
            ),
        )
        return int(cursor.lastrowid)


def last_message_at(appointment_id: int, kind: str) -> datetime | None:
    """Randevuya bu türde gönderilen (başarılı/simüle) son mesajın zamanı."""
    with connection() as conn:
        row = conn.execute(
            "SELECT created_at FROM messages WHERE appointment_id = ? AND kind = ? "
            "AND status IN ('sent','simulated') ORDER BY id DESC LIMIT 1",
            (int(appointment_id), kind),
        ).fetchone()
    return utils.parse_iso(row["created_at"]) if row is not None else None


def recent_messages(limit: int = 15) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT m.*, c.name AS customer_name FROM messages m "
            "LEFT JOIN customers c ON c.id = m.customer_id "
            "ORDER BY m.id DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return [dict(row) for row in rows]


def purge_old_messages(days: int, now: datetime | None = None) -> int:
    """Saklama süresi dolmuş mesaj kayıtlarını siler (KVKK: gereğinden uzun tutma)."""
    if days <= 0:
        return 0
    cutoff = _iso((now or utils.now_utc()) - timedelta(days=days))
    with connection() as conn:
        # Hatırlatmalar mesaja referans verir; ON DELETE SET NULL ile bağ kopar.
        cursor = conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
        return cursor.rowcount


# ---------------------------------------------------------------------
# Yedekleme
# ---------------------------------------------------------------------
def backup_database(target_dir: Path, keep: int = 14, now: datetime | None = None) -> Path:
    """Çalışırken bile tutarlı bir kopya alır (SQLite backup API) ve eskileri temizler."""
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = utils.to_utc(now or utils.now_utc()).strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"esnaf-{stamp}.db"
    source = sqlite3.connect(str(get_db_path()), timeout=20)
    try:
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    backups = sorted(target_dir.glob("esnaf-*.db"))
    for old in backups[: max(0, len(backups) - max(1, keep))]:
        try:
            old.unlink()
        except OSError:
            pass
    return target


# ---------------------------------------------------------------------
# Panel istatistikleri
# ---------------------------------------------------------------------
def dashboard_stats(today_start: datetime, today_end: datetime) -> dict[str, int]:
    with connection() as conn:
        row = conn.execute(
            "SELECT "
            f" (SELECT COUNT(*) FROM appointments WHERE starts_at >= :s AND starts_at < :e "
            f"   AND status NOT IN {_INACTIVE_SQL}) AS today_total,"
            " (SELECT COUNT(*) FROM messages WHERE created_at >= :s AND created_at < :e "
            "   AND status IN ('sent','simulated')) AS messages_today,"
            f" (SELECT COUNT(*) FROM appointments WHERE starts_at >= :e AND starts_at < :w "
            f"   AND status NOT IN {_INACTIVE_SQL}) AS upcoming_week",
            {
                "s": _iso(today_start),
                "e": _iso(today_end),
                "w": _iso(today_end + timedelta(days=7)),
            },
        ).fetchone()
    pending = pending_reminder_counts()
    return {
        "today_total": int(row["today_total"]),
        "messages_today": int(row["messages_today"]),
        "upcoming_week": int(row["upcoming_week"]),
        "pending_confirmation": pending["confirmation"],
        "pending_review": pending["review"],
    }


# ---------------------------------------------------------------------
# Örnek veri
# ---------------------------------------------------------------------
def seed_demo() -> int:
    """Panelin hemen çalıştığını göstermek için örnek randevular ekler.

    Veritabanında zaten müşteri varsa hiçbir şey yapmaz (idempotent).
    """
    with connection() as conn:
        existing = conn.execute("SELECT COUNT(*) AS c FROM customers").fetchone()["c"]
    if existing:
        return 0

    settings = get_settings()
    tz_name = settings["timezone"]
    now = utils.now_utc()
    local_now = utils.to_tz(now, tz_name)
    tomorrow = (local_now + timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)

    if not settings.get("google_review_url"):
        save_settings({"google_review_url": "https://g.page/r/ornek-isletme/review"})

    demo_people = [
        ("Ayşe Yılmaz", "905551112233"),
        ("Mehmet Demir", "905322445566"),
        ("Zeynep Kaya", "905443778899"),
        ("Ali Çelik", "905554009911"),
        ("Elif Şahin", "905365123456"),
    ]
    customer_ids = [upsert_customer(name, phone) for name, phone in demo_people]

    appointments = [
        # 90 dk sonra: onay mesajı penceresi geçmiş -> motor hemen göndermeyi dener
        (customer_ids[0], now + timedelta(minutes=90), 60, "Saç Kesimi + Fön"),
        # 4 saat sonra: onay mesajı ileride bekliyor
        (customer_ids[1], now + timedelta(hours=4), 60, "Sakal Tıraşı"),
        # 2 saat önce bitmiş: yorum mesajı zamanı gelmiş
        (customer_ids[2], now - timedelta(hours=2), 45, "Saç Boyama"),
        # yarın 11:00
        (customer_ids[3], tomorrow, 90, "Cilt Bakımı"),
        # 2 gün sonra, iptal edilmiş
        (customer_ids[4], now + timedelta(days=2), 60, "Kesim + Yıkama"),
    ]
    created = 0
    for customer_id, starts_at, duration, service in appointments:
        appointment_id = create_appointment(customer_id, starts_at, duration, service, notes="Örnek kayıt")
        created += 1
        if service == "Kesim + Yıkama":
            set_appointment_status(appointment_id, "cancelled")
    return created
