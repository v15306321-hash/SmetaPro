"""Сценарии бота через настоящий Dispatcher с подменённой сессией Telegram API.
Бот работает только кнопками мини-меню (ReplyKeyboard) — тесты нажимают их как пользователь."""
import asyncio
import itertools
from datetime import datetime

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import (Chat, Document, InlineKeyboardMarkup, Message, MessageId, PhotoSize,
                           ReplyKeyboardMarkup, Update, User as TgUser)

from app.bot.core import BTN_NEW, BTN_SUB, HOME, SKIP, TO_ORDER_LONG
from app.bot.runner import build, deliver_notifications
from app.models import Enterprise, Order, PaymentRequest, PaymentStatus, Subscription
from app.services import tariffs

_ids = itertools.count(1)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod] = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        ret = method.__returning__
        if ret is bool:
            return True
        if ret is MessageId:
            return MessageId(message_id=next(_ids))
        chat_id = getattr(method, "chat_id", 1) or 1
        return Message(message_id=next(_ids), date=datetime.now(), chat=Chat(id=chat_id, type="private"),
                       text=getattr(method, "text", None) or "ok")

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""

    async def close(self):
        pass

    def texts(self):
        return [(getattr(m, "text", None) or getattr(m, "caption", None) or "").replace("\xa0", " ")
                for m in self.calls]


class TgClient:
    def __init__(self, tg_id: int, name: str = "Тест"):
        self.bot, self.dp = build()
        self.session = FakeSession()
        self.bot.session = self.session
        self.user = TgUser(id=tg_id, is_bot=False, first_name=name)
        self.chat = Chat(id=tg_id, type="private")

    def _run(self, update: Update):
        asyncio.run(self.dp.feed_update(self.bot, update))

    def send(self, text: str | None = None, photo: str | None = None, document: str | None = None):
        msg = Message(message_id=next(_ids), date=datetime.now(), chat=self.chat, from_user=self.user, text=text,
                      photo=[PhotoSize(file_id=photo, file_unique_id=photo, width=10, height=10)] if photo else None,
                      document=Document(file_id=document, file_unique_id=document, mime_type="application/pdf",
                                        file_size=1000) if document else None)
        self._run(Update(update_id=next(_ids), message=msg))
        return self.last()

    def press(self, button: str) -> str:
        """Нажать кнопку мини-меню: проверяем, что такая кнопка действительно показана."""
        assert button in self.buttons(), f"нет кнопки {button!r}, есть: {self.buttons()}"
        return self.send(button)

    def last(self) -> str:
        texts = [t for t in self.session.texts() if t]
        return texts[-1] if texts else ""

    def buttons(self) -> list[str]:
        for m in reversed(self.session.calls):
            markup = getattr(m, "reply_markup", None)
            if isinstance(markup, ReplyKeyboardMarkup):
                return [b.text.replace("\xa0", " ") for row in markup.keyboard for b in row]
        return []

    def no_inline(self):
        assert not any(isinstance(getattr(m, "reply_markup", None), InlineKeyboardMarkup) for m in self.session.calls)


@pytest.fixture()
def owner(db):
    c = TgClient(31337, "Сардор")
    assert "Как оно называется" in c.send("/start")
    assert "телефон предприятия" in c.send("Цех Сардора")
    c.press(SKIP)
    assert "регистрирован" in c.press(SKIP)
    return c


def _activate(db, plan="PRO"):
    ent = db.query(Enterprise).first()
    tariffs.activate(db, ent.id, plan, None)
    db.commit()
    return ent


def test_new_calc_step_by_step(db, owner):
    """Сценарий из примера заказчика: поэтапный расчёт, добавление материала с размером листа и ценой."""
    c = owner
    _activate(db)
    assert "Новый расчет открыт" in c.press(BTN_NEW)
    assert "Укажите имя клиента" in c.last() and SKIP in c.buttons()
    assert "📞Укажите номер телефона клиента" in c.send("Ким Марина")
    assert "какое количество мебели" in c.send("+998 90 000 00 00")
    assert {"1", "10", "Другое число📝"} <= set(c.buttons())
    assert "мебель какого формата в №1" in c.press("2")
    assert {"Кухонный гарнитур", "Шкаф-купе", "Другое изделие📝"} <= set(c.buttons())
    card = c.press("Кухонный гарнитур")
    assert "Заказ №1 — Кухонный гарнитур" in card and "Позиций пока нет" in card and "Статус: Новый" in card
    assert {"📦Материалы", "🔩Фурнитура", "💵Настроить наценку", "🛠Работа цеха", "👥Добавить рабочих",
            "🛋Изделие №2", HOME, "↩️Назад"} <= set(c.buttons())

    # пустая база → предложение настроить
    assert "В вашей базе пока нет материалов" in c.press("📦Материалы")
    assert {"📝Настроить", "✅Приобрести настройку", "🔙Заказ"} <= set(c.buttons())
    assert "Введите название материала" in c.press("📝Настроить")
    assert "Укажите тип материала" in c.send("Акрил Темный асфальт")
    assert "ширина и высота у одного листа" in c.press("📦Корпусное покрытие")
    price_q = c.send("1220 × 2440 мм")
    assert "стоимость одного листа" in price_q and "✍Ввести вручную" in c.buttons()
    assert "Сколько листов «Акрил Темный асфальт» понадобится для «Кухонный гарнитур»" in c.send("660.000")
    added = c.press("3")
    assert "📦МАТЕРИАЛ №1" in added and "Размер: 1220 × 2440 мм" in added and "Стоимость: 660.000 сум" in added
    assert "✅Успешно сохранился" in added and "3 листа — 1.980.000 сум" in added
    assert {"📝Добавить ещё", TO_ORDER_LONG} <= set(c.buttons())
    card = c.press(TO_ORDER_LONG)
    assert "📦 Материалы: Акрил Темный асфальт (3 листа)." in card
    assert "Себестоимость: 1.980.000 сум" in card and "Итого клиенту: 2.574.000 сум" in card  # наценка 30%

    # второй материал: цена подсказывается кнопкой, размер листа — тоже
    c.press("📦Материалы")
    assert "Акрил Темный асфальт · 660.000/лист" in c.buttons()
    c.press("📝Добавить новый")
    c.send("Акрил Белый глянец")
    c.press("📦Корпусное покрытие")
    assert "1220 × 2440" in c.buttons()
    c.press("1220 × 2440")
    assert "660.000" in c.buttons()                      # прошлая цена — одной кнопкой
    c.press("660.000")
    assert "1,5 листа" in c.send("1,5")
    c.press(TO_ORDER_LONG)

    # фурнитура: тип не спрашивается, единица — кнопкой
    assert "нет фурнитуры" in c.press("🔩Фурнитура")
    c.press("📝Настроить")
    assert "В чём считать" in c.send("Петля Blum с доводчиком")
    c.press("шт")
    c.send("25.000")
    assert "🔩ФУРНИТУРА №1" in c.press("10")
    c.press(TO_ORDER_LONG)

    # работа цеха и рабочие
    c.press("🛠Работа цеха")
    c.press("Сборка и монтаж")
    assert "Работа «Сборка и монтаж» добавлена — 900.000 сум" in c.send("900.000")
    c.press("👥Добавить рабочих")
    c.press("2")
    assert "2 чел. × 300.000 сум = 600.000 сум" in c.send("300.000")

    # наценки заказа — по шагам
    c.press("💵Настроить наценку")
    c.press("0")
    c.press("0")
    c.press("10")
    card = c.press(SKIP)
    assert "Наценки: материалы 0% · фурнитура 0% · работа цеха 10%" in card
    # 1.980.000 + 990.000 + 250.000 + (900.000 + 600.000) × 1,1
    assert "Итого клиенту: 4.870.000 сум" in card

    # изделие №2
    assert "№2" in c.press("🛋Изделие №2")
    card = c.press("Шкаф-купе")
    assert "Заказ №1 — Кухонный гарнитур, Шкаф-купе" in card and "Шкаф-купе (изделие 2 из 2)" in card
    assert "🛋Сменить изделие" in c.buttons()

    # PDF, статус, удаление, расход
    c.press("📄PDF-смета")
    docs = [m for m in c.session.calls if type(m).__name__ == "SendDocument"]
    assert docs and "Смета EST-" in docs[-1].caption and "4.870.000" in docs[-1].caption.replace("\xa0", " ")
    c.press("🔄Статус")
    assert "Статус: В работе" in c.press("В работе")
    c.press("🗑Удалить позицию")
    remove_btn = next(b for b in c.buttons() if b.startswith("❌ 1."))
    assert "Позиция удалена" in c.press(remove_btn)
    c.press("♻️Расход и отходы")
    row = next(b for b in c.buttons() if b.startswith("✏️"))
    c.press(row)
    c.send("2")
    assert "Сохранено" in c.send("0,5")

    # назад к списку заказов и в главное меню
    c.press("↩️Назад")
    c.press("↩️Назад")
    assert any(b.startswith("№1 · Ким Марина") for b in c.buttons())
    assert "Главное меню" in c.press(HOME)
    c.no_inline()

    o = db.query(Order).one()
    db.refresh(o)
    assert o.products == ["Кухонный гарнитур", "Шкаф-купе"] and o.products_planned == 2


def test_menu_button_interrupts_dialog(db, owner):
    c = owner
    _activate(db)
    c.press(BTN_NEW)
    c.send("Клиент")
    assert "Ваши заказы" in c.send("📋 Заказы") or "Заказов пока нет" in c.last()


def test_payment_flow_and_admin_review(db, owner):
    c = owner
    assert "Подписки ещё нет" in c.press(BTN_SUB)
    start_btn = next(b for b in c.buttons() if b.startswith("START — 149.000"))
    assert "Выберите способ оплаты" in c.press(start_btn)
    text = c.press("HUMO")
    assert "9860 0101 4036 3283" in text and "Отправьте сюда фото или PDF чека" in text
    assert "Жду фото" in c.send("просто текст")
    assert "Чек получен" in c.send(photo="RECEIPT1")
    db.expire_all()
    pr = db.query(PaymentRequest).one()
    assert pr.status == PaymentStatus.pending and pr.receipt_file_id == "RECEIPT1"

    admin = TgClient(999, "Админ")
    admin.send("/admin")
    pay_btn = next(b for b in admin.buttons() if b.startswith(pr.uid))
    admin.press(pay_btn)
    assert any(getattr(m, "photo", None) == "RECEIPT1" for m in admin.session.calls)
    assert "подтверждена" in admin.press("✅ Подтвердить")
    db.expire_all()
    assert db.query(Subscription).count() == 1
    assert tariffs.current_subscription(db, pr.enterprise_id).plan_code == "START"
    admin.no_inline()

    sess = FakeSession()
    c.bot.session = sess
    asyncio.run(deliver_notifications(c.bot))
    assert any("подтверждена" in t for t in sess.texts())
    assert asyncio.run(deliver_notifications(c.bot)) == 0


def test_setup_service_request(db, owner):
    """«✅Приобрести настройку»: каталог пересылается администраторам, учитывается лимит тарифа."""
    c = owner
    _activate(db, "START")
    admin = TgClient(999, "Админ")
    admin.send("/start")  # создаёт пользователя-суперадмина
    c.press(BTN_NEW)
    c.press(SKIP)
    c.press(SKIP)
    c.press("1")
    c.press("Стол")
    c.press("📦Материалы")
    text = c.press("✅Приобрести настройку")
    assert "100.000 сум" in text and "до 80 позиций" in text
    assert "Пока ничего не получено" in c.press("✅ Готово, всё отправил")
    assert "Получено" in c.send(document="CATALOG1")
    copies = [m for m in c.session.calls if type(m).__name__ == "CopyMessage"]
    assert copies and copies[0].chat_id == 999
    assert "Заявка на настройку отправлена" in c.press("✅ Готово, всё отправил")


def test_expenses_step_by_step(db, owner):
    c = owner
    _activate(db, "BUSINESS")
    c.send("💸 Затраты")
    c.press("➕ Добавить затрату")
    assert "Сумма затраты «Распил»" in c.press("Распил")
    assert "К какому заказу" in c.send("150.000")
    assert "Комментарий" in c.press(SKIP)
    assert "Затрата «Распил» 150.000 сум сохранена" in c.press(SKIP)
    c.press("🏷 Новая категория")
    assert "«Сборка» и «Монтаж»" in c.send("Сборка")
    c.no_inline()


def test_paid_features_blocked_without_subscription(db, owner):
    c = owner
    assert "подписка" in c.press(BTN_NEW).lower()
    c.send("💸 Затраты")
    assert "подписка" in c.last().lower() or "недоступно" in c.last().lower()


def test_settings_markups_step_by_step(db, owner):
    c = owner
    _activate(db)
    c.send("⚙️ Настройки")
    c.press("💵 Наценки")
    c.press("40")
    c.press("20")
    c.send("15")
    assert "Наценки сохранены" in c.send("50.000")
    assert "материалы 40% · фурнитура 20% · работа цеха 15%" in c.last()
    assert "Фиксированная доплата: 50.000 сум" in c.last()


def test_duplicate_update_ignored(db, owner):
    c = owner
    msg = Message(message_id=1, date=datetime.now(), chat=c.chat, from_user=c.user, text="/menu")
    upd = Update(update_id=777777, message=msg)
    before = len(c.session.calls)
    asyncio.run(c.dp.feed_update(c.bot, upd))
    mid = len(c.session.calls)
    asyncio.run(c.dp.feed_update(c.bot, upd))
    assert mid > before and len(c.session.calls) == mid


def test_invite_and_login_deeplinks(db, owner):
    from app.services import auth_web
    _activate(db, "BUSINESS")
    owner.send("👥 Сотрудники")
    link = owner.press("➕ Менеджер")
    code = link.split("start=inv_")[1].split()[0]
    worker = TgClient(4242, "Менеджер")
    worker.send(f"/start inv_{code}")
    assert any("присоединились" in t for t in worker.session.texts())
    worker.send(BTN_SUB)
    assert "владелец" in worker.last()

    token, poll = auth_web.create_login_request(db)
    db.commit()
    worker.send(f"/start login_{token}")
    assert "Вход подтверждён" in worker.press("✅ Подтвердить вход")
    db.expire_all()
    assert auth_web.poll_login(db, token, poll).telegram_id == 4242
    worker.no_inline()


def test_background_is_event_driven(db, monkeypatch):
    """Без действий пользователей фоновая задача не обращается к БД (бесплатная БД может спать),
    а после kick() уведомления доставляются сразу."""
    from sqlalchemy import event

    from app import db as dbmod
    from app.bot import runner
    from app.config import get_settings
    from app.services import accounts
    from app.services.audit import notify

    monkeypatch.setattr(get_settings(), "jobs_interval_seconds", 3600)
    user, _ = accounts.get_or_create_telegram_user(db, 5555, full_name="Тест")
    db.commit()
    c = TgClient(5555)
    queries = []
    listener = lambda *a, **k: queries.append(1)  # noqa: E731

    async def scenario():
        task = asyncio.create_task(runner.background(c.bot))
        await asyncio.sleep(0.5)
        event.listen(dbmod.engine, "before_cursor_execute", listener)
        await asyncio.sleep(2)
        idle = len(queries)
        with dbmod.session_scope() as s:
            notify(s, user.id, "Событие")
        runner.kick()
        await asyncio.sleep(1.8)
        event.remove(dbmod.engine, "before_cursor_execute", listener)
        task.cancel()
        return idle

    assert asyncio.run(scenario()) == 0
    assert any(getattr(m, "text", "") == "Событие" for m in c.session.calls)
