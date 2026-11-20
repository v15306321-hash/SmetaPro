"""Публичный сайт (ТЗ 16.2) + SEO (sitemap, robots)."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import User
from app.services import tariffs
from app.web.deps import optional_user, render

router = APIRouter(include_in_schema=False)

PAGES = ["/", "/features", "/pricing", "/how-it-works", "/faq", "/contacts", "/legal/terms",
         "/legal/privacy", "/legal/subscription"]

FAQ = [
    ("Нужно ли что-то устанавливать?",
     "Нет. Основная работа ведётся в Telegram-боте, а веб-кабинет открывается в любом браузере."),
    ("Как считается стоимость заказа?",
     "Бот по шагам спрашивает изделия, материалы (листами), фурнитуру и технические работы. Их сумма — "
     "себестоимость; к ней добавляется стоимость работы цеха — ваша наценка в процентах на общую сумму."),
    ("Можно ли задать свою наценку на тарифе START?",
     "Да. Стоимость работы цеха (наценку) можно настроить на любом тарифе — для всех заказов или для одного."),
    ("Почему «Сборки» и «Монтажа» нет в производственных затратах?",
     "Это работы по заказу — они добавляются в расчёт заказа как технические работы, а не в общие затраты."),
    ("Что будет, если подписка закончится?",
     "Платные функции временно блокируются, но все данные сохраняются. После оплаты доступ восстанавливается."),
    ("Переносится ли остаток генераций смет?",
     "Нет. Лимит обновляется в каждом новом оплаченном 30-дневном периоде."),
    ("Как работает оплата?",
     "Выберите тариф и способ оплаты (HUMO, CLICK, UZUM BANK), переведите сумму и отправьте чек в бот. "
     "Подписка активируется после проверки администратором."),
    ("Как получить скидку?",
     "Пригласите владельцев других мебельных предприятий по своей ссылке. За каждого, кто впервые оплатит "
     "подписку, скидка на вашу следующую покупку растёт на 10% — до 50%."),
    ("Является ли PDF-смета кассовым чеком?",
     "Нет. Смета — коммерческий документ (предложение клиенту), а не фискальный чек."),
]


def _plans(db: Session):
    return tariffs.list_plans(db)


@router.get("/")
def index(request: Request, db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    return render(request, "public/index.html", user=user, plans=_plans(db), faq=FAQ[:4], page="home")


@router.get("/features")
def features(request: Request, db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    return render(request, "public/features.html", user=user, plans=_plans(db), page="features")


@router.get("/pricing")
def pricing(request: Request, db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    return render(request, "public/pricing.html", user=user, plans=_plans(db), page="pricing")


@router.get("/how-it-works")
def how(request: Request, user: User | None = Depends(optional_user)):
    return render(request, "public/how.html", user=user, page="how")


@router.get("/faq")
def faq(request: Request, user: User | None = Depends(optional_user)):
    return render(request, "public/faq.html", user=user, faq=FAQ, page="faq")


@router.get("/contacts")
def contacts(request: Request, user: User | None = Depends(optional_user)):
    return render(request, "public/contacts.html", user=user, page="contacts")


@router.get("/legal/{doc}")
def legal(doc: str, request: Request, db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    if doc not in ("terms", "privacy", "subscription"):
        return render(request, "public/error.html", status_code=404, code=404, message="Страница не найдена.", error="not_found",
                      extra={})
    return render(request, f"public/legal_{doc}.html", user=user, plans=_plans(db), page="legal")


@router.get("/r/{code}")
def referral(code: str):
    """Реферальная ссылка для сайта: сохраняет код и ведёт в бота, где происходит регистрация."""
    s = get_settings()
    resp = RedirectResponse(f"https://t.me/{s.bot_username}?start=ref_{code[:16]}", status_code=303)
    resp.set_cookie("fe_ref", code[:16], max_age=30 * 86400, httponly=True, samesite="lax")
    return resp


@router.get("/healthz")
def healthz():
    """Проверка доступности без обращения к БД (Render health check, UptimeRobot)."""
    return PlainTextResponse("ok")


@router.get("/robots.txt")
def robots():
    s = get_settings()
    return PlainTextResponse(f"User-agent: *\nDisallow: /cabinet\nDisallow: /admin\nDisallow: /api\n"
                             f"Sitemap: {s.base_url.rstrip('/')}/sitemap.xml\n")


@router.get("/sitemap.xml")
def sitemap():
    base = get_settings().base_url.rstrip("/")
    urls = "".join(f"<url><loc>{base}{p}</loc></url>" for p in PAGES)
    return Response(f'<?xml version="1.0" encoding="UTF-8"?>'
                    f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
                    media_type="application/xml")
