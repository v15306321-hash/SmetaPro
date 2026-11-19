"""Доменные ошибки. Тексты безопасно показывать пользователю — без технических деталей."""


class DomainError(Exception):
    code = "error"
    http_status = 400

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra


class NotFound(DomainError):
    code = "not_found"
    http_status = 404


class PermissionDenied(DomainError):
    code = "forbidden"
    http_status = 403


class SubscriptionRequired(DomainError):
    code = "subscription_required"
    http_status = 402


class FeatureUnavailable(DomainError):
    """Функция недоступна на текущем тарифе. extra: feature, min_plan."""
    code = "feature_unavailable"
    http_status = 402


class LimitReached(DomainError):
    code = "limit_reached"
    http_status = 409


class ValidationError(DomainError):
    code = "validation"
    http_status = 422


class Conflict(DomainError):
    code = "conflict"
    http_status = 409


class Blocked(DomainError):
    code = "blocked"
    http_status = 403
