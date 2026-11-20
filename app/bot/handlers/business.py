"""Аналитика, сотрудники, подписка и оплата, рефералы, настройки — кнопками мини-меню."""
from __future__ import annotations


from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardRemove

from app.bot.core import (BACK, BTN_ANALYTICS, BTN_LEAVE, BTN_REF, BTN_SETTINGS, BTN_STAFF, BTN_SUB, HOME, NO, SKIP,
                          YES, call,
                          e, grid, main_menu, read_money, read_qty, rk)
from app.config import get_settings
from app.core.errors import LimitReached
from app.core.fmt import date_str, money, qty
from app.services import accounts, employees, payments, referrals, tariffs

router = Router(name="business")

PCT = [["0", "10", "20", "30"], ["40", "50", "70", "100"]]


class St(StatesGroup):
    menu = State()
    remove = State()
    confirm = State()
    leave = State()


class Sub(StatesGroup):
    plans = State()
    method = State()
    receipt = State()


class Set(StatesGroup):
    menu = State()
    mk_work = State()
    mk_fixed = State()
    field = State()
    logo = State()


# ================================================================ аналитика

@router.message(F.text == BTN_ANALYTICS)
async def analytics_entry(message: Message, state: FSMContext):
    await state.clear()
    url = f"{get_settings().base_url.rstrip('/')}/cabinet/analytics"
    await message.answer(f"📊 <b>Аналитика</b> — с графиками в веб-кабинете:\n\n{url}\n\n"
                         "Войдите через Telegram — подтверждение придёт сюда.",
                         reply_markup=main_menu(), disable_web_page_preview=True)


# ================================================================ сотрудники

S_ADD = "➕ Добавить сотрудника"
S_FIRE = "🚫 Уволить сотрудника"
S_ACCESS = "🔑 Дать доступ к сметам"
OTHER_POSITION = "Другая должность📝"
PAY_PCT_BTN = "💯 Процент от заказа"
PAY_FIX_BTN = "💵 Фиксированная сумма"


class Emp(StatesGroup):
    position = State()
    name = State()
    pay_type = State()
    pay_rate = State()
    access = State()


@router.message(F.text == BTN_STAFF)
async def staff_entry(message: Message, state: FSMContext):
    await staff_menu(message, state)


async def staff_menu(message: Message, state: FSMContext, prefix: str = ""):
    await state.clear()

    def work(ctx):
        ctx.require("staff.view")
        emps = employees.list_active(ctx)
        us = tariffs.usage(ctx.db, ctx.eid)
        lines = ["👥 <b>Сотрудники</b>"]
        if us.get("users_limit"):
            lines.append(f"Доступ к боту: {us['users']} из {us['users_limit']} (включая вас)")
        lines.append("")
        if not emps:
            lines.append("Сотрудников пока нет.")
        for x in emps:
            access = " · 🔑 доступ к сметам" if x.user_id else ""
            lines.append(f"• {e(x.name)} — {e(x.position)} · {employees.pay_text(x)}{access}")
        no_access = [x for x in emps if not x.user_id]
        return "\n".join(lines), ctx.can("staff.manage"), bool(emps), bool(no_access)
    res = await call(message, work, keyboard=main_menu())
    if not res:
        return
    text, can_manage, has_emps, has_no_access = res
    rows = []
    if can_manage:
        rows.append([S_ADD])
        if has_no_access:
            rows.append([S_ACCESS])
        if has_emps:
            rows.append([S_FIRE])
    await state.set_state(St.menu)
    await message.answer((prefix + "\n\n" if prefix else "") + text, reply_markup=rk(*rows, [HOME]))


async def start_add_employee(message: Message, state: FSMContext, origin: str = "staff"):
    """Добавление сотрудника по шагам. origin="order" — после создания он сразу назначается на заказ."""
    await state.update_data(emp_origin=origin)
    await state.set_state(Emp.position)
    back = "🔙Заказ" if origin == "order" else BACK
    await message.answer("👤 Кого добавляем? Выберите должность:",
                         reply_markup=rk(*grid(employees.POSITIONS, 2), [OTHER_POSITION], [back]))


async def _emp_cancel(message: Message, state: FSMContext):
    if (await state.get_data()).get("emp_origin") == "order":
        from app.bot.handlers.calc import show_card
        await show_card(message, state)
    else:
        await staff_menu(message, state)


@router.message(St.menu, F.text == S_ADD)
async def staff_add(message: Message, state: FSMContext):
    await start_add_employee(message, state)


@router.message(Emp.position, F.text == OTHER_POSITION)
async def emp_position_other(message: Message):
    await message.answer("✍ Напишите должность, например: Сборщик")


@router.message(Emp.position, F.text)
async def emp_position(message: Message, state: FSMContext):
    if message.text == BACK:
        await _emp_cancel(message, state)
        return
    await state.update_data(emp_position=message.text.strip()[:80])
    await state.set_state(Emp.name)
    await message.answer("✅Отлично!\n\n✍ Как зовут сотрудника?", reply_markup=rk([BACK]))


@router.message(Emp.name, F.text)
async def emp_name(message: Message, state: FSMContext):
    if message.text == BACK:
        await start_add_employee(message, state, (await state.get_data()).get("emp_origin", "staff"))
        return
    name = message.text.strip()[:120]
    if len(name) < 2:
        await message.answer("Напишите имя сотрудника 🙂")
        return
    await state.update_data(emp_name=name)
    await state.set_state(Emp.pay_type)
    await message.answer(f"✅Отлично!\n\n💵 Как вы платите {e(name)}?",
                         reply_markup=rk([PAY_PCT_BTN], [PAY_FIX_BTN], [BACK]))


@router.message(Emp.pay_type, F.text)
async def emp_pay_type(message: Message, state: FSMContext):
    if message.text == BACK:
        await state.set_state(Emp.name)
        await message.answer("✍ Как зовут сотрудника?", reply_markup=rk([BACK]))
        return
    if message.text not in (PAY_PCT_BTN, PAY_FIX_BTN):
        await message.answer("Выберите способ оплаты кнопкой ниже 👇")
        return
    pct = message.text == PAY_PCT_BTN
    await state.update_data(emp_pay_type=employees.PAY_PERCENT if pct else employees.PAY_FIXED)
    await state.set_state(Emp.pay_rate)
    if pct:
        await message.answer("✅Отлично!\n\n💯 Какой процент от суммы заказа (итого клиенту)?",
                             reply_markup=rk(["3", "5", "7", "10"], ["15", "20", "25", "30"], [BACK]))
    else:
        await message.answer("✅Отлично!\n\n💵 Какую сумму платите за один заказ? Например 300.000",
                             reply_markup=rk([BACK]))


@router.message(Emp.pay_rate, F.text)
async def emp_pay_rate(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.text == BACK:
        await state.set_state(Emp.pay_type)
        await message.answer("💵 Как вы платите сотруднику?", reply_markup=rk([PAY_PCT_BTN], [PAY_FIX_BTN], [BACK]))
        return
    pct = data["emp_pay_type"] == employees.PAY_PERCENT
    v = (0 if message.text.strip() == "0" else read_qty(message.text)) if pct else read_money(message.text)
    if v is None or (pct and v > 100):
        await message.answer("Напишите процент от 0 до 100, например 10" if pct else "Напишите сумму цифрами, например 300.000")
        return
    await state.update_data(emp_pay_rate=str(v))
    await state.set_state(Emp.access)
    await message.answer(f"✅Отлично!\n\n🔑 Разрешить {e(data['emp_name'])} составлять сметы в боте?\n\n"
                         "Если да — получите ссылку для сотрудника. Он будет работать как ваш сотрудник "
                         "(подписка ему не нужна), а подписка, сотрудники и настройки ему недоступны.",
                         reply_markup=rk([YES, NO], [BACK]))


@router.message(Emp.access, F.text)
async def emp_access(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.text == BACK:
        await state.set_state(Emp.pay_type)
        await message.answer("💵 Как вы платите сотруднику?", reply_markup=rk([PAY_PCT_BTN], [PAY_FIX_BTN], [BACK]))
        return
    if message.text not in (YES, NO):
        await message.answer("Ответьте кнопкой «✅ Да» или «❌ Нет» 👇")
        return

    want_access = message.text == YES

    def work(ctx):
        emp = employees.create(ctx, name=data["emp_name"], position=data["emp_position"],
                               pay_type=data["emp_pay_type"], pay_rate=data["emp_pay_rate"])
        link, note = None, None
        if want_access:
            try:
                link = employees.grant_access(ctx, emp.id)
            except LimitReached as err:  # сотрудник сохранён, но без доступа к боту
                note = err.message
        return emp.id, employees.pay_text(emp), link, note
    res = await call(message, work)
    if not res:
        return
    emp_id, pay, link, note = res
    text = f"✅ Сотрудник добавлен: {e(data['emp_name'])} — {e(data['emp_position'])} · {pay}"
    if link:
        text += (f"\n\n🔗 Отправьте эту ссылку сотруднику:\n{link}\n\n"
                 "Ссылка одноразовая и действует 3 дня.")
    if note:
        text += f"\n\n⚠️ Доступ к сметам не выдан: {e(note)}"
    if data.get("emp_origin") == "order":
        from app.bot.handlers.calc import assign_worker
        await message.answer(text, disable_web_page_preview=True)
        await assign_worker(message, state, emp_id)
    else:
        await staff_menu(message, state, text)


@router.message(St.menu, F.text == S_ACCESS)
async def staff_access(message: Message, state: FSMContext):
    rows = await call(message, lambda ctx: [(x.id, f"{x.name} — {x.position}"[:60])
                                            for x in employees.list_active(ctx) if not x.user_id])
    if rows:
        await state.set_state(St.remove)
        await state.update_data(member_map={t: mid for mid, t in rows}, staff_action="access")
        await message.answer("🔑 Кому дать доступ к составлению смет?", reply_markup=rk(*[[t] for _, t in rows], [BACK]))


@router.message(St.menu, F.text == S_FIRE)
async def staff_fire(message: Message, state: FSMContext):
    rows = await call(message, lambda ctx: [(x.id, f"{x.name} — {x.position}"[:60]) for x in employees.list_active(ctx)])
    if rows:
        await state.set_state(St.remove)
        await state.update_data(member_map={t: mid for mid, t in rows}, staff_action="fire")
        await message.answer("🚫 Кого уволить?", reply_markup=rk(*[[t] for _, t in rows], [BACK]))


@router.message(St.remove, F.text)
async def staff_pick(message: Message, state: FSMContext):
    data = await state.get_data()
    eid = data.get("member_map", {}).get(message.text)
    if not eid:
        await staff_menu(message, state)
        return
    if data.get("staff_action") == "access":
        link = await call(message, lambda ctx: employees.grant_access(ctx, eid))
        if link:
            await staff_menu(message, state, f"🔗 Ссылка для {e(message.text)}:\n{link}\n\n"
                                             "Отправьте её сотруднику. Ссылка одноразовая и действует 3 дня.")
        return
    await state.update_data(member_id=eid)
    await state.set_state(St.confirm)
    await message.answer(f"Уволить {e(message.text)}? Доступ к боту будет закрыт, история заказов сохранится.",
                         reply_markup=rk([YES, NO]))


@router.message(St.confirm, F.text)
async def staff_fire_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.text == YES and await call(message, lambda ctx: employees.fire(ctx, data["member_id"]).id):
        await staff_menu(message, state, "✅ Сотрудник уволен.")
    else:
        await staff_menu(message, state)


@router.message(F.text == BTN_LEAVE)
async def leave_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(St.leave)
    await message.answer("🚪 Уйти из цеха? Доступ к расчётам этого предприятия будет закрыт.",
                         reply_markup=rk([YES, NO]))


@router.message(St.leave, F.text)
async def leave_confirm(message: Message, state: FSMContext):
    if message.text != YES:
        from app.bot.handlers.start import show_home
        await show_home(message, state)
        return
    name = await call(message, employees.leave)
    if name:
        await state.clear()
        await message.answer(f"Вы вышли из «{e(name)}».\n\nЧтобы зарегистрировать своё предприятие, отправьте /start.",
                             reply_markup=ReplyKeyboardRemove())


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
S_MARKUP = "💵 Наценка"
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
                f"<b>Стоимость работы цеха</b> (наценка на общую сумму): {qty(ent.markup_works)}%\n"
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
    await state.set_state(Set.mk_work)
    await message.answer("💵 Стоимость работы цеха — наценка на общую сумму материалов, фурнитуры и технических "
                         f"работ. Применяется к новым заказам.\n\nСейчас: {qty(cur[2])}%. Выберите или напишите:",
                         reply_markup=rk(*PCT, [BACK]))


@router.message(Set.mk_work, F.text)
async def set_mk_work(message: Message, state: FSMContext):
    v = await _read_pct(message, state)
    if v is not None:
        await state.update_data(mk_work=str(v))
        await state.set_state(Set.mk_fixed)
        cur = (await state.get_data())["mk_cur"]
        await message.answer(f"✅Отлично!\n\n💵 Фиксированная доплата к каждому заказу, сум (например замер). "
                             f"Сейчас: {money(cur[3])}.\nНапишите сумму или пропустите:",
                             reply_markup=rk([SKIP, "0"], [BACK]))


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
    if await call(message, lambda ctx: accounts.set_markups(ctx, materials=0, hardware=0, works=data["mk_work"],
                                                            fixed=fixed).id):
        await settings_menu(message, state, "✅ Сохранено! Применяется к новым заказам.")


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
