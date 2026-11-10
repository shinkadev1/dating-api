# Запуск оплаты премиума: чек-лист

Страница оплаты (premium_site, ЮKassa) поднимается тем же деплоем, что и Chat API: push в `master` → `.github/workflows/deploy.yml` собирает образ `dating-premium` → `deploy/update.sh` на сервере. Включается переключателем `PAYMENTS_ENABLED=true|false` в секрете `ENV_FILE`. Деплой не стартует, если `PAYMENTS_ENABLED` в `ENV_FILE` не задан (проверка в `deploy.yml`); при ручном запуске `update.sh` без него оплата включается, когда в `ENV_FILE` есть `PREMIUM_LINK_SECRET` и `YOOKASSA_SHOP_ID`. `.env` на сервере на это решение не влияет. Без оплаты всё работает как раньше, только контейнеров оплаты нет.

До любых действий с контейнерами деплой проверяет секреты и останавливается с понятной ошибкой, если:
- `PREMIUM_LINK_SECRET` или `PAY_AUTH_SECRET` короче 32 символов;
- `PREMIUM_LINK_SECRET` совпадает с одним из токенов `CHAT_API_*`;
- `PREM_SERVICE_*` короче 32 символов или совпадают друг с другом.

**Важно:** запрет медиа без премиума действует сразу после деплоя этой ветки и для всех. Поэтому сначала проверьте, что оплата работает, и только потом выкатывайте ботов с кнопкой оплаты.

## 1. Cloudflare

1. Zero Trust → Networks → Tunnels → туннель сервера vk-bot → Public Hostname → Add: `pay.chatwithyou.site` → Service `HTTP` → `premium:8000`. Cloudflare Access на этот адрес **не** вешать: его открывают покупатели и вызывает ЮKassa.
2. Если на `pay.chatwithyou.site` раньше был custom domain или route старого Worker, удалите его (Workers & Pages → Worker → Settings → Domains & Routes), иначе запросы уйдут в Worker, а не в туннель.
3. DNS-запись `pay` должна указывать на туннель: CNAME на `<id>.cfargotunnel.com`, Cloudflare создаёт её сам при добавлении hostname.

## 2. Секрет ENV_FILE (GitHub → Settings → Secrets → Actions)

С 2026-11-10 все настройки и секреты приложения — в одном секрете **`ENV_FILE`** (env-файл, `КЛЮЧ=значение` по строке): токены Chat API, `CLOUDFLARE_TUNNEL_TOKEN`, оплата. Отдельные секреты GitHub остались только для CI: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN` (и необязательные `DEPLOY_PATH`, `DEPLOY_PORT`). Секрет `PAYMENTS_ENV` больше не читается.

Ведите файл локально как `vk-bot/.env.production` (он в `.gitignore`) и выкладывайте целиком:

```bash
gh secret set ENV_FILE --repo shinkadev1/dating-api < .env.production
```

Деплой не начнётся, если в `ENV_FILE` нет `CHAT_API_APP_TOKEN`, `CLOUDFLARE_TUNNEL_TOKEN` или `PAYMENTS_ENABLED`. Блок оплаты в нём:

```
PAYMENTS_ENABLED=true
YOOKASSA_SHOP_ID=<id тестового магазина>
YOOKASSA_SECRET_KEY=<test_…>
YOOKASSA_TEST_MODE=true
YOOKASSA_RECEIPT_MODE=exempt
PREMIUM_LINK_SECRET=<случайные 48+ символов>
PAY_AUTH_SECRET=<случайные 48+ символов>
```

Переход на боевой магазин: в `.env.production` замените `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET_KEY` боевые, `YOOKASSA_TEST_MODE=false`; для чеков `YOOKASSA_RECEIPT_MODE=receipt` и ключи ниже. Выложите файл в `ENV_FILE`, затем Actions → Deploy → Re-run. `PREMIUM_LINK_SECRET` и `PAY_AUTH_SECRET` не меняйте без нужды: смена разлогинит покупателей и сделает недействительными выданные ссылки.

Без чеков (`exempt`) Resend не обязателен: покупатель входит по ссылке из бота, вход по email отвечает «Письмо не отправлено». В режиме `receipt` нужны `RESEND_API_KEY` и `AUTH_MAIL_FROM` — чеку нужен email.


Здесь только имена. Значения не храните в репозитории и не пересылайте в чатах.

Переключатель: `PAYMENTS_ENABLED=true` (рекомендуется задавать явно).

Обязательные для оплаты:
- `PREMIUM_LINK_SECRET`: не короче 32 символов. Тот же секрет подписывает ссылки в Chat API.
- `PAY_AUTH_SECRET`: не короче 32 символов.
- `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET_KEY`: сначала ключи **тестового** магазина.
- `RESEND_API_KEY`, `AUTH_MAIL_FROM` (только для `receipt`): например `neon chat <noreply@chatwithyou.site>`. Домен должен быть подтверждён в Resend.
- `YOOKASSA_VAT_CODE`, `YOOKASSA_PAYMENT_SUBJECT`: для чеков (режим `receipt`). Значения уточните у бухгалтерии или в настройках онлайн-кассы ЮKassa.

Необязательные (у деплоя есть значения по умолчанию):
- `PAY_PUBLIC_URL` — по умолчанию `https://pay.chatwithyou.site`;
- `PAY_SECURE_COOKIE` — `true`;
- `YOOKASSA_TEST_MODE` — `true`;
- `YOOKASSA_RECEIPT_MODE` — `receipt`;
- `PREM_SERVICE_VK_TOKEN`, `PREM_SERVICE_TG_TOKEN`, `PREM_SERVICE_WEB_TOKEN` — сервер сгенерирует их сам. Задайте их в ENV_FILE, если боты будут ходить в `/internal/peers/...` страницы оплаты;
- `PREMIUM_FORWARDED_ALLOW_IPS` — см. раздел 5;
- `YOOKASSA_WEBHOOK_IPS` — по умолчанию пусто, проверка выключена.

## 3. ЮKassa (тестовый магазин)

1. Интеграция → HTTP-уведомления: URL `https://pay.chatwithyou.site/webhooks/yookassa`, события `payment.succeeded`, `payment.canceled`, `payment.waiting_for_capture`.
2. Включите онлайн-кассу (чеки), иначе платежи с `receipt` будут отклоняться.
3. Тестовые карты — в документации ЮKassa, раздел «Тестирование».
4. Автоплатежи (списание сохранённой картой без участия пользователя) должны быть включены для магазина в ЮKassa. Без них продление создаётся, но ЮKassa требует 3-D Secure (`confirmation.type=redirect`): платёж висит в `pending`, затем отменяется, премиум не продлевается. Проверка: после продления (§10, п. 7) заказ продления должен стать «оплачен» без открытия ссылки подтверждения. Проверено 2026-11-04 на тестовом магазине: автоплатежи не включены.

## 4. Resend

Добавьте и подтвердите домен из `AUTH_MAIL_FROM` (DNS-записи SPF/DKIM в Cloudflare). Без этого коды входа по email не уходят. Email обязателен для чека, поэтому без писем оплатить нельзя.

## 5. IP клиента (уже настроено деплоем)

Покупатель → Cloudflare edge → туннель → контейнер `cloudflared` → `premium:8000` по сети Compose. Edge дописывает настоящий IP клиента **в конец** `X-Forwarded-For`, cloudflared передаёт заголовок дальше. uvicorn доверяет заголовку, только если соединение пришло из `FORWARDED_ALLOW_IPS`. Затем он идёт по списку справа налево и берёт первый адрес не из доверенной сети.

Деплой задаёт `FORWARDED_ALLOW_IPS` равным подсети сети Compose (`docker network inspect`). Поддержку CIDR проверили в uvicorn 0.54, поэтому в requirements стоит `uvicorn>=0.54`. Справа в заголовке всегда стоит адрес, который дописал edge, а адрес клиента в подсеть Docker не попадает. Поэтому подделанный клиентом левый `X-Forwarded-For` игнорируется. Чтобы задать адреса вручную, укажите `PREMIUM_FORWARDED_ALLOW_IPS` в ENV_FILE. `*` не используйте.

`YOOKASSA_WEBHOOK_IPS` можно включить позже, когда в логах видны настоящие IP уведомлений. Вебхук и без списка ничего не начисляет сам: он только перечитывает платёж через API ЮKassa.

Порт хоста: `premium` публикуется только на `127.0.0.1:18080` для отладки с самого сервера (`curl http://127.0.0.1:18080/health`). Туннелю он не нужен: cloudflared ходит к `premium:8000` по сети Compose.

## 6. Порядок выкладки

1. **vk-bot** (эта ветка → `master`). Деплой поднимает chat-api, `premium` и `premium-scheduler`. Проверьте в логах деплоя «Страница оплаты доверяет X-Forwarded-For…» и `Application startup` у `vk-premium-web`. Затем откройте `https://pay.chatwithyou.site/health` — должно прийти `{"status":"ok"}`.
2. **dating-bot-vk**, затем **dating-bot-tg**: кнопка оплаты берёт адрес из `GET /api/v1/peers/{platform}/{bot_key}/{user_id}/premium/link` и отправляет его **URL-кнопкой клавиатуры**, а не текстом. Ссылка действует 24 часа и входит один раз. Превью мессенджера ничего не расходует, вход происходит по кнопке «Продолжить».

## 7. Смоук-тест (тестовый магазин)

1. В боте нажмите кнопку премиума: откроется страница «Премиум для профиля VK ••••NNNN» → «Продолжить».
2. Укажите email → подтвердите привязку к профилю → введите код из письма.
3. Выберите тариф → оплатите тестовой картой → страница счёта. Если подтверждение задерживается, нажмите «Проверить оплату».
4. Проверьте в Studio: в `bot_users.premium_until` этого профиля стоит будущая дата.
5. Отправьте фото собеседнику с премиумом: должно дойти.
6. Отправьте фото собеседнику без премиума: бот должен показать отказ («Собеседник без премиума…»). Без премиума у себя бот предлагает купить.
7. В ЮKassa → Платежи проверьте, что уведомление доставлено (HTTP 200).

## 8. Боевые ключи

В ENV_FILE замените `YOOKASSA_SHOP_ID` и `YOOKASSA_SECRET_KEY` на боевые и поставьте `YOOKASSA_TEST_MODE=false`. В боевом магазине настройте те же HTTP-уведомления и кассу. Перезапустите последний деплой (Actions → Deploy → Re-run) и проведите одну реальную оплату минимального тарифа.

## 9. Откат

- **Выключить оплату:** поставьте `PAYMENTS_ENABLED=false` в ENV_FILE и перезапустите деплой (Actions → Deploy → Re-run). Деплой удалит контейнеры `vk-premium-web` и `vk-premium-scheduler` и уберёт из `.env` сервера `PREMIUM_LINK_SECRET`, `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET_KEY`, `PAY_AUTH_SECRET`, `RESEND_API_KEY`, `YOOKASSA_WEBHOOK_IPS` и `PREMIUM_FORWARDED_ALLOW_IPS`. После этого `/premium/link` в Chat API отвечает 503. Значения в ENV_FILE можно оставить: при `PAYMENTS_ENABLED=true` оплата включится снова. Запрет медиа при этом останется: он в коде Chat API.
- **Вернуть медиа без премиума:** задеплойте предыдущий коммит `master` (revert merge-коммита ветки `payments` и push). Таблицы оплаты и `bot_users.premium_until` останутся в базе и при повторном включении продолжат работать.
- Hostname `pay.chatwithyou.site` в туннеле можно оставить: без контейнера он отвечает 502.

## 10. Локальный тест с тестовым магазином

Нужны: Python 3.11+, PostgreSQL (локально или в контейнере) и ключи **тестового** магазина ЮKassa. Webhook и онлайн-касса не нужны. Значения ключей храните только в локальном `.env`/переменных окружения, не в репозитории.

1. Окружение (из каталога `premium_site`):
   - `python -m venv .venv`, активируйте и выполните `pip install -r requirements.txt`.
2. База: создайте пустую БД Postgres и задайте `DATABASE_URL=postgresql://USER:PASSWORD@127.0.0.1:5432/DBNAME`. Таблицы оплаты создаются при старте. Таблицы Chat API (`bot_users`) необязательны: без них доступ не синхронизируется, в логе будет предупреждение.
3. Переменные окружения (имена; значения подставьте сами):
   - `PAY_PUBLIC_URL=http://127.0.0.1:8000`, `PAY_SECURE_COOKIE=false`;
   - `PAY_AUTH_SECRET` и `PREMIUM_LINK_SECRET` — любые строки не короче 32 символов;
   - `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET_KEY` — ключи тестового магазина, `YOOKASSA_TEST_MODE=true`;
   - `YOOKASSA_RECEIPT_MODE=exempt` — без онлайн-кассы (чек не отправляется). Для режима `receipt` нужны `YOOKASSA_VAT_CODE`, `YOOKASSA_PAYMENT_SUBJECT` и вход по email (`RESEND_API_KEY`, `AUTH_MAIL_FROM`).
4. Запуск из `premium_site`: `uvicorn app.main:app --port 8000`. Проверка: `http://127.0.0.1:8000/health` → `{"status":"ok"}`.
5. Вход по подписанной ссылке (почта не нужна). Сгенерируйте токен тем же `PREMIUM_LINK_SECRET`, из каталога `premium_site`:
   `python -c "from app import paylink; import os; print(paylink.sign('vk', 12345, os.environ['PREMIUM_LINK_SECRET']))"`
   Откройте `http://127.0.0.1:8000/?utm_content=<токен>` и нажмите «Продолжить» (ссылка одноразовая, действует 24 часа). Альтернатива: `GET /api/v1/peers/{platform}/{bot_key}/{user_id}/premium/link` у локального Chat API с тем же `PREMIUM_LINK_SECRET`.
6. Выберите тариф → отметьте оба согласия → «Перейти к оплате →»: откроется страница ЮKassa. Оплатите тестовой картой (номера — в документации ЮKassa, раздел «Тестирование»).
7. Webhook локально не придёт: после возврата на страницу заказа нажмите «Проверить оплату» — заказ перечитает платёж через API и начислит премиум. Планировщик автопродления локально не запускается; при необходимости `python -c "from app import billing; billing.tick()"`. Чтобы проверить продление сразу, сдвиньте в базе `premium_subscriptions.period_end` в прошлое и вызовите `billing.tick()` (оплаченный срок считается от `premium_accounts.premium_until`).
8. Если ЮKassa отклонила создание платежа (неверные ключи, запрет настроек магазина), страница покажет сообщение, а заказ получит статус «отклонён» — можно сразу создать новый. Причина (код и описание ответа ЮKassa) пишется в лог сервера без ключей.

## 11. Кабинет на сайте

Worker сайта показывает подписку и платежи и отменяет продление через внутренние маршруты `premium_site` (email в теле запроса):

- `POST /internal/web/account` — только чтение: подписка, ближайшее списание, последние 4 цифры и бренд карты, история платежей (`paid` и `failed`, до 100). Неизвестный email → пустой ответ, аккаунт не создаётся.
- `POST /internal/web/account/cancel` — отключает автопродление и удаляет сохранённую карту; повтор безопасен (200), без подписки — 409.

Авторизация: `Authorization: Bearer <токен>`, подходит `PREM_SERVICE_WEB_TOKEN` (не короче 32 символов) или `CHAT_API_APP_TOKEN` (не короче 16). Контейнеру `vk-premium-web` `CHAT_API_APP_TOKEN` передаётся из `.env` сервера (`docker-compose.payments.yml`); Worker использует тот же секрет. Схема дополняется колонками `premium_subscriptions.card_last4` и `card_brand` при старте (аддитивно); для подписок, оплаченных до выкладки, карта в кабинете появится после следующего платежа.

Ограничение: кабинет ищет аккаунт по email. Подписки, оформленные по ссылке из бота без привязки email, по email не видны.
