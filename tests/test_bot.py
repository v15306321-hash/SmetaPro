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


@pytest.fixture(autouse=True)
def _fresh_dialogs():
    """Диспетчер общий для тестов — сбрасываем незавершённые диалоги (FSM) между тестами."""
    _, dp = build()
    dp.storage.storage.clear()
    yield


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
    assert "Себестоимость: 1.980.000 сум" in card and "Стоимость работы цеха (0%): 0 сум" in card
    assert "Итого клиенту: 1.980.000 сум" in card

    # второй материал: цена подсказывается кнопкой, размер листа — тоже
    c.press("📦Материалы")
    assert "Акрил Темный асфальт · 660.000/лист" in c.buttons()
    c.press("📝Добавить новый")
    c.send("Акрил Белый глянец")
    c.press("📦Корпусное покрытие")
    assert "1220 × 2440" in c.buttons()
    assert "ширину одного листа" in c.press("✍Свой размер")    # свой размер — по шагам
    assert "высоту листа" in c.send("1220")
    c.send("2440")
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

    # технические работы — без собственной наценки
    c.press("🛠Работа цеха")
    assert {"Распил", "Кромка", "Доставка", "Транспортировка"} <= set(c.buttons())
    c.press("Распил")
    assert "Работа «Распил» добавлена — 150.000 сум" in c.send("150.000")

    # стоимость работы цеха — одна наценка на общую сумму
    c.press("💵Настроить наценку")
    card = c.press("10")
    # 1.980.000 + 990.000 + 250.000 + 150.000 = 3.370.000; +10 % = 337.000
    assert "Себестоимость: 3.370.000 сум" in card and "Стоимость работы цеха (10%): 337.000 сум" in card
    assert "Итого клиенту: 3.707.000 сум" in card

    # рабочие — из сотрудников; нового можно добавить прямо из заказа
    assert "пока нет сотрудников" in c.press("👥Добавить рабочих")
    assert "Выберите должность" in c.press("➕ Новый сотрудник")
    assert {"Дизайнер", "Конструктор", "Менеджер", "Мастер", "Помощник мастера", "Бухгалтер",
            "Другая должность📝"} <= set(c.buttons())
    assert "Как зовут" in c.press("Мастер")
    assert "Как вы платите" in c.send("Бахтиёр")
    assert "Какой процент" in c.press("💯 Процент от заказа")
    assert "составлять сметы" in c.press("5")
    card = c.press("❌ Нет")
    assert "👥 Рабочие: Бахтиёр (Мастер) — 185.350 сум." in card     # 5 % от 3.707.000
    assert "Итого клиенту: 3.707.000 сум" in card                   # клиенту не добавляется
    assert "Оплата рабочим: 185.350 сум" in card and "Прибыль цеха: 151.650 сум" in card

    # изделие №2
    assert "№2" in c.press("🛋Изделие №2")
    card = c.press("Шкаф-купе")
    assert "Заказ №1 — Кухонный гарнитур, Шкаф-купе" in card and "Шкаф-купе (изделие 2 из 2)" in card
    assert "🛋Сменить изделие" in c.buttons()

    # PDF, статус, удаление, расход
    c.press("📄PDF-смета")
    docs = [m for m in c.session.calls if type(m).__name__ == "SendDocument"]
    assert docs and "Смета EST-" in docs[-1].caption and "3.707.000" in docs[-1].caption.replace("\xa0", " ")
    c.press("🔄Статус")
    assert "Статус: В работе" in c.press("В работе")
    c.press("🗑Удалить позицию")
    remove_btn = next(b for b in c.buttons() if b.startswith("❌ 1."))
    assert "Удалено из заказа" in c.press(remove_btn)
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
    assert "Стоимость работы цеха — наценка на общую сумму" in c.press("💵 Наценка")
    c.press("20")
    assert "Сохранено" in c.send("50.000")
    assert "Стоимость работы цеха</b> (наценка на общую сумму): 20%" in c.last()
    assert "Фиксированная доплата: 50.000 сум" in c.last()


def test_analytics_is_a_link(db, owner):
    c = owner
    assert "/cabinet/analytics" in c.send("📊 Аналитика") and "Выручка" not in c.last()


def test_duplicate_update_ignored(db, owner):
    c = owner
    msg = Message(message_id=1, date=datetime.now(), chat=c.chat, from_user=c.user, text="/menu")
    upd = Update(update_id=777777, message=msg)
    before = len(c.session.calls)
    asyncio.run(c.dp.feed_update(c.bot, upd))
    mid = len(c.session.calls)
    asyncio.run(c.dp.feed_update(c.bot, upd))
    assert mid > before and len(c.session.calls) == mid


def test_employee_access_and_leave(db, owner):
    """Сотрудник с доступом к сметам: ссылка → работает как сотрудник владельца → может уйти; владелец увольняет."""
    from app.services import auth_web
    _activate(db, "PRO")
    owner.send("👥 Сотрудники")
    owner.press("➕ Добавить сотрудника")
    owner.press("Другая должность📝")
    owner.send("Сборщик")
    owner.send("Анвар")
    owner.press("💵 Фиксированная сумма")
    owner.send("200.000")
    text = owner.press("✅ Да")
    assert "Сотрудник добавлен: Анвар — Сборщик · 200.000 сум за заказ" in text and "start=inv_" in text
    code = text.split("start=inv_")[1].split()[0]

    worker = TgClient(4242, "Анвар")
    worker.send(f"/start inv_{code}")
    assert any("присоединились" in t for t in worker.session.texts())
    assert set(worker.buttons()) == {"🧮 Новый расчёт", "📋 Заказы", "📦 Материалы", "🌐 Веб-кабинет",
                                     "👨‍💻 Тех. поддержка", "🌐 Язык", "🚪 Уйти из цеха"}  # без подписки и сотрудников
    assert "Новый расчет открыт" in worker.press("🧮 Новый расчёт")
    worker.send(BTN_SUB)
    assert "владелец" in worker.last()

    token, poll = auth_web.create_login_request(db)
    db.commit()
    worker.send(f"/start login_{token}")
    assert "Вход подтверждён" in worker.press("✅ Подтвердить вход")

    worker.send("🚪 Уйти из цеха")
    assert "Вы вышли из" in worker.press("✅ Да")
    owner.send("👥 Сотрудники")
    assert "Сотрудников пока нет" in owner.last()
    worker.no_inline()
    owner.no_inline()


def test_owner_fires_employee(db, owner):
    _activate(db, "START")
    owner.send("👥 Сотрудники")
    owner.press("➕ Добавить сотрудника")
    owner.press("Бухгалтер")
    owner.send("Нигора")
    owner.press("💯 Процент от заказа")
    owner.press("3")
    text = owner.press("✅ Да")                                      # START — только владелец
    assert "Сотрудник добавлен" in text and "Доступ к сметам не выдан: Достигнут лимит пользователей" in text
    owner.send("👥 Сотрудники")
    assert "Нигора — Бухгалтер · 3% от заказа" in owner.last()
    owner.press("🚫 Уволить сотрудника")
    owner.press("Нигора — Бухгалтер")
    assert "Сотрудник уволен" in owner.press("✅ Да")


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


def test_support_roundtrip(db, owner):
    admin = TgClient(999, "Админ")
    admin.send("/start")
    assert "Техническая поддержка" in owner.send("👨‍💻 Тех. поддержка")
    assert "отправлено в поддержку" in owner.send("Не формируется смета")
    to_admin = [m for m in admin.session.calls if "#u31337" in (getattr(m, "text", "") or "")]
    # сообщение ушло через бота-владельца сессии клиента; эмулируем ответ админа reply на него
    sent = [m for m in owner.session.calls if getattr(m, "chat_id", None) == 999]
    assert sent and "Не формируется смета" in sent[-1].text and "#u31337" in sent[-1].text
    reply_to = Message(message_id=5, date=datetime.now(), chat=admin.chat, text=sent[-1].text,
                       from_user=TgUser(id=1, is_bot=True, first_name="SmetaPro"))
    msg = Message(message_id=next(_ids), date=datetime.now(), chat=admin.chat, from_user=admin.user,
                  text="Нажмите «📄PDF-смета» ещё раз", reply_to_message=reply_to)
    admin._run(Update(update_id=next(_ids), message=msg))
    answer = [m for m in admin.session.calls if getattr(m, "chat_id", None) == 31337]
    assert answer and "Ответ поддержки" in answer[-1].text and "ещё раз" in answer[-1].text
    assert "Ответ отправлен" in admin.last()
    _ = to_admin


def test_delete_order(db, owner):
    c = owner
    _activate(db)
    c.press(BTN_NEW)
    c.press(SKIP)
    c.press(SKIP)
    c.press("1")
    c.press("Стол")
    assert "удалятся" in c.press("🗑Удалить заказ")
    assert "Заказ №1" in c.press("❌ Нет")
    c.press("🗑Удалить заказ")
    c.press("✅ Да")
    assert any("Заказ удалён" in t for t in c.session.texts())
    assert db.query(Order).count() == 0
