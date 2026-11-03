# Запуск оплаты премиума: чек-лист

Страница оплаты (premium_site, ЮKassa) поднимается тем же деплоем, что и Chat API: push в `master` → `.github/workflows/deploy.yml` собирает образ `dating-premium` → `deploy/update.sh` на сервере. Включается сама, когда в секрете `ENV_FILE` есть `PREMIUM_LINK_SECRET` и `YOOKASSA_SHOP_ID`. Без них всё работает как раньше, только контейнеров оплаты нет.

**Важно:** запрет медиа без премиума действует сразу после деплоя этой ветки и для всех. Поэтому сначала проверьте, что оплата работает, и только потом выкатывайте ботов с кнопкой оплаты.

## 1. Cloudflare

1. Zero Trust → Networks → Tunnels → туннель сервера vk-bot → Public Hostname → Add: `pay.chatwithyou.site` → Service `HTTP` → `premium:8000`. Cloudflare Access на этот адрес **не** вешать: его открывают покупатели и вызывает ЮKassa.
2. Если на `pay.chatwithyou.site` раньше был custom domain или route старого Worker, удалите его (Workers & Pages → Worker → Settings → Domains & Routes), иначе запросы уйдут в Worker, а не в туннель.
3. DNS-запись `pay` должна указывать на туннель: CNAME на `<id>.cfargotunnel.com`, Cloudflare создаёт её сам при добавлении hostname.

## 2. Секрет ENV_FILE (GitHub → Settings → Secrets → Actions)

Здесь только имена. Значения не храните в репозитории и не пересылайте в чатах.

Обязательные для оплаты:
- `PREMIUM_LINK_SECRET`: не короче 32 символов. Тот же секрет подписывает ссылки в Chat API.
- `PAY_AUTH_SECRET`: не короче 32 символов.
- `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET_KEY`: сначала ключи **тестового** магазина.
- `RESEND_API_KEY`, `AUTH_MAIL_FROM`: например `neon chat <noreply@chatwithyou.site>`. Домен должен быть подтверждён в Resend.
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

## 4. Resend

Добавьте и подтвердите домен из `AUTH_MAIL_FROM` (DNS-записи SPF/DKIM в Cloudflare). Без этого коды входа по email не уходят. Email обязателен для чека, поэтому без писем оплатить нельзя.

## 5. IP клиента (уже настроено деплоем)

Покупатель → Cloudflare edge → туннель → контейнер `cloudflared` → `premium:8000` по сети Compose. Edge дописывает настоящий IP клиента **в конец** `X-Forwarded-For`, cloudflared передаёт заголовок дальше. uvicorn доверяет заголовку, только если соединение пришло из `FORWARDED_ALLOW_IPS`. Затем он идёт по списку справа налево и берёт первый адрес не из доверенной сети.

Деплой задаёт `FORWARDED_ALLOW_IPS` равным подсети сети Compose (`docker network inspect`). Поддержку CIDR проверили в uvicorn 0.54, поэтому в requirements стоит `uvicorn>=0.54`. Справа в заголовке всегда стоит адрес, который дописал edge, а адрес клиента в подсеть Docker не попадает. Поэтому подделанный клиентом левый `X-Forwarded-For` игнорируется. Чтобы задать адреса вручную, укажите `PREMIUM_FORWARDED_ALLOW_IPS` в ENV_FILE. `*` не используйте.

`YOOKASSA_WEBHOOK_IPS` можно включить позже, когда в логах видны настоящие IP уведомлений. Вебхук и без списка ничего не начисляет сам: он только перечитывает платёж через API ЮKassa.

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

- **Выключить оплату:** удалите `PREMIUM_LINK_SECRET` (или `YOOKASSA_SHOP_ID`) из ENV_FILE и перезапустите деплой. Контейнеры `vk-premium-web` и `vk-premium-scheduler` будут удалены, а `/premium/link` начнёт отвечать 503. Запрет медиа при этом останется: он в коде Chat API.
- **Вернуть медиа без премиума:** задеплойте предыдущий коммит `master` (revert merge-коммита ветки `payments` и push). Таблицы оплаты и `bot_users.premium_until` останутся в базе и при повторном включении продолжат работать.
- Hostname `pay.chatwithyou.site` в туннеле можно оставить: без контейнера он отвечает 502.
