"""Простое ограничение частоты запросов (скользящее окно в памяти процесса).
Для нескольких экземпляров приложения заменить на Redis/ограничение на уровне nginx."""
import time
from collections import defaultdict, deque

from app.config import get_settings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse

RULES = [
    # (префикс пути, запросов, окно в секундах)
    ("/auth/", 30, 60),
    ("/api/", 120, 60),
    ("/telegram/", 600, 60),
    ("/", 300, 60),
]


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.hits: dict[tuple[str, str], deque] = defaultdict(deque)

    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/static/") or get_settings().app_env == "test":
            return await call_next(request)
        ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
        path = request.url.path
        for prefix, limit, window in RULES:
            if path.startswith(prefix):
                key = (ip, prefix)
                q = self.hits[key]
                now = time.monotonic()
                while q and now - q[0] > window:
                    q.popleft()
                if len(q) >= limit:
                    if path.startswith("/api/"):
                        return JSONResponse({"error": "rate_limited", "message": "Слишком много запросов."}, 429)
                    return PlainTextResponse("Слишком много запросов. Повторите через минуту.", 429)
                q.append(now)
                break
        return await call_next(request)
