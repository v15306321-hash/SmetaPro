"""Точка входа веб-приложения: публичный сайт, веб-кабинет, админ-панель, REST API, вебхуки.

Запуск: uvicorn app.main:app
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import db as dbmod
from app.config import get_settings
from app.core.errors import DomainError
from app.services.seed import ensure_seed
from app.web.deps import LoginRequired, render
from app.web.ratelimit import RateLimitMiddleware

log = logging.getLogger("app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    s = get_settings()
    if not s.is_production and s.database_url.startswith("sqlite"):
        # локальная разработка без миграций; в production схема создаётся `alembic upgrade head`
        dbmod.Base.metadata.create_all(dbmod.engine)
    with dbmod.session_scope() as db:
        ensure_seed(db)
    embedded_bot = s.bot_mode == "webhook" and bool(s.bot_token)
    if embedded_bot:
        from app.bot.runner import start_embedded
        await start_embedded()
    yield
    if embedded_bot:
        from app.bot.runner import stop_embedded
        await stop_embedded()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Furniture Estimate API", version="1.0.0", lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.add_middleware(RateLimitMiddleware)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        if request.method == "POST":
            # действие могло создать уведомления — разбудить доставку (в режиме webhook бот в этом процессе)
            from app.bot.runner import kick
            kick()
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if s.is_production:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")

    from app.web.routes import admin, api, auth, cabinet, public, webhooks
    for r in (public.router, auth.router, cabinet.router, admin.router, api.router, webhooks.router):
        app.include_router(r)

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, _exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized", "message": "Требуется вход."}, 401)
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.code, "message": exc.message, **exc.extra}, exc.http_status)
        return render(request, "public/error.html", status_code=exc.http_status, code=exc.http_status,
                      message=exc.message, error=exc.code, extra=exc.extra)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        # Внутренние детали не раскрываются пользователю; полная трассировка — в логах сервера
        log.exception("Unhandled error on %s", request.url.path)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "internal", "message": "Внутренняя ошибка. Попробуйте позже."}, 500)
        return render(request, "public/error.html", status_code=500, code=500, message="Что-то пошло не так. Попробуйте позже.",
                      error="internal", extra={})

    return app


app = create_app()
