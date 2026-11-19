"""Хранение файлов (логотипы, чеки, PDF-сметы) в базе данных.

Почему в БД, а не на диске: на бесплатных/контейнерных хостингах (Render Free и др.) файловая система
временная и очищается при перезапуске; в БД файлы сохраняются и попадают в её резервные копии.
Объёмы небольшие: смета ~50 КБ, логотип и чек — до 8 МБ (проверяется при загрузке).
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core.errors import NotFound, ValidationError
from app.models import StoredFile

MAX_SIZE = 8 * 1024 * 1024
_SIGNATURES = {
    "png": [b"\x89PNG\r\n\x1a\n"],
    "jpg": [b"\xff\xd8\xff"],
    "pdf": [b"%PDF-"],
    "webp": [b"RIFF"],
}
CONTENT_TYPES = {"png": "image/png", "jpg": "image/jpeg", "pdf": "application/pdf", "webp": "image/webp"}


def detect_type(data: bytes) -> str | None:
    for ext, sigs in _SIGNATURES.items():
        if any(data.startswith(s) for s in sigs):
            if ext == "webp" and data[8:12] != b"WEBP":
                continue
            return ext
    return None


def _store(db: Session, data: bytes, allowed: set[str], kind: str, enterprise_id: int | None) -> str:
    if not data:
        raise ValidationError("Файл пустой.")
    if len(data) > MAX_SIZE:
        raise ValidationError("Файл слишком большой (максимум 8 МБ).")
    ext = detect_type(data)  # тип определяется по содержимому, а не по имени файла
    if ext not in allowed:
        raise ValidationError("Неподдерживаемый формат файла. Допустимо: " + ", ".join(sorted(allowed)).upper())
    key = f"{kind[:3]}-{uuid.uuid4().hex}"  # ≤ 36 символов
    db.add(StoredFile(id=key, enterprise_id=enterprise_id, kind=kind, content_type=CONTENT_TYPES[ext],
                      size=len(data), data=data))
    db.flush()
    return key


def save_image(db: Session, data: bytes, enterprise_id: int | None, kind: str = "logo") -> str:
    return _store(db, data, {"png", "jpg"}, kind, enterprise_id)


def save_receipt(db: Session, data: bytes, enterprise_id: int | None) -> str:
    return _store(db, data, {"png", "jpg", "pdf", "webp"}, "receipt", enterprise_id)


def save_pdf(db: Session, data: bytes, enterprise_id: int | None) -> str:
    return _store(db, data, {"pdf"}, "estimate", enterprise_id)


def load(db: Session, key: str | None, enterprise_id: int | None = None) -> StoredFile:
    """enterprise_id — дополнительная проверка принадлежности (изоляция предприятий)."""
    f = db.get(StoredFile, key) if key else None
    if not f or (enterprise_id is not None and f.enterprise_id != enterprise_id):
        raise NotFound("Файл не найден.")
    return f


def delete(db: Session, key: str | None) -> None:
    f = db.get(StoredFile, key) if key else None
    if f:
        db.delete(f)
        db.flush()
