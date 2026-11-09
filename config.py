"""Uygulama yapılandırması: dosya yolları, .env okuma, günlük kaydı.

Bu modül bilinçli olarak bağımlılık içermez; `python main.py` ile çalışan
tek dosyalık bir kurulumda tüm yollar buradan yönetilir.
"""

from __future__ import annotations

import logging
import os
import secrets
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
APP_NAME = "Esnaf Randevu Asistanı"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000
DEFAULT_TIMEZONE = "Europe/Istanbul"
DEFAULT_WORKER_INTERVAL = 30  # saniye


# ---------------------------------------------------------------------
# .env desteği (harici paket gerekmez)
# ---------------------------------------------------------------------
def load_dotenv(path: Path | None = None) -> None:
    """`.env` dosyasındaki KEY=VALUE satırlarını ortam değişkenlerine ekler.

    Var olan ortam değişkenleri ezilmez; böylece `ESNAF_DB=... python main.py`
    gibi komut satırı kullanımı her zaman öncelikli olur.
    """
    env_path = path or (BASE_DIR / ".env")
    if not env_path.is_file():
        return
    try:
        content = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


# ---------------------------------------------------------------------
# Yollar
# ---------------------------------------------------------------------
def data_dir() -> Path:
    """Veritabanı, gizli anahtar ve diğer kalıcı dosyaların klasörü."""
    return Path(os.environ.get("ESNAF_DATA_DIR", BASE_DIR / "data")).expanduser()


def db_path() -> Path:
    """SQLite veritabanı dosyasının yolu (ESNAF_DB ile değiştirilebilir)."""
    custom = os.environ.get("ESNAF_DB")
    if custom:
        return Path(custom).expanduser()
    return data_dir() / "esnaf.db"


def logs_dir() -> Path:
    return Path(os.environ.get("ESNAF_LOG_DIR", data_dir() / "logs")).expanduser()


def ensure_dirs() -> None:
    """Gerekli klasörleri (data, logs) oluşturur."""
    for directory in (data_dir(), logs_dir()):
        directory.mkdir(parents=True, exist_ok=True)


def secret_key() -> str:
    """Flask oturum/flash çerezleri için kalıcı rastgele anahtar.

    Anahtar `data/.secret_key` dosyasında saklanır; böylece uygulama her
    yeniden başladığında oturumdaki bildirimler kaybolmaz.
    """
    key_file = data_dir() / ".secret_key"
    if key_file.is_file():
        try:
            existing = key_file.read_text(encoding="utf-8").strip()
        except OSError:
            # Dosya var ama okunamıyor: üzerine yazma (kalıcı anahtar kaybolmasın),
            # bu çalıştırma için geçici anahtar kullan.
            logging.getLogger(__name__).warning(
                "Gizli anahtar dosyası okunamadı (%s); geçici anahtar kullanılıyor.", key_file
            )
            return secrets.token_hex(32)
        if existing:
            return existing
    key = secrets.token_hex(32)
    try:
        ensure_dirs()
        key_file.write_text(key, encoding="utf-8")
    except OSError:
        pass
    return key


# ---------------------------------------------------------------------
# Sunucu ayarları
# ---------------------------------------------------------------------
def host() -> str:
    return os.environ.get("ESNAF_HOST", DEFAULT_HOST)


def port() -> int:
    try:
        return int(os.environ.get("ESNAF_PORT", DEFAULT_PORT))
    except (TypeError, ValueError):
        return DEFAULT_PORT


def admin_password() -> str:
    """Panel giriş parolası (ESNAF_PASSWORD). Boşsa giriş istenmez.

    Panel yerel ağa açılıyorsa (`--host 0.0.0.0`) parola zorunludur;
    `main.py` parola olmadan başlamayı reddeder.
    """
    return os.environ.get("ESNAF_PASSWORD", "")


def is_loopback(host_name: str) -> bool:
    return host_name in ("127.0.0.1", "localhost", "::1")


def worker_interval() -> int:
    try:
        value = int(os.environ.get("ESNAF_WORKER_INTERVAL", DEFAULT_WORKER_INTERVAL))
    except (TypeError, ValueError):
        return DEFAULT_WORKER_INTERVAL
    return max(5, min(value, 3600))


# ---------------------------------------------------------------------
# Günlük kaydı
# ---------------------------------------------------------------------
def setup_logging(level: int = logging.INFO) -> None:
    """Konsol + `data/logs/app.log` dosyasına günlük kaydı kurar."""
    ensure_dirs()
    root = logging.getLogger()
    if getattr(root, "_esnaf_configured", False):
        return

    root.setLevel(level)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            logs_dir() / "app.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:  # disk izni yoksa sadece konsola yaz
        pass

    # Flask/Werkzeug istek günlüklerini biraz sakinleştir
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    root._esnaf_configured = True  # type: ignore[attr-defined]
