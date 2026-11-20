"""/admin — проверка оплат суперадминистратором прямо в Telegram (кнопками мини-меню)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app.bot.core import BACK, HOME, SKIP, call_db, e, main_menu, rk
from app.config import get_settings
from app.core.errors import DomainError
from app.core.fmt import dt_str, money
from app.models import PaymentRequest, PaymentStatus, User
from app.services import admin as admin_svc
from app.services import payments
from app.services.accounts import get_or_create_telegram_user

router = Router(name="admin")

APPROVE = "✅ Подтвердить"
REJECT = "❌ Отклонить"
REFRESH = "🔄 Обновить"


class Adm(StatesGroup):
    list = State()
    view = State()
    reason = State()


def _admin(db, tg_id: int) -> User:
    # get_or_create применяет SUPERADMIN_TELEGRAM_IDS, даже если /start ещё не отправлялся
    user, _ = get_or_create_telegram_user(db, tg_id)
    return admin_svc.require_superadmin(user)


@router.message(Command("admin"))
async def admin_entry(message: Message, state: FSMContext):
    await admin_list(message, state)


async def admin_list(message: Message, state: FSMContext, prefix: str = ""):
    await state.clear()

    def work(db, tg_id):
        _admin(db, tg_id)
        st = admin_svc.stats(db)
        items = [(p.id, f"{p.uid} · {money(p.amount, '').strip()} · {p.enterprise.name}"[:60])
                 for p in admin_svc.payments(db, PaymentStatus.pending, 30)]
        return st, items
    try:
        st, items = await call_db(work, message.from_user.id)
    except DomainError as err:
        await message.answer(f"⚠️ {e(err.message)}")
        return
    await state.set_state(Adm.list)
    await state.update_data(pay_map={t: pid for pid, t in items})
    text = (f"🛠 <b>Администрирование SmetaPro</b>\n\nПользователей: {st['users']} · предприятий: {st['enterprises']}\n"
            f"Активных подписок: {st['active_total']} · оплат за 30 дней: {money(st['revenue_30'])}\n"
            f"Заявок на проверке: <b>{st['pending_payments']}</b>\n\n"
            f"Админ-панель на сайте: {get_settings().base_url.rstrip('/')}/admin")
    text += "\n\nВыберите заявку для проверки 👇" if items else "\n\nНовых заявок нет ✅"
    await message.answer((prefix + "\n\n" if prefix else "") + text,
                         reply_markup=rk(*[[t] for _, t in items], [REFRESH, HOME]), disable_web_page_preview=True)


@router.message(Adm.list, F.text == REFRESH)
async def admin_refresh(message: Message, state: FSMContext):
    await admin_list(message, state)


@router.message(Adm.list, F.text)
async def admin_pick(message: Message, state: FSMContext):
    pid = (await state.get_data()).get("pay_map", {}).get(message.text)
    if not pid:
        await message.answer("Выберите заявку кнопкой ниже 👇")
        return

    def work(db, tg_id):
        _admin(db, tg_id)
        p = db.get(PaymentRequest, pid)
        return dict(uid=p.uid, ent=p.enterprise.name, plan=p.plan_code, amount=money(p.amount),
                    base=money(p.base_amount), disc=p.discount_percent, method=p.method.title,
                    file_id=p.receipt_file_id, web=bool(p.receipt_path), created=dt_str(p.created_at),
                    user=p.user.full_name, tg=p.user.telegram_id)
    try:
        p = await call_db(work, message.from_user.id)
    except DomainError as err:
        await message.answer(f"⚠️ {e(err.message)}")
        return
    await state.set_state(Adm.view)
    await state.update_data(payment_id=pid, payment_uid=p["uid"])
    text = (f"💳 <b>{p['uid']}</b> · {p['created']}\n{e(p['ent'])}\n{e(p['user'])} (ID {p['tg']})\n\n"
            f"Тариф {p['plan']} · <b>{p['amount']}</b> · {e(p['method'])}"
            + (f"\nРеферальная скидка {p['disc']}% (без скидки {p['base']})" if p["disc"] else ""))
    kb = rk([APPROVE, REJECT], [BACK])
    if p["file_id"]:
        try:
            await message.answer_photo(p["file_id"], caption=text, reply_markup=kb)
            return
        except Exception:
            try:
                await message.answer_document(p["file_id"], caption=text, reply_markup=kb)
                return
            except Exception:
                pass
    await message.answer(text + "\n\n📎 Чек загружен через сайт — откройте его в админ-панели.", reply_markup=kb)


@router.message(Adm.view, F.text == BACK)
async def admin_back(message: Message, state: FSMContext):
    await admin_list(message, state)


@router.message(Adm.view, F.text == APPROVE)
async def admin_approve(message: Message, state: FSMContext):
    pid = (await state.get_data())["payment_id"]

    def work(db, tg_id):
        return payments.approve(db, _admin(db, tg_id), pid).uid
    try:
        uid = await call_db(work, message.from_user.id)
        await admin_list(message, state, f"✅ {uid} подтверждена, подписка активирована. Клиент уведомлён.")
    except DomainError as err:
        await admin_list(message, state, f"⚠️ {e(err.message)}")


@router.message(Adm.view, F.text == REJECT)
async def admin_reject(message: Message, state: FSMContext):
    await state.set_state(Adm.reason)
    await message.answer("✍ Напишите причину отклонения — её увидит клиент. (Или пропустить)",
                         reply_markup=rk([SKIP], [BACK]))


@router.message(Adm.reason, F.text)
async def admin_reason(message: Message, state: FSMContext):
    if message.text == BACK:
        await admin_list(message, state)
        return
    pid = (await state.get_data())["payment_id"]
    reason = None if message.text == SKIP else message.text.strip()

    def work(db, tg_id):
        return payments.reject(db, _admin(db, tg_id), pid, reason).uid
    try:
        uid = await call_db(work, message.from_user.id)
        await admin_list(message, state, f"❌ {uid} отклонена, клиент уведомлён.")
    except DomainError as err:
        await admin_list(message, state, f"⚠️ {e(err.message)}")


_ = main_menu
