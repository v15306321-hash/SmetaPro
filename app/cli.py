"""Служебные команды.

  python -m app.cli init-db              создать таблицы (только для разработки; в production — alembic upgrade head)
  python -m app.cli seed                 заполнить справочники (тарифы, реквизиты, категории)
  python -m app.cli make-admin <tg_id>   выдать права суперадминистратора
  python -m app.cli run-jobs             выполнить периодические задачи один раз
  python -m app.cli demo [--tg 1]        демо-предприятие с заказами за 7 месяцев (для показа/разработки)
"""
from __future__ import annotations

import argparse
import random
from datetime import date, timedelta

from app import db as dbmod
from app.models import ItemKind, OrderStatus, User, utcnow
from app.services import accounts, estimates, expenses, jobs, materials, orders, tariffs
from app.services.seed import ensure_seed


def cmd_init_db(_):
    dbmod.Base.metadata.create_all(dbmod.engine)
    with dbmod.session_scope() as db:
        ensure_seed(db)
    print("OK")


def cmd_seed(_):
    with dbmod.session_scope() as db:
        ensure_seed(db)
    print("OK")


def cmd_make_admin(a):
    with dbmod.session_scope() as db:
        user, _ = accounts.get_or_create_telegram_user(db, a.tg)
        user.is_superadmin = True
    print(f"Пользователь {a.tg} — суперадминистратор")


def cmd_run_jobs(_):
    with dbmod.session_scope() as db:
        n = jobs.subscription_reminders(db)
        jobs.cleanup(db)
    print(f"Уведомлений поставлено: {n}")


MATS = [
    (ItemKind.material, "ЛДСП", "ЛДСП Egger 16 мм белый", "м²", 92000),
    (ItemKind.material, "ЛДСП", "ЛДСП Kronospan 16 мм дуб", "м²", 98000),
    (ItemKind.material, "МДФ", "МДФ 18 мм под эмаль", "м²", 165000),
    (ItemKind.material, "МДФ", "МДФ фасад с патиной", "м²", 310000),
    (ItemKind.material, "Столешница", "Столешница HPL 38 мм", "м.п.", 420000),
    (ItemKind.material, "Кромка", "Кромка ПВХ 2 мм", "м.п.", 4500),
    (ItemKind.material, "Стекло", "Стекло тонированное 4 мм", "м²", 210000),
    (ItemKind.hardware, "Петли", "Петля Blum с доводчиком", "шт", 36000),
    (ItemKind.hardware, "Направляющие", "Направляющие Hettich 450 мм", "компл", 145000),
    (ItemKind.hardware, "Ручки", "Ручка-скоба 128 мм", "шт", 28000),
    (ItemKind.hardware, "Подъёмники", "Подъёмник Aventos HK", "компл", 690000),
]
PRODUCTS = [("Кухня", 1.0), ("Шкаф-купе", 0.8), ("Гардероб", 0.9), ("Детская", 0.6), ("Прихожая", 0.4)]
CLIENTS = ["Алишер Каримов", "Дилноза Юсупова", "Сардор Рахимов", "Нигора Ахмедова", "Тимур Саидов",
           "Малика Турсунова", "Бекзод Назаров", "Зарина Абдуллаева", "Рустам Исмоилов", "Шахноза Мирзаева"]


def cmd_demo(a):
    rnd = random.Random(42)
    with dbmod.session_scope() as db:
        ensure_seed(db)
        user, _ = accounts.get_or_create_telegram_user(db, a.tg, username="demo_owner", full_name="Сардор Умаров")
        ctx = accounts.load_ctx(db, user)
        if not ctx.enterprise:
            accounts.register_enterprise(db, user, name="Мебельный цех «Дуб и Ко»", phone="+998 90 123 45 67",
                                         address="Ташкент, Юнусабадский р-н, ул. Мебельщиков, 12")
            tariffs.activate(db, user.active_enterprise_id, "PRO", None)
            db.flush()
            ctx = accounts.load_ctx(db, user)
        accounts.set_markups(ctx, materials=35, hardware=25, works=15, fixed=0)
        ctx.enterprise.tax_id = "301234567"
        mats = [materials.create(ctx, kind=k, category=c, name=n, unit=u, price=p) for k, c, n, u, p in MATS]
        cats = expenses.categories(ctx)
        today = date.today()
        for i in range(48):
            days_ago = int(rnd.random() ** 1.4 * 205)
            product, scale = rnd.choice(PRODUCTS)
            o = orders.create(ctx, title=product, client_name=rnd.choice(CLIENTS),
                              client_phone=f"+998 9{rnd.randint(0, 9)} {rnd.randint(100, 999)} {rnd.randint(10, 99)} {rnd.randint(10, 99)}")
            for m in rnd.sample(mats[:7], 3):
                if m.unit == "м²":
                    orders.add_item(ctx, o.id, kind=m.kind, material_id=m.id, section=product,
                                    height_mm=rnd.choice([600, 720, 2100, 2400]), width_mm=rnd.choice([400, 600, 900]),
                                    pieces=max(1, int(rnd.randint(2, 8) * scale)))
                else:
                    orders.add_item(ctx, o.id, kind=m.kind, material_id=m.id, section=product,
                                    quantity=round(rnd.uniform(3, 12) * scale, 1))
            for m in rnd.sample(mats[7:], 2):
                orders.add_item(ctx, o.id, kind=m.kind, material_id=m.id, section=product,
                                pieces=max(1, int(rnd.randint(2, 16) * scale)))
            orders.add_item(ctx, o.id, kind=ItemKind.work, name="Сборка и монтаж", unit="услуга", section=product,
                            unit_cost=int(rnd.randint(4, 12) * 100000 * scale))
            created = utcnow() - timedelta(days=days_ago, hours=rnd.randint(0, 10))
            o.created_at = created
            if days_ago > 30:
                o.status = OrderStatus.completed
            elif days_ago > 12:
                o.status = rnd.choice([OrderStatus.manufactured, OrderStatus.completed])
            elif days_ago > 4:
                o.status = OrderStatus.in_progress
            if i % 3 == 0:
                expenses.add(ctx, category_id=rnd.choice(cats).id, amount=rnd.randint(3, 25) * 50000,
                             spent_on=today - timedelta(days=days_ago), order_id=o.id, comment="Демо")
        for _ in range(3):
            last = orders.list_orders(ctx, limit=3)[0]
            estimates.generate(ctx, last.id)
        print(f"Демо-предприятие готово. Telegram ID владельца: {a.tg}")


def main():
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db").set_defaults(fn=cmd_init_db)
    sub.add_parser("seed").set_defaults(fn=cmd_seed)
    s = sub.add_parser("make-admin")
    s.add_argument("tg", type=int)
    s.set_defaults(fn=cmd_make_admin)
    sub.add_parser("run-jobs").set_defaults(fn=cmd_run_jobs)
    s = sub.add_parser("demo")
    s.add_argument("--tg", type=int, default=1)
    s.set_defaults(fn=cmd_demo)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()


_ = User
