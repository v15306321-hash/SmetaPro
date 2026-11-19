"""Токены, хеши и проверка подписи Telegram Login Widget."""
import hashlib
import hmac
import secrets
import string
import time

_ALPHABET = string.ascii_uppercase + string.digits


def token_urlsafe(n: int = 32) -> str:
    return secrets.token_urlsafe(n)


def short_code(n: int = 8) -> str:
    # без похожих символов
    alphabet = "".join(c for c in _ALPHABET if c not in "0O1I")
    return "".join(secrets.choice(alphabet) for _ in range(n))


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def verify_telegram_login(data: dict, bot_token: str, max_age: int = 86400) -> bool:
    """Проверка данных Telegram Login Widget (https://core.telegram.org/widgets/login)."""
    if not bot_token:
        return False
    data = dict(data)
    received_hash = data.pop("hash", None)
    if not received_hash:
        return False
    check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data) if data[k] is not None)
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(received_hash)):
        return False
    try:
        auth_date = int(data.get("auth_date", 0))
    except (TypeError, ValueError):
        return False
    return time.time() - auth_date <= max_age
