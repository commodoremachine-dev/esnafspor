"""Test yardımcıları.

`tempfile.TemporaryDirectory` Windows'ta 0o700 izinli klasör oluşturur; bazı
kısıtlı (sandbox) ortamlarda bu klasör yazılamaz hale gelebiliyor. Bu yüzden
testler varsayılan izinlerle benzersiz bir geçici klasör kullanır.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path


def make_temp_dir(prefix: str = "esnaf-test-") -> Path:
    base = Path(tempfile.gettempdir()) / "esnaf-randevu-tests"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{prefix}{uuid.uuid4().hex[:12]}"
    path.mkdir()
    return path


def remove_temp_dir(path: Path | str) -> None:
    shutil.rmtree(path, ignore_errors=True)
