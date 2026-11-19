import hashlib
import hmac
import re
import time

import pytest
from fastapi.testclient import TestClient

from app.models import ItemKind, PaymentRequest, User
from app.services import accounts, auth_web, estimates, materials, orders, tariffs
from tests.conftest import make_enterprise

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture()
def client(db):
    from app.main import app
    with TestClient(app) as c:
        yield c


def login(client, tg, name="Пользователь", admin=False):
    c = TestClient(client.app)
    r = c.get(f"/auth/dev-login?tg={tg}&name={name}" + ("&admin=1" if admin else ""), follow_redirects=False)
    assert r.status_code == 303
    return c


def csrf(c, path):
    html = c.get(path).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.mark.parametrize("path", ["/", "/features", "/pricing", "/how-it-works", "/faq", "/contacts",
                                  "/legal/terms", "/legal/privacy", "/legal/subscription", "/login",
                                  "/sitemap.xml", "/robots.txt"])
def test_public_pages(client, path):
    r = client.get(path)
    assert r.status_code == 200
    if path == "/pricing":
        assert "149" in r.text and "ENTERPRISE" in r.text


def test_cabinet_requires_login(client):
    r = client.get("/cabinet", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/api/v1/me").status_code == 401


def test_full_web_flow(client, db):
    c = login(client, 5001, "Владелец")
    assert "Зарегистрируйте предприятие" in c.get("/cabinet").text
    r = c.post("/cabinet/onboarding", data={"name": "Цех Тест", "phone": "+998", "address": "Ташкент",
                                           "csrf": csrf(c, "/cabinet")})
    assert r.status_code == 200 and "Оплатить подписку" in r.text

    # заявка на оплату → чек
    token = csrf(c, "/cabinet/subscription")
    r = c.post("/cabinet/subscription/request", data={"plan": "PRO", "method": "HUMO", "csrf": token})
    assert "9860 0101 4036 3283" in r.text
    pid = int(re.search(r"/cabinet/payments/(\d+)/receipt", r.text).group(1))
    r = c.post(f"/cabinet/payments/{pid}/receipt", data={"csrf": token},
               files={"receipt": ("check.png", PNG, "image/png")})
    assert "Чек на проверке" in r.text

    # без CSRF — отказ
    r = c.post(f"/cabinet/payments/{pid}/cancel", data={})
    assert "Сессия формы устарела" in r.text

    # обычный пользователь не видит админку
    assert c.get("/admin").status_code == 403

    # суперадмин подтверждает
    a = login(client, 999, "Админ", admin=True)
    assert "Заявки на проверке" in a.get("/admin").text
    r = a.post(f"/admin/payments/{pid}/approve", data={"csrf": csrf(a, "/admin/payments")})
    assert "подтверждена" in r.text
    r = a.post(f"/admin/payments/{pid}/approve", data={"csrf": csrf(a, "/admin/payments")})
    assert "уже обработана" in r.text  # идемпотентность
    for page in ("/admin/users", "/admin/enterprises", "/admin/plans", "/admin/referrals", "/admin/audit",
                 "/admin/payments?status=all"):
        assert a.get(page).status_code == 200, page

    # данные из «бота» (сервисный слой) видны в кабинете
    db.expire_all()
    user = db.query(User).filter_by(telegram_id=5001).one()
    ctx = accounts.load_ctx(db, user)
    m = materials.create(ctx, kind=ItemKind.material, name="ЛДСП Egger", unit="м²", price=95000, category="ЛДСП")
    o = orders.create(ctx, title="Кухня", client_name="Алишер")
    orders.add_item(ctx, o.id, kind=ItemKind.material, material_id=m.id, height_mm=2000, width_mm=1000, pieces=3,
                    section="Кухня")
    est = estimates.generate(ctx, o.id)
    db.commit()

    html = c.get("/cabinet").text
    assert "Выручка" in html and "Алишер" in html and est.uid in html
    for page in ("/cabinet/analytics", "/cabinet/analytics?period=year", f"/cabinet/orders/{o.id}",
                 "/cabinet/orders", "/cabinet/staff", "/cabinet/referrals", "/cabinet/profile",
                 "/cabinet/payments", f"/cabinet/payments/{pid}", "/cabinet/subscription"):
        assert c.get(page).status_code == 200, page
    pdf = c.get(f"/cabinet/estimates/{est.uid}.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    # приглашение сотрудника через кабинет
    r = c.post("/cabinet/staff/invite", data={"role": "manager", "csrf": csrf(c, "/cabinet/staff")})
    assert "t.me/" in r.text and "inv_" in r.text

    # API
    me = c.get("/api/v1/me").json()
    assert me["enterprise"]["role"] == "owner" and me["subscription"]["plan"] == "PRO"
    assert c.get("/api/v1/orders").json()[0]["client_name"] == "Алишер"
    assert c.get("/api/v1/analytics/summary").json()["orders"] == 1

    # чужой пользователь не видит заказ и смету
    other = login(client, 5002, "Чужой")
    other.post("/cabinet/onboarding", data={"name": "Чужой цех", "csrf": csrf(other, "/cabinet")})
    assert other.get(f"/cabinet/orders/{o.id}").status_code == 404
    assert other.get(f"/cabinet/estimates/{est.uid}.pdf").status_code == 404
    assert other.get(f"/api/v1/orders/{o.id}").status_code == 404


def test_feature_locked_page(client, db):
    ctx = make_enterprise(db, "START", user=None)
    db.commit()
    c = TestClient(client.app)
    c.get(f"/auth/dev-login?tg={ctx.user.telegram_id}")
    r = c.get("/cabinet/analytics")
    assert "недоступно на тарифе START" in r.text and "BUSINESS" in r.text
    assert "Финансовая аналитика" in c.get("/cabinet").text


def test_bot_confirmed_login(client, db):
    r = client.post("/auth/start").json()
    assert r["link"].endswith("login_" + r["token"])
    assert client.post("/auth/poll", data={"token": r["token"], "poll": r["poll"]}).json()["status"] == "waiting"
    assert client.post("/auth/poll", data={"token": r["token"], "poll": "wrong"}).status_code == 410
    user, _ = accounts.get_or_create_telegram_user(db, 7777, full_name="Бот")
    auth_web.confirm_login(db, user, r["token"])
    db.commit()
    res = client.post("/auth/poll", data={"token": r["token"], "poll": r["poll"]})
    assert res.json()["status"] == "ok" and "fe_session" in res.cookies
    assert client.post("/auth/poll", data={"token": r["token"], "poll": r["poll"]}).status_code == 410  # одноразово
    assert client.get("/cabinet").status_code == 200


def test_telegram_widget_signature(client):
    data = {"id": "4242", "first_name": "Ann", "auth_date": str(int(time.time()))}
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    data["hash"] = hmac.new(hashlib.sha256(b"123456:TEST-TOKEN").digest(), check.encode(), "sha256").hexdigest()
    ok = client.get("/auth/telegram", params=data, follow_redirects=False)
    assert ok.status_code == 303 and "fe_session" in ok.cookies
    data["first_name"] = "Mallory"
    bad = client.get("/auth/telegram", params=data, follow_redirects=False)
    assert bad.status_code == 422


def test_sessions_revoke(client, db):
    c1 = login(client, 8080)
    c2 = login(client, 8080)
    c1.post("/cabinet/onboarding", data={"name": "Сессии", "csrf": csrf(c1, "/cabinet")})
    c1.post("/cabinet/sessions/revoke", data={"others": "1", "csrf": csrf(c1, "/cabinet/profile")})
    assert c2.get("/cabinet", follow_redirects=False).status_code == 303
    assert c1.get("/cabinet").status_code == 200


_ = (PaymentRequest, tariffs)
