"""Начальные справочники: тарифы, способы оплаты, стандартные категории затрат. Идемпотентно."""
from sqlalchemy.orm import Session

from app.models import PaymentMethod, Plan
from app.services.expenses import seed_standard
from app.services.tariffs import DEFAULT_PLANS

DEFAULT_METHODS = [
    dict(code="HUMO", title="HUMO", details="9860 0101 4036 3283", kind="manual", sort=1),
    dict(code="CLICK", title="CLICK", details="8802611240073151", kind="manual", sort=2),
    dict(code="UZUM", title="UZUM BANK", details="4916 9903 1742 4379", kind="manual", sort=3),
    # Сценарий CRYPTO PAY согласуется после получения API-данных — по умолчанию выключен
    dict(code="CRYPTO", title="CRYPTO PAY", details="", kind="crypto", sort=4, is_active=False),
]


def ensure_seed(db: Session) -> None:
    for p in DEFAULT_PLANS:
        if not db.get(Plan, p["code"]):
            db.add(Plan(**p))
    for m in DEFAULT_METHODS:
        if not db.get(PaymentMethod, m["code"]):
            db.add(PaymentMethod(**m))
    seed_standard(db)
    db.flush()
