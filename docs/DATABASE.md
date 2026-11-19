# Структура базы данных

Схема — `app/models.py`, миграции — `alembic/versions/`. Время в UTC, деньги — `NUMERIC(16,2)` в сумах.

| Таблица | Назначение | Ключевые поля |
|---|---|---|
| `users` | пользователь Telegram | `telegram_id` (уник.), `referral_code` (уник.), `referred_by_id`, `active_enterprise_id`, `is_superadmin`, `is_blocked` |
| `enterprises` | предприятие — рабочая область | реквизиты для смет, `owner_id`, наценки по умолчанию, `order_seq`, `logo_path`, блокировка |
| `memberships` | пользователь ↔ предприятие | `role` (owner/admin/master/manager), `permissions` (JSON, для администратора), `is_active`; уник. (user, enterprise) |
| `invitations` | одноразовые приглашения сотрудников | `code`, `role`, `expires_at`, `used_by_id` |
| `plans` | тарифы | цена, лимиты смет/материалов/пользователей, `features` (JSON) |
| `subscriptions` | периоды подписки (история) | `plan_code`, `starts_at`, `ends_at`, `estimates_used`, `payment_id`, `superseded_at` |
| `payment_methods` | способы оплаты и реквизиты | `code`, `details`, `holder`, `kind` (manual/crypto), `is_active` |
| `payment_requests` | заявки на оплату | `uid`, предприятие, тариф, способ, `base_amount`, `discount_percent`, `amount`, `referral_ids`, чек (`receipt_file_id`/`receipt_path`), `status`, причина отклонения, кто и когда проверил, `refunded` |
| `referrals` | реферальная связь | `referrer_id`, `referred_user_id` (уник.), `referred_enterprise_id` (уник. — предприятие засчитывается один раз), `first_payment_id`, `used_in_payment_id`, `status`, `suspicious` |
| `materials` | база материалов/фурнитуры | `kind`, `category`, `name`, `unit`, `price`, `attrs`, `is_archived` |
| `orders` | заказы | номер внутри предприятия, клиент, `status`, снимок наценок, кэш итогов |
| `order_items` | позиции заказа | `section`, `kind`, `material_id`, размеры, `pieces`, `quantity`, `unit_cost`, `cost`, `price` |
| `order_status_history` | история статусов | откуда → куда, кто, когда |
| `estimates` | сформированные PDF | `uid`, `order_id`, `subscription_id`, `total`, `file_path` |
| `expense_categories` | категории затрат | `enterprise_id` NULL — стандартные |
| `expenses` | производственные затраты | категория, сумма, дата, комментарий, `order_id` |
| `material_usage` | план/факт/отходы по заказу | `planned_qty`, `actual_qty`, `waste_qty`, `unit_cost` |
| `audit_log` | журнал действий | `actor_id`, `enterprise_id`, `action`, объект, `data` (JSON) |
| `notifications` | очередь уведомлений в Telegram | `dedup_key` (уник.), `sent_at`, `attempts`, `failed` |
| `web_sessions` | веб-сессии | `token_hash`, `expires_at`, `revoked_at`, браузер, IP |
| `login_requests` | вход на сайт через бота | `token`, `poll_secret_hash`, `confirmed_at`, `consumed_at` |
| `processed_updates` | дедупликация апдейтов Telegram | `update_id` (PK) |

## Жизненный цикл подписки

- Активна подписка с `starts_at ≤ сейчас < ends_at` и `superseded_at IS NULL`.
- Продление того же тарифа создаёт следующий период, начинающийся в момент окончания текущего.
- Смена тарифа: текущий и будущие периоды помечаются `superseded_at`, новый период начинается сразу.
- Истёкшие подписки не удаляются — это история.

## Статусы заявки на оплату

`awaiting_receipt` → (чек) → `pending` → `approved` | `rejected`; без чека можно `cancelled`.
Переходы выполняются условным `UPDATE` (защита от двойной обработки).

## Статусы реферала

`registered` (зарегистрировался по ссылке) → `qualified` (первая подтверждённая оплата предприятия) →
`used` (скидка применена в оплате пригласившего) | `annulled` (администратор/возврат).
Скидка пригласившего = 10 % × число `qualified`, максимум 50 %.
