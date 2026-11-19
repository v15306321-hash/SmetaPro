"""Аналитика, сотрудники, подписка и оплата, рефералы, настройки предприятия."""
from __future__ import annotations

import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.core import (BTN_ANALYTICS, BTN_REF, BTN_SETTINGS, BTN_STAFF, BTN_SUB, answer, call, e, kb,
                          parse_num)
from app.config import get_settings
from app.core.errors import ValidationError
from app.core.fmt import TASHKENT_OFFSET, date_str, money, qty
from app.core.permissions import ROLE_TITLES
from app.models import PaymentStatus, Role
from app.services import accounts, analytics, payments, referrals, staff, tariffs

router = Router(name="business")


class Biz(StatesGroup):
    period = State()
    receipt = State()
    markups = State()
    field = State()
    logo = State()


# ---------------------------------------------------------------- аналитика

@router.message(F.text == BTN_ANALYTICS)
async def analytics_menu(message: Message, state: FSMContext):
    await state.clear()
    await _analytics(message, "month")


@router.callback_query(F.data.startswith("an:"))
async def analytics_cb(cq: CallbackQuery, state: FSMContext):
    period = cq.data.split(":")[1]
    if period == "custom":
        await state.set_state(Biz.period)
        await answer(cq, "Введите период: <code>01.09.2026-30.09.2026</code>")
        return
    await _analytics(cq, period, edit=True)


@router.message(Biz.period, F.text)
async def analytics_custom(message: Message, state: FSMContext):
    m = re.match(r"^\s*(\d{2}\.\d{2}\.\d{4})\s*[-–—]\s*(\d{2}\.\d{2}\.\d{4})\s*$", message.text)
    if not m:
        await message.answer("Формат: <code>01.09.2026-30.09.2026</code>")
        return
    try:
        s = datetime.strptime(m.group(1), "%d.%m.%Y").date()
        en = datetime.strptime(m.group(2), "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Проверьте даты.")
        return
    await state.clear()
    await _analytics(message, "custom", s, en)


async def _analytics(event, period, start=None, end=None, edit=False):
    def work(ctx):
        today = (datetime.utcnow() + TASHKENT_OFFSET).date()
        s, en = analytics.resolve_period(period, today, start, end)
        x = analytics.summary(ctx, s, en)
        return (f"📊 <b>Аналитика</b> · {analytics.PERIODS[period]}: {date_str(s)} — {date_str(en)}\n\n"
                f"Заказов: {x['orders']} · средний чек {money(x['avg_check'])}\n"
                f"Выручка (расчётная): <b>{money(x['revenue'])}</b>\n"
                f"Себестоимость: {money(x['cost'])}\n"
                f"  • материалы и фурнитура: {money(x['materials_and_hardware'])}\n"
                f"  • работы цеха: {money(x['cost_works'])}\n"
                f"Валовая прибыль (доход от наценок): <b>{money(x['gross'])}</b> · {qty(x['gross_margin'])}%\n"
                f"Производственные затраты: {money(x['expenses'])}\n"
                f"Расчётный фин. результат: <b>{money(x['result'])}</b> · {qty(x['result_margin'])}%\n\n"
                "<i>Расчётный результат не является фактической чистой прибылью: налоги, аренда и зарплаты "
                "не учитываются.</i>")
    text = await call(event, work)
    if text:
        rows = [[("День", "an:day"), ("Неделя", "an:week"), ("Месяц", "an:month")],
                [("Год", "an:year"), ("📅 Период", "an:custom")],
                [("📈 Графики в веб-кабинете", f"{get_settings().base_url.rstrip('/')}/cabinet/analytics")]]
        await answer(event, text, kb(*rows), edit=edit)


# ---------------------------------------------------------------- сотрудники

@router.message(F.text == BTN_STAFF)
async def staff_menu(message: Message, state: FSMContext):
    await state.clear()
    await _staff(message)


async def _staff(event, edit=False):
    def work(ctx):
        ms = staff.members(ctx)
        us = tariffs.usage(ctx.db, ctx.eid)
        lines = [f"👥 <b>Сотрудники</b> · {us['users']}" + (f" из {us['users_limit']}" if us.get("users_limit") else "")]
        for m in ms:
            lines.append(f"• {e(m.user.full_name or m.user.telegram_username)} — {ROLE_TITLES[m.role]}")
        removable = [(m.id, m.user.full_name or str(m.user.telegram_id)) for m in ms if m.role != Role.owner]
        return "\n".join(lines), ctx.can("staff.manage"), removable
    res = await call(event, work)
    if not res:
        return
    text, can_manage, removable = res
    rows = []
    if can_manage:
        rows = [[("➕ Менеджер", "st:inv:manager"), ("➕ Мастер", "st:inv:master"), ("➕ Админ", "st:inv:admin")]]
        rows += [[(f"🚫 Отозвать: {n}"[:40], f"st:rm:{mid}")] for mid, n in removable]
    await answer(event, text + "\n\n<i>Права администратора настраиваются в веб-кабинете.</i>", kb(*rows), edit=edit)


@router.callback_query(F.data.startswith("st:inv:"))
async def staff_invite(cq: CallbackQuery):
    role = Role(cq.data.split(":")[2])
    link = await call(cq, lambda ctx: staff.invite_link(staff.invite(ctx, role)))
    if link:
        await answer(cq, f"🔗 Приглашение ({ROLE_TITLES[role].lower()}), действует 3 дня, одноразовое:\n{link}\n\n"
                         "Перешлите ссылку сотруднику.")


@router.callback_query(F.data.startswith("st:rm:"))
async def staff_remove(cq: CallbackQuery):
    if await call(cq, lambda ctx: (staff.remove(ctx, int(cq.data.split(":")[2])), True)[1]):
        await _staff(cq, edit=True)


# ---------------------------------------------------------------- подписка и оплата

@router.message(F.text == BTN_SUB)
async def sub_menu_msg(message: Message, state: FSMContext):
    await state.clear()
    await _sub(message)


@router.callback_query(F.data == "sub:menu")
async def sub_menu_cb(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await _sub(cq)


async def _sub(event, edit=False):
    def work(ctx):
        us = tariffs.usage(ctx.db, ctx.eid)
        sub = us.get("subscription")
        if sub:
            text = (f"💳 <b>Подписка</b>\nТариф <b>{us['plan'].title}</b> до {date_str(sub.ends_at)} "
                    f"(осталось {us['days_left']} дн.)\nСметы: {us['estimates_used']}/{us['estimates_limit']} · "
                    f"материалы: {us['materials']}/{us['materials_limit']} · пользователи: {us['users']}/{us['users_limit']}")
        else:
            last = tariffs.last_subscription(ctx.db, ctx.eid)
            text = ("💳 <b>Подписка</b>\n" + (f"🔒 Истекла {date_str(last.ends_at)}. Данные сохранены." if last
                                              else "Подписки ещё нет."))
        discount, _ = referrals.available_discount(ctx.db, ctx.user.id)
        plans = [(p.code, f"{p.title} — {money(p.price * (100 - discount) / 100)}") for p in tariffs.list_plans(ctx.db)]
        if discount:
            text += f"\n🎁 Ваша реферальная скидка на эту оплату: <b>{discount}%</b>"
        text += "\n\nТарифы на 30 дней:"
        return text, plans, ctx.can("billing.manage")
    res = await call(event, work)
    if not res:
        return
    text, plans, can_pay = res
    if not can_pay:
        await answer(event, text + "\n\nОплатой управляет владелец предприятия.", edit=edit)
        return
    rows = [[(t, f"sub:plan:{code}")] for code, t in plans]
    rows.append([("Сравнить тарифы", f"{get_settings().base_url.rstrip('/')}/pricing")])
    await answer(event, text, kb(*rows), edit=edit)


@router.callback_query(F.data.startswith("sub:plan:"))
async def sub_plan(cq: CallbackQuery):
    code = cq.data.split(":")[2]
    methods = await call(cq, lambda ctx: [(m.code, m.title) for m in payments.list_methods(ctx.db)])
    if methods:
        await answer(cq, f"Тариф <b>{e(code)}</b>. Способ оплаты:",
                     kb(*[[(t, f"sub:pay:{code}:{mc}")] for mc, t in methods], [("🔙 Назад", "sub:menu")]), edit=True)


@router.callback_query(F.data.startswith("sub:pay:"))
async def sub_pay(cq: CallbackQuery, state: FSMContext):
    _, _, plan, method = cq.data.split(":")

    def work(ctx):
        pr = payments.create_request(ctx, plan, method)
        disc = f"\nСкидка {pr.discount_percent}% (было {money(pr.base_amount)})" if pr.discount_percent else ""
        holder = f"\nПолучатель: {e(pr.method.holder)}" if pr.method.holder else ""
        return pr.id, (f"🧾 Заявка <b>{pr.uid}</b>\nТариф {pr.plan_code} · 30 дней\n"
                       f"Сумма к оплате: <b>{money(pr.amount)}</b>{disc}\n\n"
                       f"{e(pr.method.title)}: <code>{e(pr.method.details)}</code>{holder}\n\n"
                       "Переведите сумму и <b>отправьте сюда фото или PDF чека</b>. Подписка активируется после "
                       "проверки администратором.")
    res = await call(cq, work)
    if res:
        pid, text = res
        await state.set_state(Biz.receipt)
        await state.update_data(payment_id=pid)
        await answer(cq, text, kb([("❌ Отменить заявку", f"sub:cancel:{pid}")]), edit=True)


@router.message(Biz.receipt, F.photo | F.document)
async def sub_receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.document and message.document.file_size and message.document.file_size > 8 * 1024 * 1024:
        await message.answer("Файл больше 8 МБ. Отправьте фото или файл поменьше.")
        return
    if message.document and (message.document.mime_type or "") not in ("application/pdf", "image/jpeg", "image/png"):
        await message.answer("Поддерживаются фото, JPG, PNG или PDF.")
        return
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    if await call(message, lambda ctx: payments.attach_receipt(ctx, data["payment_id"], file_id=file_id).uid):
        await state.clear()


@router.message(Biz.receipt)
async def sub_receipt_wrong(message: Message):
    await message.answer("Ожидаю фото или файл чека. Чтобы выйти — /cancel.")


@router.callback_query(F.data.startswith("sub:cancel:"))
async def sub_cancel(cq: CallbackQuery, state: FSMContext):
    if await call(cq, lambda ctx: (payments.cancel(ctx, int(cq.data.split(":")[2])), True)[1]):
        await state.clear()
        await answer(cq, "Заявка отменена.", edit=True)


# ---------------------------------------------------------------- рефералы

@router.message(F.text == BTN_REF)
async def referral_menu(message: Message, state: FSMContext):
    await state.clear()

    def work(ctx):
        s = referrals.summary(ctx.db, ctx.user)
        hist = [r for r in s["history"]][:8]
        st = {"registered": "ждём первую оплату", "qualified": "засчитан ✅", "used": "скидка использована",
              "annulled": "аннулирован"}
        lines = [f"🎁 <b>Пригласить друга</b>\n\nВаша ссылка:\n{s['links']['telegram']}\n",
                 f"Засчитано в текущем цикле: <b>{s['qualified_in_cycle']}</b>",
                 f"Скидка на следующую оплату: <b>{s['discount']}%</b> (максимум {s['max_discount']}%)",
                 f"Всего успешных приглашений: {s['total_successful']}",
                 "\nКак это работает: владелец другого мебельного предприятия регистрируется по ссылке и впервые "
                 "оплачивает подписку — ваша скидка растёт на 10%. Скидка одноразовая; после использования цикл "
                 "начинается заново."]
        if hist:
            lines.append("\n<b>История</b>")
            for r in hist:
                name = r.referred_enterprise.name if r.referred_enterprise else "предприятие не создано"
                lines.append(f"• {e(name)} — {st[r.status.value]}")
        return "\n".join(lines), s["links"]["telegram"]
    res = await call(message, work)
    if res:
        text, link = res
        from urllib.parse import quote
        await message.answer(text, reply_markup=kb([("📤 Отправить другу",
                                                     f"https://t.me/share/url?url={quote(link)}&text="
                                                     f"{quote('Считаю сметы мебели в Furniture Estimate — попробуйте')}")]))


# ---------------------------------------------------------------- настройки

FIELDS = {"name": "Название", "phone": "Телефон", "address": "Адрес", "email": "E-mail", "tax_id": "ИНН"}


@router.message(F.text == BTN_SETTINGS)
async def settings_menu(message: Message, state: FSMContext):
    await state.clear()
    await _settings(message)


async def _settings(event, edit=False):
    def work(ctx):
        ent = ctx.enterprise
        return (f"⚙️ <b>Настройки</b>\n\n<b>{e(ent.name)}</b>\nТелефон: {e(ent.phone) or '—'}\n"
                f"Адрес: {e(ent.address) or '—'}\nE-mail: {e(ent.email) or '—'}\nИНН: {e(ent.tax_id) or '—'}\n\n"
                f"<b>Наценки</b> (на всех тарифах):\nматериалы {qty(ent.markup_materials)}% · "
                f"фурнитура {qty(ent.markup_hardware)}% · работы цеха {qty(ent.markup_works)}%\n"
                f"Фиксированная сумма: {money(ent.fixed_addition)}\n"
                f"Логотип в PDF: {'загружен' if ent.logo_path else 'нет'}"), ctx.can("settings.edit")
    res = await call(event, work)
    if not res:
        return
    text, can = res
    rows = []
    if can:
        rows = [[("📐 Наценки", "set:mk"), ("🖼 Логотип", "set:logo")],
                [(f"✏️ {t}", f"set:f:{k}") for k, t in list(FIELDS.items())[:3]],
                [(f"✏️ {t}", f"set:f:{k}") for k, t in list(FIELDS.items())[3:]]]
    await answer(event, text, kb(*rows), edit=edit)


@router.callback_query(F.data == "set:mk")
async def set_markups(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Biz.markups)
    await answer(cq, "Наценки по умолчанию для новых заказов:\n<code>материалы фурнитура работы фикс.сумма</code>\n"
                     "Пример: <code>35 25 15 0</code>")


@router.message(Biz.markups, F.text)
async def markups_saved(message: Message, state: FSMContext):
    nums = message.text.replace(";", " ").split()
    try:
        if len(nums) < 3:
            raise ValidationError("")
        v = [parse_num(x) for x in nums[:4]]
    except Exception:
        await message.answer("Нужно 3–4 числа, например <code>35 25 15 0</code>.")
        return
    if await call(message, lambda ctx: accounts.set_markups(ctx, materials=v[0], hardware=v[1], works=v[2],
                                                            fixed=v[3] if len(v) > 3 else None).id):
        await state.clear()
        await _settings(message)


@router.callback_query(F.data.startswith("set:f:"))
async def set_field(cq: CallbackQuery, state: FSMContext):
    field = cq.data.split(":")[2]
    await state.set_state(Biz.field)
    await state.update_data(field=field)
    await answer(cq, f"Новое значение — {FIELDS[field]}:")


@router.message(Biz.field, F.text)
async def field_saved(message: Message, state: FSMContext):
    data = await state.get_data()
    if await call(message, lambda ctx: accounts.update_enterprise(ctx, **{data["field"]: message.text}).id):
        await state.clear()
        await _settings(message)


@router.callback_query(F.data == "set:logo")
async def set_logo(cq: CallbackQuery, state: FSMContext):
    ok = await call(cq, lambda ctx: tariffs.require_feature(ctx.db, ctx.eid, "pdf_logo") and True)
    if ok:
        await state.set_state(Biz.logo)
        await answer(cq, "Отправьте логотип изображением (PNG или JPG, лучше на светлом фоне).")


@router.message(Biz.logo, F.photo | F.document)
async def logo_saved(message: Message, state: FSMContext, bot: Bot):
    file = message.photo[-1] if message.photo else message.document
    if getattr(file, "file_size", 0) and file.file_size > 8 * 1024 * 1024:
        await message.answer("Файл больше 8 МБ.")
        return
    buf = await bot.download(file.file_id)
    data = buf.read()
    if await call(message, lambda ctx: accounts.set_logo(ctx, data, "")):
        await state.clear()
        await message.answer("✅ Логотип сохранён и будет добавляться в PDF-сметы.")


_ = PaymentStatus
