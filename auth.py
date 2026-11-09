"""Panel parolası yardımcıları (Flask ve Streamlit panelleri ortak kullanır).

Parola `ESNAF_PASSWORD` ortam değişkeninden gelir. Oturumda parolanın kendisi
değil kısa bir özeti tutulur; parola değişince eski oturumlar geçersiz olur.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time

LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 300


def fingerprint(password: str) -> str:
    return hashlib.sha256(("esnaf-login:" + password).encode("utf-8")).hexdigest()[:32]


def check_password(supplied: str, expected: str) -> bool:
    """Sabit zamanlı karşılaştırma; uzunluk farkı bile sızmasın diye özetler kıyaslanır."""
    if not expected:
        return False
    return hmac.compare_digest(
        hashlib.sha256((supplied or "").encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def session_is_valid(stored: object, expected_password: str) -> bool:
    if not expected_password:
        return True
    return bool(stored) and hmac.compare_digest(str(stored), fingerprint(expected_password))


class LoginThrottle:
    """İstemci başına art arda hatalı denemeleri sınırlar (bellek içi)."""

    def __init__(self, max_failures: int = LOGIN_MAX_FAILURES, lock_seconds: int = LOGIN_LOCK_SECONDS):
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self._failures: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def locked_for(self, client: str) -> float:
        """İstemci kilitliyse kalan saniyeyi, değilse 0 döndürür."""
        with self._lock:
            _failures, locked_until = self._failures.get(client, (0, 0.0))
            remaining = locked_until - time.monotonic()
            return remaining if remaining > 0 else 0.0

    def record_failure(self, client: str) -> None:
        with self._lock:
            failures, _locked_until = self._failures.get(client, (0, 0.0))
            failures += 1
            locked_until = 0.0
            if failures >= self.max_failures:
                locked_until = time.monotonic() + self.lock_seconds
                failures = 0
            self._failures[client] = (failures, locked_until)

    def reset(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)

    def clear(self) -> None:
        with self._lock:
            self._failures.clear()


def lock_message(remaining_seconds: float) -> str:
    return f"Çok fazla hatalı deneme. {int(remaining_seconds // 60) + 1} dakika sonra tekrar deneyin."
