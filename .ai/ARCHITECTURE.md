# Архитектура vk-bot

Проверено: **2026-10-31**.

## Поток данных

VK/TG → REST Chat API → ChatService → SQL/Redis → WebSocket клиентам. Сайт и mobile приходят через Worker из репозитория landing. Для внешнего собеседника: Redis → engine_bridge → REST/WS chat-engine → attach в Chat API.

## Точки входа

| Путь | Ответственность |
| --- | --- |
| `chat_api/app/main.py` | FastAPI, lifespan, подключения к Postgres/Redis/S3, `/health`. |
| `chat_api/app/routers/` | REST/WS: peers, engine, channels, media, visitors, stats, ws. |
| `chat_api/app/services/chat.py` | Поиск, остановка, жалобы, создание пары и пересылка. |
| `chat_api/app/repositories/` | SQL для сессий, посетителей и медиа. |
| `chat_api/app/events.py` | Redis Streams и выдача событий по WebSocket. |
| `chat_api/app/deps.py` | Авторизация и ограничения платформ для токенов. |
| `chat_api/app/db.py` | Повторный запуск всех SQL-файлов из init при старте. |
| `supabase/init/` | 001_schema.sql и 002_premium_grant.sql: схема и изменения данных. |
| `engine_bridge/app/bridge.py` | demand → hunt → внешний поиск → attach → пересылка. |
| `docker-compose.yml` | Postgres, Redis, API, bridge и вспомогательные сервисы. |
| `premium_site/` | Страница оплаты премиума (FastAPI/Jinja, ЮKassa, автопродление `python -m app.scheduler`). Та же база, что у Chat API. |
| `docker-compose.payments.yml` | Overlay с `premium` и `premium-scheduler` (профиль `payments`). |

## Контракты и ограничения

- Адрес участника: `(platform, bot_key, user_id)`; фазы: `idle`, `searching`, `chatting`. Ключ профиля bot_users в актуальной схеме — `(platform, user_id)`.
- Живые люди приоритетны; engine не встаёт в человеческую очередь. Attach должен учитывать уже найденную пару и отмену поиска.
- `POST .../media` сохраняет байты и вызывает пересылку события; README с описанием только хранения устарел. Для TG предусмотрены file_id / method.
- Медиа уходит, только если премиум (`bot_users.premium_until > NOW()`) есть у отправителя и у живого собеседника; собеседник chat-engine исключён. Иначе 403 с «премиум» в detail до чтения файла: боты показывают предложение купить.
- `GET .../premium/link` (токены vk/tg/web, не engine) → `{url, premium_active, premium_until}`; `url` = `PAY_PUBLIC_URL/?utm_source=<platform>&utm_campaign=premium&utm_content=<token>`. Токен — зашифрованный и подписанный `PREMIUM_LINK_SECRET` (platform, user_id, срок 24 часа), формат в `chat_api/app/premium_link.py` = `premium_site/app/paylink.py`. Без секрета — 503.
- Доступ к медиа хранится в `bot_users.premium_until`. premium_site после оплаты, привязки профиля и входа по ссылке копирует срок аккаунта всем связанным `(platform, user_id)` (upsert, `GREATEST`), планировщик раз в минуту досинхронизирует активные аккаунты.
- Вход по ссылке: токен действует один раз (`premium_link_nonces`, очистка в tick через 25 ч); GET по ссылке ничего не создаёт и не расходует (превью мессенджеров), а показывает «Продолжить» с профилем «VK ••••1943». Вход — только по POST `/auth/link` с CSRF, после него сессия `via_link` с профилем. Сессия `via_link` не выдаёт коды привязки, не отключает автопродление email-подписки и не меняет подписку с сохранённой картой. Email из такой сессии привязывается к аккаунту ссылки только после отдельного подтверждения; занятый email отклоняется (409, «используйте команду /link в чате»), слияния с сайта нет. Слияние аккаунта ссылки в email-аккаунт — только через `claim` (код от email-аккаунта, введённый в боте): оставшийся срок прибавляется к сроку email-аккаунта, заказы и профили переносятся, у перенесённой подписки сбрасываются карта и автопродление.
- SQL-файлы выполняются при каждом старте, а не один раз. Изменения должны выдерживать повторное применение.
- Сервисный, TG, app и engine токены имеют разные области доступа; app обслуживает платформу web.

## Развёртывание

Push в `master` запускает `.github/workflows/deploy.yml`; серверное обновление — `deploy/update.sh`.

Схема описывает репозиторий, а не подтверждённое состояние production.
