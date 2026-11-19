"""Сценарии бота через настоящий Dispatcher с подменённой сессией Telegram API."""
import asyncio
import itertools
from datetime import datetime

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Document, Message, PhotoSize, Update, User as TgUser

from app.bot.core import BTN_NEW, BTN_SUB
from app.bot.runner import build, deliver_notifications
from app.models import Order, PaymentRequest, PaymentStatus, Subscription
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
        chat_id = getattr(method, "chat_id", 1) or 1
        return Message(message_id=next(_ids), date=datetime.now(), chat=Chat(id=chat_id, type="private"),
                       text=getattr(method, "text", None) or "ok")

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""

    async def close(self):
        pass

    def texts(self):
        out = []
        for m in self.calls:
            out.append(getattr(m, "text", None) or getattr(m, "caption", None) or "")
        return out


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

    def click(self, data: str):
        msg = Message(message_id=next(_ids), date=datetime.now(), chat=self.chat, text="card")
        cq = CallbackQuery(id=str(next(_ids)), from_user=self.user, chat_instance="x", data=data, message=msg)
        self._run(Update(update_id=next(_ids), callback_query=cq))
        return self.last()

    def last(self) -> str:
        texts = [t for t in self.session.texts() if t]
        return texts[-1] if texts else ""

    def buttons(self) -> list[tuple[str, str]]:
        for m in reversed(self.session.calls):
            markup = getattr(m, "reply_markup", None)
            if markup is not None and hasattr(markup, "inline_keyboard"):
                return [(b.text, b.callback_data or b.url) for row in markup.inline_keyboard for b in row]
        return []


@pytest.fixture()
def owner(db):
    c = TgClient(31337, "Сардор")
    c.send("/start")
    assert "Как оно называется" in c.last()
    c.send("Цех Сардора")
    c.send("+998901112233")
    assert "регистрирован" in c.send("Ташкент")
    return c


def test_registration_and_payment_flow(db, owner):
    c = owner
    assert "Подписки ещё нет" in c.send(BTN_SUB)
    assert ("START — 149 000 сум", "sub:plan:START") in c.buttons()
    c.click("sub:plan:START")
    text = c.click("sub:pay:START:HUMO")
    assert "9860 0101 4036 3283" in text and "чека" in text
    c.send("просто текст")
    assert "Ожидаю фото" in c.last()
    c.send(photo="RECEIPT1")
    db.expire_all()
    pr = db.query(PaymentRequest).one()
    assert pr.status == PaymentStatus.pending and pr.receipt_file_id == "RECEIPT1"

    # суперадмин подтверждает в боте
    admin = TgClient(999, "Админ")
    admin.send("/admin")
    assert any(getattr(m, "photo", None) == "RECEIPT1" for m in admin.session.calls)
    admin.click(f"adm:ok:{pr.id}")
    admin.click(f"adm:ok:{pr.id}")  # повторное нажатие
    db.expire_all()
    assert db.query(Subscription).count() == 1
    assert tariffs.current_subscription(db, pr.enterprise_id).plan_code == "START"

    # доставка уведомлений
    sess = FakeSession()
    c.bot.session = sess
    asyncio.run(deliver_notifications(c.bot))
    assert any("подтверждена" in t for t in sess.texts())
    assert asyncio.run(deliver_notifications(c.bot)) == 0  # повторно не отправляется


def test_order_flow(db, owner):
    c = owner
    from app.models import Enterprise
    ent = db.query(Enterprise).one()
    tariffs.activate(db, ent.id, "PRO", None)
    db.commit()

    c.send("⚙️ Настройки")
    c.click("set:mk")
    c.send("30 20 10 50000")
    assert "материалы 30%" in c.last()

    c.send("📦 Материалы")
    c.click("m:add:material")
    c.send("ЛДСП")
    c.send("ЛДСП Egger 16")
    c.click("m:unit:м²")
    assert "Добавлено" in c.send("100000")

    c.send(BTN_NEW)
    c.send("Иван")
    c.send("-")
    card = c.send("Кухня")
    assert "Заказ №1" in card and "Позиций пока нет" in card
    oid = db.query(Order).one().id
    c.click(f"oi:pick:{oid}:material:0")
    mid = next(d for t, d in c.buttons() if d.startswith("oi:m:")).split(":")[3]
    c.click(f"oi:m:{oid}:{mid}")
    card = c.send("2000x1000x2")          # 4 м² × 100 000 = 400 000 → ×1.3 = 520 000
    assert "520 000" in card
    c.click(f"oi:work:{oid}")
    card = c.send("Сборка и монтаж; 300000")  # ×1.1 = 330 000
    assert "Итого клиенту: 900 000 сум" in card  # 520 000 + 330 000 + 50 000

    c.click(f"o:pdf:{oid}")
    docs = [m for m in c.session.calls if type(m).__name__ == "SendDocument"]
    assert docs and "Смета EST-" in docs[0].caption

    c.click(f"o:set:{oid}:in_progress")
    assert "В работе" in c.last()

    c.click(f"w:show:{oid}")
    assert "Расход по заказу" in c.last()
    uid = next(d for t, d in c.buttons() if d.startswith("w:edit:")).split(":")[3]
    c.click(f"w:edit:{oid}:{uid}")
    assert "отходы 1" in c.send("5 1")

    c.send("📊 Аналитика")
    assert "Выручка (расчётная): <b>900 000 сум</b>" in c.last()

    # кнопка меню прерывает незавершённый диалог
    c.send(BTN_NEW)
    c.send("📋 Заказы")
    assert "Заказы" in c.last()


def test_paid_features_blocked_without_subscription(db, owner):
    c = owner
    assert "подписка" in c.send(BTN_NEW).lower()
    c.send("💸 Затраты")
    assert "недоступно" in c.last().lower() or "подписка" in c.last().lower()


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
    from app.models import Enterprise
    from app.services import auth_web
    ent = db.query(Enterprise).one()
    tariffs.activate(db, ent.id, "BUSINESS", None)
    db.commit()
    owner.send("👥 Сотрудники")
    link = owner.click("st:inv:manager")
    code = link.split("start=inv_")[1].split()[0]
    worker = TgClient(4242, "Менеджер")
    worker.send(f"/start inv_{code}")
    assert any("присоединились" in t for t in worker.session.texts())
    worker.send("💳 Подписка")
    assert "владелец" in worker.last()

    token, poll = auth_web.create_login_request(db)
    db.commit()
    worker.send(f"/start login_{token}")
    assert ("✅ Подтвердить вход", f"login:{token}") in worker.buttons()
    worker.click(f"login:{token}")
    assert "Вход подтверждён" in worker.last()
    db.expire_all()
    assert auth_web.poll_login(db, token, poll).telegram_id == 4242
