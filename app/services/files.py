"""Безопасное сохранение загружаемых файлов (логотипы, чеки)."""
import uuid
from pathlib import Path

from app.config import get_settings
from app.core.errors import ValidationError

MAX_SIZE = 8 * 1024 * 1024
_SIGNATURES = {
    "png": [b"\x89PNG\r\n\x1a\n"],
    "jpg": [b"\xff\xd8\xff"],
    "pdf": [b"%PDF-"],
    "webp": [b"RIFF"],
}


def detect_type(data: bytes) -> str | None:
    for ext, sigs in _SIGNATURES.items():
        if any(data.startswith(s) for s in sigs):
            if ext == "webp" and data[8:12] != b"WEBP":
                continue
            return ext
    return None


def _store(data: bytes, allowed: set[str], folder: str) -> str:
    if not data:
        raise ValidationError("Файл пустой.")
    if len(data) > MAX_SIZE:
        raise ValidationError("Файл слишком большой (максимум 8 МБ).")
    ext = detect_type(data)  # тип определяется по содержимому, а не по имени файла
    if ext not in allowed:
        raise ValidationError("Неподдерживаемый формат файла. Допустимо: " + ", ".join(sorted(allowed)).upper())
    root = get_settings().storage_dir.resolve()
    target_dir = (root / folder).resolve()
    if root not in target_dir.parents and target_dir != root:
        raise ValidationError("Недопустимый путь.")
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{ext}"
    (target_dir / name).write_bytes(data)
    return str(Path(folder) / name)


def save_image(data: bytes, _ext_hint: str = "", folder: str = "images") -> str:
    return _store(data, {"png", "jpg"}, folder)


def save_receipt(data: bytes, folder: str = "receipts") -> str:
    return _store(data, {"png", "jpg", "pdf", "webp"}, folder)


def abs_path(rel: str) -> Path:
    root = get_settings().storage_dir.resolve()
    p = (root / rel).resolve()
    if root not in p.parents:
        raise ValidationError("Недопустимый путь.")
    return p
