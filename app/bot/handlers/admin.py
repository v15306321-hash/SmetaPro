"""Проверка оплат суперадминистратором прямо в Telegram."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.core import answer, call_db, e, kb
from app.config import get_settings
from app.core.errors import DomainError
from app.core.fmt import dt_str, money
from app.models import PaymentStatus, User  # noqa: F401
from app.services import admin as admin_svc
from app.services import payments

router = Router(name="admin")


class Rej(StatesGroup):
    reason = State()


def _admin(db, tg_id: int) -> User:
    # get_or_create применяет SUPERADMIN_TELEGRAM_IDS, даже если /start ещё не отправлялся
    from app.services.accounts import get_or_create_telegram_user
    user, _ = get_or_create_telegram_user(db, tg_id)
    return admin_svc.require_superadmin(user)


@router.message(Command("admin"))
async def admin_menu(message: Message):
    def work(db, tg_id):
        _admin(db, tg_id)
        st = admin_svc.stats(db)
        items = [(p.id, p.uid, p.enterprise.name, p.plan_code, money(p.amount), p.method.title,
                  p.discount_percent, p.receipt_file_id, dt_str(p.created_at))
                 for p in admin_svc.payments(db, PaymentStatus.pending, 20)]
        return st, items
    try:
        st, items = await call_db(work, message.from_user.id)
    except DomainError as err:
        await message.answer(f"⚠️ {e(err.message)}")
        return
    await message.answer(
        f"🛠 <b>Администрирование</b>\nПользователей: {st['users']} · предприятий: {st['enterprises']}\n"
        f"Активных подписок: {st['active_total']} · оплат за 30 дней: {money(st['revenue_30'])}\n"
        f"Заявок на проверке: <b>{st['pending_payments']}</b>",
        reply_markup=kb([("Админ-панель на сайте", f"{get_settings().base_url.rstrip('/')}/admin")]))
    for pid, uid, ent, plan, amount, method, disc, file_id, created in items:
        text = (f"💳 <b>{uid}</b> · {created}\n{e(ent)}\nТариф {plan} · <b>{amount}</b> · {e(method)}"
                + (f"\nРеферальная скидка {disc}%" if disc else ""))
        markup = kb([("✅ Подтвердить", f"adm:ok:{pid}"), ("❌ Отклонить", f"adm:no:{pid}")])
        try:
            if file_id:
                await message.answer_photo(file_id, caption=text, reply_markup=markup)
                continue
        except Exception:
            try:
                await message.answer_document(file_id, caption=text, reply_markup=markup)
                continue
            except Exception:
                pass
        await message.answer(text + "\n(чек загружен через сайт — откройте админ-панель)", reply_markup=markup)


async def _edit(cq: CallbackQuery, suffix: str):
    await cq.answer()
    try:
        if cq.message.caption is not None:
            await cq.message.edit_caption(caption=(cq.message.caption or "") + suffix, reply_markup=None)
        else:
            await cq.message.edit_text((cq.message.text or "") + suffix, reply_markup=None)
    except Exception:
        await cq.message.answer(suffix.strip())


@router.callback_query(F.data.startswith("adm:ok:"))
async def approve(cq: CallbackQuery):
    pid = int(cq.data.split(":")[2])

    def work(db, tg_id):
        pr = payments.approve(db, _admin(db, tg_id), pid)
        return pr.uid
    try:
        uid = await call_db(work, cq.from_user.id)
        await _edit(cq, f"\n\n✅ Подтверждено ({cq.from_user.full_name}). Подписка {uid} активирована.")
    except DomainError as err:
        await _edit(cq, f"\n\n⚠️ {err.message}")


@router.callback_query(F.data.startswith("adm:no:"))
async def reject_start(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Rej.reason)
    await state.update_data(payment_id=int(cq.data.split(":")[2]))
    await answer(cq, "Причина отклонения (будет отправлена пользователю), или «-»:")


@router.message(Rej.reason, F.text)
async def reject_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    reason = None if message.text.strip() == "-" else message.text.strip()

    def work(db, tg_id):
        return payments.reject(db, _admin(db, tg_id), data["payment_id"], reason).uid
    try:
        uid = await call_db(work, message.from_user.id)
        await message.answer(f"❌ Заявка {uid} отклонена, пользователь уведомлён.")
    except DomainError as err:
        await message.answer(f"⚠️ {e(err.message)}")
    await state.clear()
