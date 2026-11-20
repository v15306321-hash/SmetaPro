import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="fe-test-")
# По умолчанию — SQLite; для проверки на PostgreSQL: TEST_DATABASE_URL=postgresql+psycopg://... pytest
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmp}/test.db"
os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN"
os.environ["SUPERADMIN_TELEGRAM_IDS"] = "999"
os.environ["APP_ENV"] = "test"
os.environ["DEV_LOGIN"] = "true"
os.environ["PROMO_PERCENT"] = "0"  # акция включается в отдельных тестах

import pytest  # noqa: E402

from app import db as dbmod  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import Role  # noqa: E402
from app.services import accounts, payments, tariffs  # noqa: E402
from app.services.seed import ensure_seed  # noqa: E402


@pytest.fixture()
def db():
    from sqlalchemy import text
    with dbmod.engine.connect() as conn:
        if conn.dialect.name == "sqlite":
            conn.execute(text("PRAGMA foreign_keys=OFF"))
        Base.metadata.drop_all(conn)
        conn.commit()
    Base.metadata.create_all(dbmod.engine)
    s = dbmod.SessionLocal()
    ensure_seed(s)
    s.commit()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


_tg = iter(range(1000, 10**6))


def make_user(db, name="Тест", ref_code=None, tg=None):
    user, _ = accounts.get_or_create_telegram_user(db, tg or next(_tg), username=None, full_name=name,
                                                   ref_code=ref_code)
    return user


def make_enterprise(db, plan: str | None = "START", name="Мастерская", phone=None, user=None):
    user = user or make_user(db)
    ent = accounts.register_enterprise(db, user, name=name, phone=phone)
    if plan:
        tariffs.activate(db, ent.id, plan, None)
    db.flush()
    return accounts.load_ctx(db, user)


def pay(db, ctx, plan="START", method="HUMO"):
    """Полный цикл оплаты: заявка → чек → подтверждение суперадмином."""
    admin = make_user(db, "Админ", tg=999) if not _admin(db) else _admin(db)
    pr = payments.create_request(ctx, plan, method)
    payments.attach_receipt(ctx, pr.id, file_id="file")
    payments.approve(db, admin, pr.id)
    return pr


def _admin(db):
    from app.models import User
    return db.query(User).filter_by(telegram_id=999).first()


def add_member(db, ctx, role=Role.master):
    from app.services import staff
    inv = staff.invite(ctx, role)
    u = make_user(db, "Сотрудник")
    staff.accept(db, u, inv.code)
    return accounts.load_ctx(db, u)

