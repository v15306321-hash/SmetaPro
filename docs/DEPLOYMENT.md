# Развёртывание и эксплуатация

## 1. Подготовка

1. Сервер Linux (2 vCPU / 2 ГБ RAM достаточно для старта), Docker + Docker Compose, nginx, certbot.
2. Домен, указывающий на сервер (A-запись).
3. Бот в @BotFather: получить токен, задать имя (`BOT_USERNAME`). Для Telegram Login Widget — `/setdomain`.
4. Узнать свой Telegram ID (например, у @userinfobot) — он нужен для `SUPERADMIN_TELEGRAM_IDS`.

## 2. Конфигурация

```bash
git clone <repo> /opt/furniture && cd /opt/furniture
cp .env.example .env
```

Обязательно заполнить в `.env`:

| Переменная | Значение |
|---|---|
| `APP_ENV` | `production` |
| `BASE_URL` | `https://ваш-домен` |
| `SECRET_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `POSTGRES_PASSWORD` | надёжный пароль БД |
| `BOT_TOKEN`, `BOT_USERNAME` | из @BotFather |
| `SUPERADMIN_TELEGRAM_IDS` | ID администраторов платформы через запятую |
| `SUPPORT_*` | контакты поддержки для страницы «Контакты» |
| `DEV_LOGIN` | `false` (приложение не запустится в production при `true`) |

Платёжные реквизиты HUMO / CLICK / UZUM BANK заполняются автоматически значениями из ТЗ и далее
редактируются в админ-панели («Тарифы и реквизиты») без изменения кода.

## 3. Запуск

```bash
docker compose up -d --build
docker compose logs -f web bot
```

Сервис `web` при каждом старте применяет миграции (`alembic upgrade head`) и заполняет справочники
(идемпотентно). Сервис `bot` получает апдейты Telegram (long polling) и доставляет уведомления.

## 4. nginx и HTTPS

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/furniture-estimate   # заменить example.uz на домен
sudo ln -s /etc/nginx/sites-available/furniture-estimate /etc/nginx/sites-enabled/
sudo certbot --nginx -d ваш-домен
sudo nginx -t && sudo systemctl reload nginx
```

## 5. Получение апдейтов Telegram

- **Polling (по умолчанию, рекомендуется для одного сервера):** `BOT_MODE=polling`. Не требует публичного
  webhook-адреса, процесс `bot` держит состояние диалогов.
- **Webhook:** `BOT_MODE=webhook`, задать `WEBHOOK_SECRET`. Процесс `bot` регистрирует
  `https://домен/telegram/webhook` и выполняет фоновые задачи, апдейты обрабатывает сервис `web`
  (запускать **одним** процессом uvicorn либо подключить Redis-хранилище FSM aiogram).

## 6. Резервное копирование и восстановление

```bash
# ежедневно в 03:15 — БД (pg_dump) и файлы (PDF, чеки, логотипы), хранение 14 дней
15 3 * * * /opt/furniture/deploy/backup.sh >> /var/log/furniture-backup.log 2>&1

# восстановление
./deploy/restore.sh /var/backups/furniture/db-YYYYMMDD-HHMM.dump /var/backups/furniture/storage-YYYYMMDD-HHMM.tgz
```

Рекомендуется дополнительно копировать каталог бэкапов во внешнее хранилище и периодически проверять
восстановление на тестовом сервере.

## 7. Обновление без потери данных

```bash
cd /opt/furniture
./deploy/backup.sh                 # копия перед обновлением
git pull
docker compose up -d --build       # миграции применятся при старте web
docker compose logs --tail=100 web bot
```

Миграции только добавляют/изменяют схему через Alembic; откат — `docker compose run --rm web alembic downgrade -1`
и восстановление из копии.

## 8. Мониторинг

- Доступность: внешний пинг `GET https://домен/robots.txt` (UptimeRobot и аналоги).
- Ошибки: логи `docker compose logs web bot` (необработанные ошибки пишутся с трассировкой); при желании
  подключить Sentry (`sentry-sdk[fastapi]`) — понадобится DSN.
- Бизнес-мониторинг: админ-панель → «Обзор» (регистрации, активные подписки, заявки на проверке).

## 9. Внешние сервисы и платные зависимости

| Сервис | Назначение | Стоимость |
|---|---|---|
| Telegram Bot API | бот, уведомления, вход на сайт | бесплатно |
| Сервер (VPS) | приложение и БД | по тарифу хостинга |
| Домен и SSL | сайт | домен — платно; Let's Encrypt — бесплатно |
| Google Fonts | шрифты сайта (Inter, Unbounded) | бесплатно (можно разместить локально) |
| CRYPTO PAY | приём криптоплатежей | комиссия провайдера; интеграция — после получения API-данных |

Все Python-библиотеки — open source (см. `requirements.txt`).
