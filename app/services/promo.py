"""Акция на подписку (например, −50 % до 1 января)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.config import get_settings
from app.core.fmt import TASHKENT_OFFSET


def _until() -> date | None:
    try:
        return date.fromisoformat(get_settings().promo_until)
    except (TypeError, ValueError):
        return None


def active_percent(now: datetime | None = None) -> int:
    s = get_settings()
    until = _until()
    today = ((now or datetime.utcnow()) + TASHKENT_OFFSET).date()
    if s.promo_percent <= 0 or not until or today >= until:
        return 0
    return min(90, s.promo_percent)


def last_day() -> date | None:
    until = _until()
    return until - timedelta(days=1) if until else None


def banner(now: datetime | None = None) -> str | None:
    """«🔥 Акция: до 1 января все тарифы со скидкой 50%!» или None."""
    pct = active_percent(now)
    until = _until()
    if not pct or not until:
        return None
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря"]
    return f"🔥 Акция: до {until.day} {months[until.month - 1]} все тарифы со скидкой {pct}%!"
