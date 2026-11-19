"""Централизованная конфигурация. Все секреты читаются только из окружения."""
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    base_url: str = "http://localhost:8000"
    secret_key: str = "dev-secret-key-change-me"

    database_url: str = "sqlite:///./dev.db"

    bot_token: str = ""
    bot_username: str = "FurnitureEstimateBot"
    bot_mode: str = "polling"
    webhook_secret: str = ""
    superadmin_telegram_ids: str = ""

    crypto_pay_api_token: str = ""
    crypto_pay_api_url: str = "https://pay.crypt.bot/api"

    session_ttl_days: int = 30

    # Как часто (сек) запускать периодические задачи (напоминания о подписке). Уведомления о действиях
    # отправляются сразу. Для бесплатной БД Neon — 3600, чтобы база могла «засыпать» между обращениями.
    jobs_interval_seconds: int = 600
    # Не держать открытые соединения с БД (Neon Free: база засыпает только без соединений)
    db_null_pool: bool = False
    # Вход на сайт без Telegram для локальной разработки (/auth/dev-login). НИКОГДА не включать в production.
    dev_login: bool = False
    # Показывать Telegram Login Widget (нужно указать домен сайта в @BotFather: /setdomain)
    telegram_login_widget: bool = False

    # Контакты поддержки (предоставляются заказчиком)
    support_telegram: str = ""
    support_phone: str = ""
    support_email: str = ""

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        # Render, Heroku и др. выдают postgres:// или postgresql:// — указываем драйвер psycopg 3
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v

    @property
    def effective_webhook_secret(self) -> str:
        """Секрет заголовка webhook: WEBHOOK_SECRET или производный от SECRET_KEY."""
        if self.webhook_secret:
            return self.webhook_secret
        import hashlib
        return hashlib.sha256(("tg-webhook:" + self.secret_key).encode()).hexdigest()[:48]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def superadmin_ids(self) -> set[int]:
        return {int(x) for x in self.superadmin_telegram_ids.replace(" ", "").split(",") if x}

    @property
    def bot_link(self) -> str:
        return f"https://t.me/{self.bot_username}"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.is_production and s.secret_key in ("", "change-me", "dev-secret-key-change-me"):
        raise RuntimeError("SECRET_KEY must be set in production")
    if s.is_production and s.dev_login:
        raise RuntimeError("DEV_LOGIN must be disabled in production")
    return s
