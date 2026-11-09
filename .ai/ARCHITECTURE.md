# Архитектура vk-bot

Проверено: **2026-11-10** (раздел «История диалогов»); остальное — 2026-10-31.

## Поток данных

VK/TG → REST Chat API → ChatService → SQL/Redis → WebSocket клиентам. Сайт и mobile приходят через Worker из репозитория landing. Для внешнего собеседника: Redis → engine_bridge → REST/WS chat-engine → attach в Chat API.

## Точки входа

| Путь | Ответственность |
| --- | --- |
| `chat_api/app/main.py` | FastAPI, lifespan, подключения к Postgres/Redis/S3, `/health`. |
| `chat_api/app/reminders/` | Напоминания давно не заходившим: `settings.py` (параметры, `ENABLED=False`), `content.py` (текст, фото `photo.jpg` необязательно), `service.py` (`ReminderService`, фоновая задача `reminders` в lifespan `main.py`). Раз в 15 мин выбирает `user_channels` через `repositories/reminders.py` и кладёт notice в `chat:broadcast:<platform>` через `publish_notices`. Журнал — `reminder_log`; выбор и запись журнала в одной транзакции с постановкой в очередь; проход под advisory-lock `7_310_003`. Включается только правкой `settings.ENABLED` и деплоем, не `.env`. |
| `chat_api/app/routers/` | REST/WS: peers, engine, channels, media, visitors, stats, ws. |
| `chat_api/app/services/chat.py` | Поиск, остановка, жалобы, создание пары и пересылка. |
| `chat_api/app/repositories/` | SQL для сессий и диалогов (`sessions.py`), реплик и журнала выгрузок (`messages.py`), посетителей и медиа. |
| `chat_api/app/services/archive.py` | `MessageArchiver`: суточная выгрузка старых реплик в S3, запускается из lifespan `main.py`. |
| `chat_api/app/events.py` | Redis Streams и выдача событий по WebSocket. |
| `chat_api/app/deps.py` | Авторизация и ограничения платформ для токенов. |
| `chat_api/app/db.py` | Повторный запуск всех SQL-файлов из init при старте. |
| `supabase/init/` | 001_schema.sql, 002_premium_grant.sql, 003_storage_roles.sql, 004_dialog_history.sql: схема, данные, роли для Supabase Storage, история диалогов. |
| `supabase/gateway/nginx.conf` | Шлюз `storage-gw`: `/storage/v1/*` → `storage:5000` для Studio. |
| `engine_bridge/app/bridge.py` | demand → hunt → внешний поиск → attach → пересылка. |
| `docker-compose.yml` | Postgres, Redis, API, bridge, Studio, Supabase Storage (`storage`, `storage-gw`, том `storage_data`), rclone-перенос (`media-migrate`, профиль migrate). |
| `premium_site/` | Страница оплаты премиума (FastAPI/Jinja, ЮKassa, автопродление `python -m app.scheduler`). Та же база, что у Chat API. |
| `docker-compose.payments.yml` | Overlay с `premium` и `premium-scheduler` (профиль `payments`). |

## Контракты и ограничения

- Адрес участника: `(platform, bot_key, user_id)`; фазы: `idle`, `searching`, `chatting`. Ключ профиля bot_users в актуальной схеме — `(platform, user_id)`.
- Живые люди приоритетны; engine не встаёт в человеческую очередь. Attach должен учитывать уже найденную пару и отмену поиска.
- `POST .../media` сохраняет байты в Supabase Storage (бакет `chat-media`, S3-протокол `http://storage:5000/s3`, ключи `STORAGE_S3_*`; старые медиа Beget не переносились (для них 502), переопределение/откат на Beget через `MEDIA_S3_*`) и вызывает пересылку события; README с описанием только хранения устарел. Для TG предусмотрены file_id / method.
- Медиа уходит, только если премиум (`bot_users.premium_until > NOW()`) есть у отправителя и у живого собеседника; собеседник chat-engine исключён. Иначе 403 с «премиум» в detail до чтения файла: боты показывают предложение купить.
- `GET .../premium/link` (токены vk/tg/web, не engine) → `{url, premium_active, premium_until}`; `url` = `PAY_PUBLIC_URL/?utm_source=<platform>&utm_campaign=premium&utm_content=<token>`. Токен — зашифрованный и подписанный `PREMIUM_LINK_SECRET` (platform, user_id, срок 24 часа), формат в `chat_api/app/premium_link.py` = `premium_site/app/paylink.py`. Без секрета — 503.
- Доступ к медиа хранится в `bot_users.premium_until`. premium_site после оплаты, привязки профиля и входа по ссылке копирует срок аккаунта всем связанным `(platform, user_id)` (upsert, `GREATEST`), планировщик раз в минуту досинхронизирует активные аккаунты.
- Вход по ссылке: токен действует один раз (`premium_link_nonces`, очистка в tick через 25 ч); GET по ссылке ничего не создаёт и не расходует (превью мессенджеров), а показывает «Продолжить» с профилем «VK ••••1943». Вход — только по POST `/auth/link` с CSRF, после него сессия `via_link` с профилем. Сессия `via_link` не выдаёт коды привязки, не отключает автопродление email-подписки и не меняет подписку с сохранённой картой. Email из такой сессии привязывается к аккаунту ссылки только после отдельного подтверждения; занятый email отклоняется (409, «используйте команду /link в чате»), слияния с сайта нет. Слияние аккаунта ссылки в email-аккаунт — только через `claim` (код от email-аккаунта, введённый в боте): оставшийся срок прибавляется к сроку email-аккаунта, заказы и профили переносятся, у перенесённой подписки сбрасываются карта и автопродление.
- SQL-файлы выполняются при каждом старте, а не один раз. Изменения должны выдерживать повторное применение.
- Сервисный, TG, app и engine токены имеют разные области доступа; app обслуживает платформу web.

## История диалогов (с 2026-11-10)

- Таблицы (`004_dialog_history.sql`, идемпотентно, anon/authenticated отозваны): `dialogs` (пара: сторона `a` — инициатор/искавший, у attach — аккаунт chat-engine; `started_at`, `ended_at`, `end_reason` stopped|reported|undeliverable, `ended_by` a|b, NULL — закрыл сервис; строки не удаляются), `messages` (`dialog_id` FK, `sender` a|b, `kind` text|photo|video|video_note|voice|audio, `text`, `media_id`, `tg_file_id`, `tg_method`, `content_type`, `status` delivered|left|skipped), `message_archives` (журнал выгрузок, PK `(day, part)`), колонка `chat_sessions.dialog_id` (без FK).
- Открытие: `_search` (образование пары) и `engine_attach` (успех) вызывают `repo.open_dialog` и `set_chatting(..., dialog_id)` обоим в той же транзакции под lock 7_310_002. Закрытие: `set_idle(peer, reason=, ended_by=)` и `set_searching` закрывают открытый диалог сессии (повторно не перезаписывают); stolen и отказ attach — `ended_by=None`. Пары, созданные до деплоя, имеют `dialog_id` NULL и не пишутся.
- Запись реплик: `ChatService._record` после публикации события (вне lock, отдельное соединение, сбой — WARNING «Не записал сообщение в диалог N: ТипОшибки» без текста, ответ клиенту не меняется). Пишутся `send_message` и `forward_media` (sent → delivered, нет пары → left, skipped); TG→TG медиа — только file_id. Служебные тексты ботов не пишутся.
- Архив: `MessageArchiver` через 300 с после старта и раз в 24 ч берёт session advisory lock 7_310_003 (try), для дней старше `MESSAGES_HOT_DAYS` (90) выгружает части ≤ 20 000 строк (≤ 200 за проход) в бакет медиа: `archive/messages/ГГГГ/ММ/ДД.jsonl.gz`, повторные части — `ДД.partN.jsonl.gz`. Строки удаляются только после `head_object` (размер) и повторного чтения (число строк и крайние id), одной транзакцией с записью в `message_archives`. Без S3 — warning и пропуск. Отключение — `MESSAGES_ARCHIVE_ENABLED=false`. Настройки: `MESSAGES_HOT_DAYS`, `MESSAGES_ARCHIVE_ENABLED`.
- Чтения истории (API/админки) нет. `deploy/update.sh` и `server-logs.yml` печатают только счётчики.

## Развёртывание

Push в `master` запускает `.github/workflows/deploy.yml`; серверное обновление — `deploy/update.sh`.

Схема описывает репозиторий, а не подтверждённое состояние production.
