"""Аналитика, сотрудники, подписка и оплата, рефералы, настройки — кнопками мини-меню."""
from __future__ import annotations

import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app.bot.core import (BACK, BTN_ANALYTICS, BTN_REF, BTN_SETTINGS, BTN_STAFF, BTN_SUB, HOME, NO, SKIP, YES, call,
                          e, grid, main_menu, read_money, read_qty, rk)
from app.config import get_settings
from app.core.fmt import TASHKENT_OFFSET, date_str, money, qty
from app.core.permissions import ROLE_TITLES
from app.models import Role
from app.services import accounts, analytics, payments, referrals, staff, tariffs

router = Router(name="business")

PCT = [["0", "10", "20", "30"], ["40", "50", "70", "100"]]


class An(StatesGroup):
    menu = State()
    start = State()
    end = State()


class St(StatesGroup):
    menu = State()
    remove = State()
    confirm = State()


class Sub(StatesGroup):
    plans = State()
    method = State()
    receipt = State()


class Set(StatesGroup):
    menu = State()
    mk_mat = State()
    mk_hw = State()
    mk_work = State()
    mk_fixed = State()
    field = State()
    logo = State()


# ================================================================ аналитика

PERIOD_BUTTONS = {"День": "day", "Неделя": "week", "Месяц": "month", "Год": "year"}
CUSTOM = "📅 Свой период"


def analytics_kb():
    return rk(["День", "Неделя", "Месяц"], ["Год", CUSTOM], [HOME])


@router.message(F.text == BTN_ANALYTICS)
async def analytics_entry(message: Message, state: FSMContext):
    await state.clear()
    await _analytics(message, state, "month")


@router.message(An.menu, F.text.in_(set(PERIOD_BUTTONS)))
async def analytics_period(message: Message, state: FSMContext):
    await _analytics(message, state, PERIOD_BUTTONS[message.text])


@router.message(An.menu, F.text == CUSTOM)
async def analytics_custom(message: Message, state: FSMContext):
    await state.set_state(An.start)
    await message.answer("📅 С какой даты? Напишите в формате 01.09.2026", reply_markup=rk([BACK]))


def _date(text: str):
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", text)
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date()
    except ValueError:
        return None


@router.message(An.start, F.text)
async def analytics_start(message: Message, state: FSMContext):
    if message.text == BACK:
        await _analytics(message, state, "month")
        return
    d = _date(message.text)
    if not d:
        await message.answer("Не понял дату. Напишите так: 01.09.2026")
        return
    await state.update_data(an_start=d.isoformat())
    await state.set_state(An.end)
    await message.answer("✅Отлично!\n\n📅 По какую дату? Например 30.09.2026", reply_markup=rk([BACK]))


@router.message(An.end, F.text)
async def analytics_end(message: Message, state: FSMContext):
    if message.text == BACK:
        await analytics_custom(message, state)
        return
    d = _date(message.text)
    if not d:
        await message.answer("Не понял дату. Напишите так: 30.09.2026")
        return
    start = datetime.fromisoformat((await state.get_data())["an_start"]).date()
    await _analytics(message, state, "custom", start, d)


async def _analytics(message: Message, state: FSMContext, period, start=None, end=None):
    def work(ctx):
        today = (datetime.utcnow() + TASHKENT_OFFSET).date()
        s, en = analytics.resolve_period(period, today, start, end)
        x = analytics.summary(ctx, s, en)
        return (f"📊 <b>Аналитика</b> · {analytics.PERIODS[period]}: {date_str(s)} — {date_str(en)}\n\n"
                f"Заказов: {x['orders']} · средний чек {money(x['avg_check'])}\n"
                f"Выручка (расчётная): <b>{money(x['revenue'])}</b>\n"
                f"Себестоимость: {money(x['cost'])}\n"
                f"  • материалы и фурнитура: {money(x['materials_and_hardware'])}\n"
                f"  • работа цеха: {money(x['cost_works'])}\n"
                f"Валовая прибыль (доход от наценок): <b>{money(x['gross'])}</b> · {qty(x['gross_margin'])}%\n"
                f"Производственные затраты: {money(x['expenses'])}\n"
                f"Расчётный фин. результат: <b>{money(x['result'])}</b> · {qty(x['result_margin'])}%\n\n"
                "<i>Расчётный результат не является фактической чистой прибылью: налоги, аренда и зарплаты "
                "не учитываются.</i>\n\n"
                f"📈 Графики — в веб-кабинете: {get_settings().base_url.rstrip('/')}/cabinet/analytics")
    text = await call(message, work, keyboard=main_menu())
    if text:
        await state.set_state(An.menu)
        await message.answer(text, reply_markup=analytics_kb(), disable_web_page_preview=True)


# ================================================================ сотрудники

INVITE = {"➕ Менеджер": Role.manager, "➕ Мастер": Role.master, "➕ Администратор": Role.admin}
REVOKE = "🚫 Отозвать доступ"


@router.message(F.text == BTN_STAFF)
async def staff_entry(message: Message, state: FSMContext):
    await staff_menu(message, state)


async def staff_menu(message: Message, state: FSMContext, prefix: str = ""):
    await state.clear()

    def work(ctx):
        ms = staff.members(ctx)
        us = tariffs.usage(ctx.db, ctx.eid)
        lines = [f"👥 <b>Сотрудники</b> · {us['users']}" + (f" из {us['users_limit']}" if us.get("users_limit") else "")]
        for m in ms:
            lines.append(f"• {e(m.user.full_name or m.user.telegram_username)} — {ROLE_TITLES[m.role]}")
        return "\n".join(lines), ctx.can("staff.manage"), any(m.role != Role.owner for m in ms)
    res = await call(message, work, keyboard=main_menu())
    if not res:
        return
    text, can_manage, has_staff = res
    rows = []
    if can_manage:
        rows = [["➕ Менеджер", "➕ Мастер"], ["➕ Администратор"]]
        if has_staff:
            rows.append([REVOKE])
    await state.set_state(St.menu)
    await message.answer((prefix + "\n\n" if prefix else "") + text +
                         "\n\n<i>Подробные права администратора настраиваются в веб-кабинете.</i>",
                         reply_markup=rk(*rows, [HOME]))


@router.message(St.menu, F.text.in_(set(INVITE)))
async def staff_invite(message: Message, state: FSMContext):
    role = INVITE[message.text]
    link = await call(message, lambda ctx: staff.invite_link(staff.invite(ctx, role)))
    if link:
        await message.answer(f"🔗 Приглашение: {ROLE_TITLES[role].lower()}\n\n{link}\n\n"
                             "Перешлите эту ссылку сотруднику. Она одноразовая и действует 3 дня.",
                             disable_web_page_preview=True)


@router.message(St.menu, F.text == REVOKE)
async def staff_revoke(message: Message, state: FSMContext):
    members = await call(message, lambda ctx: [(m.id, f"{m.user.full_name or m.user.telegram_id} — "
                                                      f"{ROLE_TITLES[m.role]}"[:60])
                                               for m in staff.members(ctx) if m.role != Role.owner])
    if members:
        await state.set_state(St.remove)
        await state.update_data(member_map={t: mid for mid, t in members})
        await message.answer("🚫 У кого отозвать доступ?", reply_markup=rk(*[[t] for _, t in members], [BACK]))


@router.message(St.remove, F.text)
async def staff_remove_pick(message: Message, state: FSMContext):
    mid = (await state.get_data()).get("member_map", {}).get(message.text)
    if not mid:
        await staff_menu(message, state)
        return
    await state.update_data(member_id=mid, member_name=message.text)
    await state.set_state(St.confirm)
    await message.answer(f"Отозвать доступ: {e(message.text)}?", reply_markup=rk([YES, NO]))


@router.message(St.confirm, F.text)
async def staff_remove_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.text == YES and await call(message, lambda ctx: (staff.remove(ctx, data["member_id"]), True)[1]):
        await staff_menu(message, state, "✅ Доступ отозван.")
    else:
        await staff_menu(message, state)


# ================================================================ подписка и оплата

CANCEL_PAY = "❌ Отменить заявку"


@router.message(F.text == BTN_SUB)
async def sub_entry(message: Message, state: FSMContext):
    await sub_menu(message, state)


async def sub_menu(message: Message, state: FSMContext, prefix: str = ""):
    await state.clear()

    def work(ctx):
        us = tariffs.usage(ctx.db, ctx.eid)
        sub = us.get("subscription")
        if sub:
            text = (f"💳 <b>Подписка</b>\n\nТариф <b>{us['plan'].title}</b> до {date_str(sub.ends_at)} "
                    f"(осталось {us['days_left']} дн.)\n"
                    f"Сметы: {us['estimates_used']} из {us['estimates_limit']}\n"
                    f"Материалы: {us['materials']} из {us['materials_limit']}\n"
                    f"Пользователи: {us['users']} из {us['users_limit']}")
        else:
            last = tariffs.last_subscription(ctx.db, ctx.eid)
            text = "💳 <b>Подписка</b>\n\n" + (f"🔒 Истекла {date_str(last.ends_at)}. Все данные сохранены."
                                              if last else "Подписки ещё нет.")
        discount, _ = referrals.available_discount(ctx.db, ctx.user.id)
        plans = {f"{p.title} — {money(p.price * (100 - discount) / 100, '').strip()} сум": p.code
                 for p in tariffs.list_plans(ctx.db)}
        if discount:
            text += f"\n\n🎁 Ваша реферальная скидка на эту оплату: <b>{discount}%</b>"
        return text, plans, ctx.can("billing.manage")
    res = await call(message, work, keyboard=main_menu())
    if not res:
        return
    text, plans, can_pay = res
    if not can_pay:
        await message.answer(text + "\n\nОплатой подписки управляет владелец предприятия.", reply_markup=main_menu())
        return
    await state.set_state(Sub.plans)
    await state.update_data(plan_map=plans)
    await message.answer((prefix + "\n\n" if prefix else "") + text +
                         f"\n\nВыберите тариф на 30 дней 👇\nСравнение тарифов: {get_settings().base_url.rstrip('/')}/pricing",
                         reply_markup=rk(*[[p] for p in plans], [HOME]), disable_web_page_preview=True)


@router.message(Sub.plans, F.text)
async def sub_plan(message: Message, state: FSMContext):
    code = (await state.get_data()).get("plan_map", {}).get(message.text)
    if not code:
        await message.answer("Выберите тариф кнопкой ниже 👇")
        return
    methods = await call(message, lambda ctx: {m.title: m.code for m in payments.list_methods(ctx.db)})
    if methods:
        await state.update_data(plan=code, method_map=methods)
        await state.set_state(Sub.method)
        await message.answer(f"✅Отлично! Тариф <b>{e(code)}</b>.\n\n💳 Выберите способ оплаты:",
                             reply_markup=rk(*grid(list(methods), 2), [BACK]))


@router.message(Sub.method, F.text)
async def sub_method(message: Message, state: FSMContext):
    if message.text == BACK:
        await sub_menu(message, state)
        return
    data = await state.get_data()
    method = data.get("method_map", {}).get(message.text)
    if not method:
        await message.answer("Выберите способ оплаты кнопкой ниже 👇")
        return

    def work(ctx):
        pr = payments.create_request(ctx, data["plan"], method)
        disc = f"\nСкидка {pr.discount_percent}% (без скидки {money(pr.base_amount)})" if pr.discount_percent else ""
        holder = f"\nПолучатель: {e(pr.method.holder)}" if pr.method.holder else ""
        return pr.id, (f"🧾 Заявка <b>{pr.uid}</b>\nТариф {pr.plan_code} · 30 дней\n\n"
                       f"💰 Сумма к оплате: <b>{money(pr.amount)}</b>{disc}\n\n"
                       f"{e(pr.method.title)}: <code>{e(pr.method.details)}</code>{holder}\n\n"
                       "1️⃣ Переведите сумму на эти реквизиты.\n"
                       "2️⃣ <b>Отправьте сюда фото или PDF чека.</b>\n\n"
                       "Подписка активируется после проверки администратором.")
    res = await call(message, work)
    if res:
        pid, text = res
        await state.set_state(Sub.receipt)
        await state.update_data(payment_id=pid)
        await message.answer(text, reply_markup=rk([CANCEL_PAY], [HOME]))


@router.message(Sub.receipt, F.photo | F.document)
async def sub_receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    doc = message.document
    if doc and doc.file_size and doc.file_size > 8 * 1024 * 1024:
        await message.answer("Файл больше 8 МБ. Отправьте фото или файл поменьше.")
        return
    if doc and (doc.mime_type or "") not in ("application/pdf", "image/jpeg", "image/png"):
        await message.answer("Подойдёт фото, JPG, PNG или PDF.")
        return
    file_id = message.photo[-1].file_id if message.photo else doc.file_id
    if await call(message, lambda ctx: payments.attach_receipt(ctx, data["payment_id"], file_id=file_id).uid):
        await state.clear()
        await message.answer("✅ Чек получен и отправлен на проверку. Как только администратор подтвердит оплату, "
                             "придёт уведомление.", reply_markup=main_menu())


@router.message(Sub.receipt, F.text == CANCEL_PAY)
async def sub_cancel(message: Message, state: FSMContext):
    data = await state.get_data()
    if await call(message, lambda ctx: (payments.cancel(ctx, data["payment_id"]), True)[1]):
        await sub_menu(message, state, "Заявка отменена.")


@router.message(Sub.receipt)
async def sub_receipt_wrong(message: Message):
    await message.answer("📎 Жду фото или файл чека. Отменить — «❌ Отменить заявку».")


# ================================================================ рефералы

@router.message(F.text == BTN_REF)
async def referral_menu(message: Message, state: FSMContext):
    await state.clear()

    def work(ctx):
        s = referrals.summary(ctx.db, ctx.user)
        st = {"registered": "ждём первую оплату", "qualified": "засчитан ✅", "used": "скидка использована",
              "annulled": "аннулирован"}
        lines = [f"🎁 <b>Пригласить друга</b>\n\nВаша ссылка:\n{s['links']['telegram']}\n",
                 f"Засчитано в текущем цикле: <b>{s['qualified_in_cycle']}</b>",
                 f"Скидка на следующую оплату: <b>{s['discount']}%</b> (максимум {s['max_discount']}%)",
                 f"Всего успешных приглашений: {s['total_successful']}",
                 "\nКак это работает: владелец другого мебельного предприятия регистрируется по вашей ссылке и "
                 "впервые оплачивает подписку — ваша скидка растёт на 10%. Скидка одноразовая; после использования "
                 "накопление начинается заново.\n\n📤 Просто перешлите ссылку выше в чат с другом."]
        hist = s["history"][:8]
        if hist:
            lines.append("\n<b>История</b>")
            for r in hist:
                name = r.referred_enterprise.name if r.referred_enterprise else "предприятие ещё не создано"
                lines.append(f"• {e(name)} — {st[r.status.value]}")
        return "\n".join(lines)
    text = await call(message, work)
    if text:
        await message.answer(text, reply_markup=main_menu(), disable_web_page_preview=True)


# ================================================================ настройки

FIELDS = {"✏️ Название": ("name", "Название предприятия"), "✏️ Телефон": ("phone", "Телефон"),
          "✏️ Адрес": ("address", "Адрес"), "✏️ E-mail": ("email", "E-mail"), "✏️ ИНН": ("tax_id", "ИНН")}
S_MARKUP = "💵 Наценки"
S_LOGO = "🖼 Логотип"


@router.message(F.text == BTN_SETTINGS)
async def settings_entry(message: Message, state: FSMContext):
    await settings_menu(message, state)


async def settings_menu(message: Message, state: FSMContext, prefix: str = ""):
    await state.clear()

    def work(ctx):
        ent = ctx.enterprise
        return (f"⚙️ <b>Настройки</b>\n\n<b>{e(ent.name)}</b>\nТелефон: {e(ent.phone) or '—'}\n"
                f"Адрес: {e(ent.address) or '—'}\nE-mail: {e(ent.email) or '—'}\nИНН: {e(ent.tax_id) or '—'}\n\n"
                f"<b>Наценки</b> для новых заказов:\nматериалы {qty(ent.markup_materials)}% · "
                f"фурнитура {qty(ent.markup_hardware)}% · работа цеха {qty(ent.markup_works)}%\n"
                f"Фиксированная доплата: {money(ent.fixed_addition)}\n"
                f"Логотип в PDF: {'загружен ✅' if ent.logo_path else 'нет'}"), ctx.can("settings.edit"), (
            ent.markup_materials, ent.markup_hardware, ent.markup_works, ent.fixed_addition)
    res = await call(message, work, keyboard=main_menu())
    if not res:
        return
    text, can, cur = res
    await state.set_state(Set.menu)
    await state.update_data(mk_cur=[str(x) for x in cur])
    rows = [[S_MARKUP, S_LOGO], ["✏️ Название", "✏️ Телефон"], ["✏️ Адрес", "✏️ E-mail"], ["✏️ ИНН"]] if can else []
    await message.answer((prefix + "\n\n" if prefix else "") + text, reply_markup=rk(*rows, [HOME]))


async def _pct(message: Message, state: FSMContext, st: State, title: str, current, lead: str = ""):
    await state.set_state(st)
    await message.answer(f"{lead}💵 {title}, %\n\nСейчас: {qty(current)}%. Выберите или напишите своё число:",
                         reply_markup=rk(*PCT, [BACK]))


async def _read_pct(message: Message, state: FSMContext):
    if message.text == BACK:
        await settings_menu(message, state)
        return None
    v = 0 if message.text.strip() == "0" else read_qty(message.text)
    if v is None or not 0 <= v <= 1000:
        await message.answer("Напишите процент числом от 0 до 1000, например 25")
        return None
    return v


@router.message(Set.menu, F.text == S_MARKUP)
async def set_mk(message: Message, state: FSMContext):
    cur = (await state.get_data())["mk_cur"]
    await _pct(message, state, Set.mk_mat, "Наценка на материалы", cur[0])


@router.message(Set.mk_mat, F.text)
async def set_mk_mat(message: Message, state: FSMContext):
    v = await _read_pct(message, state)
    if v is not None:
        await state.update_data(mk_mat=str(v))
        await _pct(message, state, Set.mk_hw, "Наценка на фурнитуру", (await state.get_data())["mk_cur"][1],
                   "✅Отлично!\n\n")


@router.message(Set.mk_hw, F.text)
async def set_mk_hw(message: Message, state: FSMContext):
    v = await _read_pct(message, state)
    if v is not None:
        await state.update_data(mk_hw=str(v))
        await _pct(message, state, Set.mk_work, "Наценка на работу цеха (сборка, монтаж и т.п.)",
                   (await state.get_data())["mk_cur"][2], "✅Отлично!\n\n")


@router.message(Set.mk_work, F.text)
async def set_mk_work(message: Message, state: FSMContext):
    v = await _read_pct(message, state)
    if v is not None:
        await state.update_data(mk_work=str(v))
        await state.set_state(Set.mk_fixed)
        cur = (await state.get_data())["mk_cur"]
        await message.answer(f"✅Отлично!\n\n💵 Фиксированная доплата к каждому заказу, сум. Сейчас: {money(cur[3])}.\n"
                             "Напишите сумму или пропустите:", reply_markup=rk([SKIP, "0"], [BACK]))


@router.message(Set.mk_fixed, F.text)
async def set_mk_fixed(message: Message, state: FSMContext):
    if message.text == BACK:
        await settings_menu(message, state)
        return
    fixed = None if message.text == SKIP else read_money(message.text)
    if message.text != SKIP and fixed is None:
        await message.answer("Напишите сумму цифрами, например 50.000")
        return
    data = await state.get_data()
    if await call(message, lambda ctx: accounts.set_markups(ctx, materials=data["mk_mat"], hardware=data["mk_hw"],
                                                            works=data["mk_work"], fixed=fixed).id):
        await settings_menu(message, state, "✅ Наценки сохранены! Они применяются к новым заказам.")


@router.message(Set.menu, F.text.in_(set(FIELDS)))
async def set_field(message: Message, state: FSMContext):
    key, title = FIELDS[message.text]
    await state.set_state(Set.field)
    await state.update_data(field=key)
    await message.answer(f"✍ Напишите новое значение — {title}:", reply_markup=rk([BACK]))


@router.message(Set.field, F.text)
async def field_saved(message: Message, state: FSMContext):
    if message.text == BACK:
        await settings_menu(message, state)
        return
    field = (await state.get_data())["field"]
    if await call(message, lambda ctx: accounts.update_enterprise(ctx, **{field: message.text}).id):
        await settings_menu(message, state, "✅ Сохранено!")


@router.message(Set.menu, F.text == S_LOGO)
async def set_logo(message: Message, state: FSMContext):
    if await call(message, lambda ctx: tariffs.require_feature(ctx.db, ctx.eid, "pdf_logo") and True):
        await state.set_state(Set.logo)
        await message.answer("🖼 Отправьте логотип картинкой (PNG или JPG, лучше на светлом фоне).",
                             reply_markup=rk([BACK]))


@router.message(Set.logo, F.photo | F.document)
async def logo_saved(message: Message, state: FSMContext, bot: Bot):
    file = message.photo[-1] if message.photo else message.document
    if getattr(file, "file_size", 0) and file.file_size > 8 * 1024 * 1024:
        await message.answer("Файл больше 8 МБ.")
        return
    data = (await bot.download(file.file_id)).read()
    if await call(message, lambda ctx: accounts.set_logo(ctx, data, "")):
        await settings_menu(message, state, "✅ Логотип сохранён — он появится в PDF-сметах.")


@router.message(Set.logo, F.text)
async def logo_back(message: Message, state: FSMContext):
    await settings_menu(message, state)
