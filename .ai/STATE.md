# Состояние vk-bot

## Ветка feature/broadcasts, 2026-11-11

От master 410264c. Рассылки, аналитика и люди для CRM. Добавлены `005_broadcasts.sql`, `app/broadcasts/{settings,service}.py`, `repositories/broadcasts.py`, `routers/broadcasts.py`, модели в `schemas.py`, клиент `crm` в `deps.py`/`config.py`, задача в `main.py`, `CHAT_API_CRM_TOKEN` в `docker-compose.yml` и `.env.example`, тест `tests/test_broadcasts_db.py`. Проверки (Python 3.13, Postgres 16 через TEST_DATABASE_URL): `pytest tests -q` в `chat_api` — 90 passed, 0 skipped (было 81); `compileall app` без ошибок; повторный ensure_schema без ошибок. Сквозная проверка: uvicorn + Postgres + Redis локально, Bot CRM (vite preview, настоящий `api/chat.js`, GitHub замокан) — разовая рассылка на 5 человек tg ушла за проход, в `chat:broadcast:tg` 5 событий `notice`; рассылка после события создаётся и ставится на паузу. Не проверялось: деплой, Cloudflare Access до api.chatwithyou.site из Vercel, доставка ботами в Telegram/ВК, фото через S3.

Исправление после выкладки (2026-11-11): `deploy/update.sh` не переносил `CHAT_API_CRM_TOKEN` из ENV_FILE в `.env` сервера (переносит только известные ключи), CRM получала 401. Добавлен `optional_secret CHAT_API_CRM_TOKEN` и проверка, что токен отличается от PREMIUM_LINK_SECRET; кусок проверен локально (`sh -n`, перенос значения из ENV_FILE_B64 в .env).

Дополнение в той же ветке: `GET /analytics`, `GET /users*`, таблица `user_activity` (`006_user_activity.sql`), запись дня активности в `_remember`, тесты `test_users_db.py` и `test_analytics_db.py` (числа сверены с ручным расчётом, одноразовое заполнение из истории). `pytest tests -q` — 97 passed, 0 skipped. Сквозная проверка: 400 сгенерированных людей и 3578 диалогов в локальной базе, вкладка «Аналитика» в CRM в светлой и тёмной теме без ошибок. Не проверялось: время запросов аналитики и заполнения `user_activity` на боевом объёме данных.

## Ветка loop/account-page, 2026-11-10

Worktree `.worktrees/vk-bot-account-page`, от origin/master 274cee9. Изменены `premium_site/app/{billing,identity,main}.py`, `docker-compose.payments.yml`, `premium_site/DEPLOY.md` (раздел 11), добавлен `premium_site/tests/test_web_account.py`. Проверки (venv Python 3.10, pgserver): `pytest tests -q -rs` в `premium_site` — 89 passed, 0 skipped (было 75); `compileall premium_site/app` без ошибок; `docker compose -f docker-compose.yml -f docker-compose.payments.yml config -q` (с подставными значениями обязательных переменных окружения) и `git diff --check origin/master` без ошибок. Не проверялось: реальная ЮKassa (форма `payment_method.card` в ответе взята из документации), вызов из Worker, деплой. У подписок, оплаченных до выкладки, `card_last4` пуст до следующего платежа.

## Ветка loop/dialog-history, 2026-11-10

Worktree `.worktrees/vk-bot-dialog-history`, от origin/master 21a2a42. Добавлена история диалогов: `004_dialog_history.sql` (dialogs, messages, message_archives, chat_sessions.dialog_id), запись реплик в `ChatService`, суточный архиватор в S3 (`services/archive.py`, lock 7_310_003), настройки `MESSAGES_HOT_DAYS`/`MESSAGES_ARCHIVE_ENABLED`, счётчики в `deploy/update.sh` и `server-logs.yml`. Тесты без базы (`test_dialog_history.py`, `test_archive.py`) проходят вместе с прежними. Интеграционный `test_dialog_history_db.py` прогнан на временном Postgres 17 (Docker): весь набор chat_api — 55 passed, 0 skipped; повторный ensure_schema на непустой базе без ошибок, REVOKE снимает права anon/authenticated. На Windows тесту нужен селекторный цикл событий (задан в файле). Деплой не выполнялся.

## Ветка loop/reminders, 2026-11-10

Worktree `.worktrees/vk-bot-reminders`, от origin/master 21a2a42. Добавлены пакет `chat_api/app/reminders/`, `repositories/reminders.py`, таблица `reminder_log` в конце `001_schema.sql`, задача `reminders` в `main.py`, «Напоминания» в grep логов `deploy/update.sh`, тесты `test_reminders.py` и `test_reminders_db.py`. `ENABLED = False`, конфигурация `config.py`/`.env.example`/`docker-compose.yml` не менялась. Проверки (venv Python 3.10, pgserver): `compileall` без ошибок; `pytest tests -q -rs` в `chat_api` — 40 passed, 0 skipped (в т.ч. test_reminders_db на реальном Postgres через pgserver); `sh -n deploy/update.sh` без ошибок. Не проверялось: запуск compose целиком, реальная доставка через Redis/ботов, S3-загрузка фото, деплой. Остаточный риск: при обрыве Redis посреди пачки возможен дубль до BATCH_SIZE-1 сообщений; `reminder_log` без ретенции; web получает только онлайн-гость.

## Ветка loop/supabase-storage-media, 2026-11-09 (повтор после отката)

Медиа Chat API переключены на self-hosted Supabase Storage (`supabase/storage-api:v1.74.0`, S3-протокол, файловый бэкенд на томе `storage_data`), бакет `chat-media` создаётся пустым. Перенос из Beget убран: первый деплой завис на `rclone size` (Beget не работает), по решению пользователя старые медиа не переносятся. Для старых `media_id` `GET /media/{id}` отвечает 502 (запись в `stored_media` есть, объекта нет); 404 только без записи. `deploy/update.sh`: все сетевые шаги с `timeout`, ошибки печатаются; бакет создаётся до пересоздания chat-api; повторный деплой на уже созданных томе/ролях/схеме идемпотентен (проверено в Docker, два прогона шагов подряд). Остальное: `JWT_SECRET` убран из env db, демо JWT/`local-storage-*` отбрасываются, права anon/authenticated на таблицы storage выдают миграции storage-api. Сервер и update.sh целиком не проверялись.

## Ветка loop/payments-complete, 2026-11-04

Worktree `.worktrees/vk-bot-payments-complete`, от `payments` 5c0e1c1. Изменены только `premium_site/` (billing, main, identity, шаблоны, payment.css, тесты), `.env.example`, `premium_site/DEPLOY.md`, `.ai/`. База тестов: 38 passed; после изменений pytest premium_site на Postgres (pgserver): 57 passed, 0 skipped. Схема БД не менялась. Тесты с реальным тестовым магазином ЮKassa не запускались (нет ключей); отказ/неопределённый исход проверены на подставном провайдере и на мок-ответах httpx.

## Оркестрация Codex, 2026-10-31

Добавлены .codex/config.toml и пять ролей, AGENTS.md дополнен маршрутизацией и workflow. Luna/low — диспетчер и дешёвые роли; Sol/medium — developer; Astra (gpt-6-astra)/high — planner и reviewer (выбор подтверждён пользователем). 42 TOML-файла и 7 конфигураций прошли разбор и строгую проверку установленным CLI; независимый reviewer PASS. Существующие инструкции сохранены. Выполнен mobile typecheck; lint недоступен из-за исходно отсутствующей настройки ESLint. Реальный запуск ролей и смена модели текущего чата не тестировались.

Эта задача сохраняет .codex/, .ai/ и AGENTS.md локальным коммитом; hash брать из git log. Снимки незакоммиченного контекста ниже исторические. Прикладные изменения из исходного рабочего дерева не включаются. Общая .codex/ в корне workspace, как и общая .ai/, находится вне Git; дочерние репозитории имеют собственные версионируемые комплекты. Push не выполняется.

Снимок: **2026-10-31**. Ветка `master`, HEAD `e746076`; перед новой работой перепроверить.

## Исходное рабочее дерево

Untracked `.env.temp` и `rdap.json`. Их содержимое не анализировалось.

Это состояние до текущей документальной задачи. Новые .ai/ и изменения AGENTS.md относятся к добавлению контекста; коммиты и push не выполнялись.

## Подтверждено исходниками

- Адрес участника: `(platform, bot_key, user_id)`; фазы: `idle`, `searching`, `chatting`. Ключ профиля bot_users в актуальной схеме — `(platform, user_id)`.
- Живые люди приоритетны; engine не встаёт в человеческую очередь. Attach должен учитывать уже найденную пару и отмену поиска.
- `POST .../media` сохраняет байты и вызывает пересылку события; README с описанием только хранения устарел. Для TG предусмотрены file_id / method.
- SQL-файлы выполняются при каждом старте, а не один раз. Изменения должны выдерживать повторное применение.
- Сервисный, TG, app и engine токены имеют разные области доступа; app обслуживает платформу web.

## Ограничения

Автоматизированные прикладные тесты при инвентаризации не найдены. В этой задаче API, БД, Redis, S3 и engine не запускались. Наличие премиум-полей в БД не подтверждает работающую оплату.

## Проверка контекста

Документы подготовлены по ранее изученным исходникам и повторно проверенным ветке, HEAD, статусу и AGENTS.md. Проверены наличие шести файлов, локальные Markdown-ссылки, существование путей из таблицы архитектуры и `git diff --check`. Существующие инструкции сохранены; прикладные изменения не добавлены.

## engine_bridge: лимит аккаунта (2026-11-11, не выкачено)

В ветке `loop/engine-limit-failover`: состояние `limited` обрабатывается в `bridge.py`, cooldown 60 с после 409 на start, `EngineAccount.limited_until`. Тесты `engine_bridge/tests/test_bridge_limited.py` (pytest, 7 passed локально). Не проверено против живого chat-engine; нужен релиз chat-engine с `limited`.

## Промо каждого 10-го диалога (2026-11-12, не выкачено)

В ветке `loop/promo-every-10`: `bot_users.dialogs_ended` (`007_dialogs_ended.sql`, без backfill), `_close_open_dialog` считает +1 каждой не-engine стороне при реальном закрытии диалога, событие `promo`, `ChatService.aclose()` в lifespan. Тесты: `tests/test_dialog_history.py` (PromoTests), `tests/test_dialogs_ended_db.py`. Проверки: pytest chat_api/tests на временном Postgres (pgserver, Python 3.13) — 113 passed, без skipped; compileall app — ok. Боты показ промо ещё не умеют (отдельные ветки).
