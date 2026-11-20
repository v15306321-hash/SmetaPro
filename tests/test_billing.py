from decimal import Decimal

import pytest

from app.core.errors import Conflict, PermissionDenied
from app.models import PaymentStatus, ReferralStatus, Role
from app.services import accounts, payments, referrals, tariffs
from tests.conftest import _admin, add_member, make_enterprise, make_user, pay


def admin(db):
    return _admin(db) or make_user(db, "Админ", tg=999)


def test_manual_payment_flow_and_idempotency(db):
    ctx = make_enterprise(db, plan=None)
    a = admin(db)
    assert a.is_superadmin
    pr = payments.create_request(ctx, "BUSINESS", "CLICK")
    assert pr.amount == Decimal("349000") and pr.status == PaymentStatus.awaiting_receipt
    assert tariffs.current_subscription(db, ctx.eid) is None
    with pytest.raises(Conflict):
        payments.approve(db, a, pr.id)  # без чека не подтверждается
    payments.attach_receipt(ctx, pr.id, file_id="tg-file")
    assert tariffs.current_subscription(db, ctx.eid) is None  # до подтверждения не активно
    payments.approve(db, a, pr.id)
    sub = tariffs.current_subscription(db, ctx.eid)
    assert sub.plan_code == "BUSINESS" and sub.payment_id == pr.id
    with pytest.raises(Conflict):
        payments.approve(db, a, pr.id)  # повторное подтверждение исключено
    with pytest.raises(Conflict):
        payments.reject(db, a, pr.id, "x")
    assert len(tariffs.subscription_history(db, ctx.eid)) == 1


def test_reject(db):
    ctx = make_enterprise(db, plan=None)
    pr = payments.create_request(ctx, "START", "HUMO")
    payments.attach_receipt(ctx, pr.id, file_id="f")
    payments.reject(db, admin(db), pr.id, "Сумма не совпадает")
    assert pr.status == PaymentStatus.rejected and pr.reject_reason == "Сумма не совпадает"
    assert tariffs.current_subscription(db, ctx.eid) is None


def test_only_one_pending_request(db):
    ctx = make_enterprise(db, plan=None)
    pr = payments.create_request(ctx, "START", "HUMO")
    payments.attach_receipt(ctx, pr.id, file_id="f")
    with pytest.raises(Conflict):
        payments.create_request(ctx, "PRO", "HUMO")


def test_staff_cannot_pay(db):
    owner = make_enterprise(db, "PRO")
    mgr = add_member(db, owner, Role.manager)
    with pytest.raises(PermissionDenied):
        payments.create_request(mgr, "PRO", "HUMO")


def _invite_paid(db, referrer, n):
    for i in range(n):
        u = make_user(db, f"Друг{i}", ref_code=referrer.referral_code)
        accounts.register_enterprise(db, u, name=f"Цех {referrer.id}-{i}")
        pay(db, accounts.load_ctx(db, u), "START")


def test_referral_discount_ladder_and_reset(db):
    ref_ctx = make_enterprise(db, plan=None, name="Пригласивший")
    ref_user = ref_ctx.user
    for expected, n in ((10, 1), (20, 1), (30, 1), (40, 1), (50, 1), (50, 1)):
        _invite_paid(db, ref_user, n)
        assert referrals.available_discount(db, ref_user.id)[0] == expected

    pr = payments.create_request(ref_ctx, "PRO", "HUMO")
    assert pr.discount_percent == 50 and pr.amount == Decimal("374500.00")
    payments.attach_receipt(ref_ctx, pr.id, file_id="f")
    payments.approve(db, admin(db), pr.id)
    # скидка одноразовая, цикл сброшен (включая 6-го приглашённого)
    assert referrals.available_discount(db, ref_user.id) == (0, [])
    pr2 = payments.create_request(ref_ctx, "PRO", "HUMO")
    assert pr2.discount_percent == 0 and pr2.amount == Decimal("749000")
    # новый цикл — только новые клиенты
    _invite_paid(db, ref_user, 1)
    assert referrals.available_discount(db, ref_user.id)[0] == 10


def test_referral_abuse_rules(db):
    ref_ctx = make_enterprise(db, plan=None, phone="+998 90 123 45 67")
    ref_user = ref_ctx.user
    # самоприглашение игнорируется
    assert accounts.attach_referral(db, ref_user, ref_user.referral_code) is None
    # регистрация без оплаты не засчитывается
    u = make_user(db, "Без оплаты", ref_code=ref_user.referral_code)
    accounts.register_enterprise(db, u, name="Цех без оплаты")
    assert referrals.available_discount(db, ref_user.id)[0] == 0
    # первая оплата засчитывается, продление — нет
    ctx = accounts.load_ctx(db, u)
    pay(db, ctx, "START")
    assert referrals.available_discount(db, ref_user.id)[0] == 10
    pay(db, ctx, "START")
    assert referrals.available_discount(db, ref_user.id)[0] == 10
    # совпадение телефона — помечается подозрительным
    u2 = make_user(db, "Дубль", ref_code=ref_user.referral_code)
    accounts.register_enterprise(db, u2, name="Дубль", phone="90 123-45-67")
    from app.models import Referral
    r = db.query(Referral).filter_by(referred_user_id=u2.id).one()
    assert r.suspicious
    # один аккаунт — одно собственное предприятие
    with pytest.raises(Conflict):
        accounts.register_enterprise(db, u, name="Второе")


def test_referral_annul_and_refund(db):
    ref_ctx = make_enterprise(db, plan=None)
    _invite_paid(db, ref_ctx.user, 2)
    refs = referrals.cycle(db, ref_ctx.user.id)
    referrals.annul(db, admin(db), refs[0].id, "Мошенничество")
    assert referrals.available_discount(db, ref_ctx.user.id)[0] == 10
    pr = db.get(type(refs[1]), refs[1].id).first_payment_id
    payments.refund(db, admin(db), pr, "Возврат")
    assert referrals.available_discount(db, ref_ctx.user.id)[0] == 0
    assert refs[1].status == ReferralStatus.annulled


def test_concurrent_approval_activates_once(db):
    """Два администратора одновременно нажимают «Подтвердить» — подписка создаётся один раз."""
    import threading

    from app import db as dbmod
    from app.models import Subscription
    ctx = make_enterprise(db, plan=None)
    a = admin(db)
    pr = payments.create_request(ctx, "PRO", "HUMO")
    payments.attach_receipt(ctx, pr.id, file_id="f")
    db.commit()
    results, barrier = [], threading.Barrier(2)

    def worker():
        s = dbmod.SessionLocal()
        try:
            barrier.wait()
            payments.approve(s, s.get(type(a), a.id), pr.id)
            s.commit()
            results.append("ok")
        except Exception as exc:  # Conflict или блокировка SQLite
            s.rollback()
            results.append(type(exc).__name__)
        finally:
            s.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    db.expire_all()
    assert results.count("ok") == 1, results
    assert db.query(Subscription).filter_by(enterprise_id=ctx.eid).count() == 1


def test_promo_discount_not_combined_with_referral(db, monkeypatch):
    """Акция −50 % до 1 января; реферальная скидка не суммируется — берётся бо́льшая, накопление сохраняется."""
    from datetime import datetime

    from app.config import get_settings
    from app.services import promo
    s = get_settings()
    monkeypatch.setattr(s, "promo_percent", 50)
    monkeypatch.setattr(s, "promo_until", "2027-01-01")
    assert promo.active_percent(datetime(2026, 12, 31, 18, 0)) == 50     # 31.12 23:00 по Ташкенту
    assert promo.active_percent(datetime(2026, 12, 31, 19, 1)) == 0      # 1 января по Ташкенту
    assert promo.banner(datetime(2026, 11, 21)) == "🔥 Акция: до 1 января все тарифы со скидкой 50%!"

    ref_ctx = make_enterprise(db, plan=None, name="Пригласивший")
    _invite_paid(db, ref_ctx.user, 1)                                    # реферальная 10 % (оплата друга — по акции)
    pr = payments.create_request(ref_ctx, "PRO", "HUMO")
    assert pr.discount_percent == 50 and pr.amount == Decimal("374500.00") and not pr.referral_ids
    payments.attach_receipt(ref_ctx, pr.id, file_id="f")
    payments.approve(db, admin(db), pr.id)
    assert referrals.available_discount(db, ref_ctx.user.id)[0] == 10    # не сгорела

    monkeypatch.setattr(s, "promo_until", "2026-01-01")                  # акция закончилась
    pr2 = payments.create_request(ref_ctx, "PRO", "HUMO")
    assert pr2.discount_percent == 10 and pr2.referral_ids
