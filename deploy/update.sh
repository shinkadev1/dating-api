#!/bin/sh
# Подтягивает коммит, скачивает образы из Docker Hub и перезапускает контейнеры.
# В .env обновляет токены из секрета ENV_FILE и адрес Studio.
set -eu

cd "$(dirname "$0")/.."

if [ -z "${GIT_SHA:-}" ]; then
  echo "Не задан GIT_SHA." >&2
  exit 1
fi
if [ -z "${CHAT_API_IMAGE:-}" ] || [ -z "${ENGINE_BRIDGE_IMAGE:-}" ]; then
  echo "Не заданы CHAT_API_IMAGE и ENGINE_BRIDGE_IMAGE." >&2
  exit 1
fi

git fetch origin "$GIT_SHA"
git checkout --detach --force "$GIT_SHA"

if [ ! -f .env ] && [ -z "${ENV_FILE:-}" ] && [ -z "${ENV_FILE_B64:-}" ]; then
  echo "На сервере нет .env. Создайте его из .env.example и заполните токены." >&2
  exit 1
fi

set_env() {
  key=$1
  value=$2
  tmp=$(mktemp)
  grep -v "^${key}=" .env > "$tmp" || true
  printf '%s=%s\n' "$key" "$value" >> "$tmp"
  mv "$tmp" .env
  chmod 600 .env
}

secret_value() {
  key=$1
  if [ -n "${ENV_FILE_B64:-}" ]; then
    printf '%s' "$ENV_FILE_B64" | base64 -d | tr -d '\r'
  elif [ -n "${ENV_FILE:-}" ]; then
    printf '%s\n' "$ENV_FILE" | tr -d '\r'
  fi | sed -n "s/^[[:space:]]*${key}=//p" | tail -n 1 | sed 's/^"//;s/"$//'
}

# Ключ из секрета ENV_FILE, иначе тот, что уже лежит в .env, иначе новый случайный.
ensure_secret() {
  key=$1
  value=$(secret_value "$key")
  if [ -z "$value" ]; then
    value=$(sed -n "s/^${key}=//p" .env | tail -n 1)
  fi
  if [ -z "$value" ]; then
    value=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    echo "Сгенерирован ${key}."
  fi
  set_env "$key" "$value"
}

# Ключ, который знает не только сервер: из окружения (ручной запуск), из ENV_FILE или из .env.
# Код возврата 1, если его нет нигде.
optional_secret() {
  key=$1
  eval "value=\${$key:-}"
  if [ -z "$value" ]; then
    value=$(secret_value "$key")
  fi
  if [ -z "$value" ]; then
    value=$(sed -n "s/^${key}=//p" .env | tail -n 1)
  fi
  if [ -z "$value" ]; then
    return 1
  fi
  set_env "$key" "$value"
}

# То же, но без ключа деплой останавливается.
require_secret() {
  if ! optional_secret "$1"; then
    echo "Добавьте $1 в секрет ENV_FILE." >&2
    exit 1
  fi
}

drop_env() {
  tmp=$(mktemp)
  grep -v "^$1=" .env > "$tmp" || true
  mv "$tmp" .env
  chmod 600 .env
}

# Значение из окружения (ручной запуск) или ENV_FILE, без .env сервера и без записи.
deploy_value() {
  key=$1
  eval "value=\${$key:-}"
  if [ -z "$value" ]; then
    value=$(secret_value "$key")
  fi
  printf '%s' "$value"
}

env_value() {
  sed -n "s/^$1=//p" .env | tail -n 1
}

fail() {
  echo "$1" >&2
  exit 1
}

# Ключ из секретов или .env, иначе значение по умолчанию.
default_env() {
  if ! optional_secret "$1"; then
    set_env "$1" "$2"
  fi
}

ensure_secret CHAT_API_SERVICE_TOKEN
require_secret CHAT_API_APP_TOKEN
require_secret CLOUDFLARE_TUNNEL_TOKEN

# Боты живут в dating-bot-vk и dating-bot-tg. Здесь Chat API только принимает их токены.
# Без --remove-orphans, чтобы этот деплой не удалил контейнер vk-bot.
if ! optional_secret CHAT_API_TG_TOKEN; then
  echo "Нет CHAT_API_TG_TOKEN: Chat API не примет запросы бота Telegram."
  drop_env CHAT_API_TG_TOKEN
fi

# Ключи Beget больше не читаются (медиа в Supabase Storage); блок оставлен, чтобы старые значения не ломали .env.
if optional_secret S3_ACCESS_KEY && optional_secret S3_SECRET_KEY && optional_secret S3_BUCKET; then
  if ! optional_secret S3_ENDPOINT_URL; then
    set_env S3_ENDPOINT_URL "https://s3.ru1.storage.beget.cloud"
  fi
  if ! optional_secret S3_REGION; then
    set_env S3_REGION "ru1"
  fi
else
  echo "Нет ключей Beget S3: они не используются, медиа лежат в Supabase Storage."
  drop_env S3_ACCESS_KEY
  drop_env S3_SECRET_KEY
  drop_env S3_BUCKET
  drop_env S3_ENDPOINT_URL
  drop_env S3_REGION
fi

# Supabase Storage: ключи S3-протокола знают только сервер (Chat API и rclone, создающий бакет) и storage-api.
# Образцовые значения local-storage-* из .env.example на сервер не попадают: вместо них генерируется случайный ключ.
ensure_storage_secret() {
  key=$1
  value=$(secret_value "$key")
  case $value in local-storage-*) value="" ;; esac
  if [ -z "$value" ]; then
    value=$(sed -n "s/^${key}=//p" .env | tail -n 1)
    case $value in local-storage-*) value="" ;; esac
  fi
  if [ -z "$value" ]; then
    value=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    echo "Сгенерирован ${key}."
  fi
  set_env "$key" "$value"
}
ensure_storage_secret STORAGE_S3_ACCESS_KEY_ID
ensure_storage_secret STORAGE_S3_SECRET_KEY

# JWT_SECRET, ANON_KEY и SERVICE_ROLE_KEY нужны Storage и Studio. Демо-значения Supabase публичны,
# поэтому на сервере выпускаем свои. Без openssl оставляем всё как есть: менять секрет без подписи ключей нельзя.
DEMO_JWT_SECRET="super-secret-jwt-token-with-at-least-32-characters-long"

b64url() {
  openssl base64 -A | tr '+/' '-_' | tr -d '='
}

# Токен HS256: роль $1, подпись секретом $2, срок десять лет.
make_jwt() {
  jwt_header=$(printf '%s' '{"alg":"HS256","typ":"JWT"}' | b64url)
  jwt_now=$(date +%s)
  jwt_payload=$(printf '{"role":"%s","iss":"supabase","iat":%s,"exp":%s}' "$1" "$jwt_now" "$((jwt_now + 315360000))" | b64url)
  jwt_sig=$(printf '%s.%s' "$jwt_header" "$jwt_payload" | openssl dgst -sha256 -hmac "$2" -binary | b64url)
  printf '%s.%s.%s' "$jwt_header" "$jwt_payload" "$jwt_sig"
}

if command -v openssl >/dev/null 2>&1; then
  # Демо-секрет из ENV_FILE (копия .env.example) считается незаданным: берём секрет из .env сервера,
  # иначе на каждом деплое выпускался бы новый и ключи менялись бы.
  jwt_secret=$(deploy_value JWT_SECRET)
  if [ "$jwt_secret" = "$DEMO_JWT_SECRET" ]; then
    echo "JWT_SECRET в ENV_FILE — демо-значение, игнорируется: уберите его из ENV_FILE."
    jwt_secret=""
  fi
  if [ -z "$jwt_secret" ]; then
    jwt_secret=$(env_value JWT_SECRET)
  fi
  jwt_regenerated=0
  if [ -z "$jwt_secret" ] || [ "$jwt_secret" = "$DEMO_JWT_SECRET" ]; then
    jwt_secret=$(head -c 48 /dev/urandom | od -An -tx1 | tr -d ' \n')
    jwt_regenerated=1
    echo "Сгенерирован JWT_SECRET."
  fi
  set_env JWT_SECRET "$jwt_secret"
  if [ "$jwt_regenerated" -eq 1 ]; then
    set_env ANON_KEY "$(make_jwt anon "$jwt_secret")"
    set_env SERVICE_ROLE_KEY "$(make_jwt service_role "$jwt_secret")"
    echo "Выпущены ANON_KEY и SERVICE_ROLE_KEY."
  else
    for pair in ANON_KEY:anon SERVICE_ROLE_KEY:service_role; do
      key=${pair%%:*}
      if ! optional_secret "$key"; then
        set_env "$key" "$(make_jwt "${pair#*:}" "$jwt_secret")"
        echo "Выпущен ${key}."
      fi
    done
  fi
  unset jwt_secret
else
  echo "Нет openssl: JWT_SECRET, ANON_KEY и SERVICE_ROLE_KEY остаются как есть (по умолчанию демо Supabase)."
fi

# Мост к chat-engine запускается, только если есть токен chat-engine.
profiles=proxy
optional_services=""
remove_containers=""
if optional_secret CHAT_ENGINE_TOKEN; then
  ensure_secret CHAT_API_ENGINE_TOKEN
  profiles="${profiles},engine"
  optional_services="${optional_services} engine-bridge"
else
  echo "Нет CHAT_ENGINE_TOKEN: собеседники из chat-engine не подключаются."
  drop_env CHAT_API_ENGINE_TOKEN
  remove_containers="vk-engine-bridge"
fi

# Страница оплаты (premium_site). Решают только секреты деплоя (окружение и ENV_FILE), не .env сервера:
# PAYMENTS_ENABLED=true|false, а без него — наличие PREMIUM_LINK_SECRET и YOOKASSA_SHOP_ID.
case $(deploy_value PAYMENTS_ENABLED | tr '[:upper:]' '[:lower:]') in
  true|1|yes) payments=1 ;;
  false|0|no) payments=0 ;;
  "")
    if [ -n "$(deploy_value PREMIUM_LINK_SECRET)" ] && [ -n "$(deploy_value YOOKASSA_SHOP_ID)" ]; then
      payments=1
    else
      payments=0
    fi
    ;;
  *) fail "PAYMENTS_ENABLED должен быть true или false." ;;
esac
if [ "$payments" -eq 1 ]; then
  if [ -z "${PREMIUM_IMAGE:-}" ]; then
    fail "Не задан PREMIUM_IMAGE."
  fi
  require_secret PREMIUM_LINK_SECRET
  require_secret PAY_AUTH_SECRET
  require_secret YOOKASSA_SHOP_ID
  require_secret YOOKASSA_SECRET_KEY
  ensure_secret PREM_SERVICE_VK_TOKEN
  ensure_secret PREM_SERVICE_TG_TOKEN
  ensure_secret PREM_SERVICE_WEB_TOKEN
  default_env PAY_PUBLIC_URL "https://pay.chatwithyou.site"
  default_env PAY_SECURE_COOKIE "true"
  # Сначала тестовый магазин ЮKassa; боевые ключи и YOOKASSA_TEST_MODE=false — в ENV_FILE.
  default_env YOOKASSA_TEST_MODE "true"
  default_env YOOKASSA_RECEIPT_MODE "receipt"
  if [ "$(sed -n 's/^YOOKASSA_RECEIPT_MODE=//p' .env | tail -n 1)" = "receipt" ]; then
    require_secret YOOKASSA_VAT_CODE
    require_secret YOOKASSA_PAYMENT_SUBJECT
    # Чеку нужен email покупателя, а значит вход по коду из письма.
    require_secret RESEND_API_KEY
    require_secret AUTH_MAIL_FROM
  else
    # Без чеков покупатель входит по ссылке из бота; вход по email работает, только если задан Resend.
    if ! optional_secret RESEND_API_KEY; then
      echo "Нет RESEND_API_KEY: вход по email выключен, оплата по ссылке из бота."
      drop_env RESEND_API_KEY
    fi
    optional_secret AUTH_MAIL_FROM || true
  fi
  if ! optional_secret YOOKASSA_WEBHOOK_IPS; then
    drop_env YOOKASSA_WEBHOOK_IPS
  fi
  # Явный адрес прокси только из ENV_FILE; иначе подсеть сети Compose определяется после старта chat-api.
  forwarded=$(secret_value PREMIUM_FORWARDED_ALLOW_IPS)
  profiles="${profiles},payments"
  export COMPOSE_PATH_SEPARATOR=":"
  export COMPOSE_FILE="docker-compose.yml:docker-compose.payments.yml"
  export PREMIUM_IMAGE
else
  echo "Оплата выключена: страница оплаты не запускается, ссылка на оплату отвечает 503."
  # Ключи доступа к оплате убираем из .env, чтобы выключение было настоящим и при ручном запуске.
  for key in PREMIUM_LINK_SECRET YOOKASSA_SHOP_ID YOOKASSA_SECRET_KEY PAY_AUTH_SECRET RESEND_API_KEY YOOKASSA_WEBHOOK_IPS PREMIUM_FORWARDED_ALLOW_IPS; do
    drop_env "$key"
  done
  unset COMPOSE_FILE
  remove_containers="${remove_containers} vk-premium-web vk-premium-scheduler"
fi

# Проверка секретов до любых действий с контейнерами: иначе chat-api или premium упадут на старте.
min_length() {
  value=$(env_value "$1")
  if [ "${#value}" -lt 32 ]; then
    fail "$1 должен быть не короче 32 символов."
  fi
}
if [ "$payments" -eq 1 ]; then
  min_length PREMIUM_LINK_SECRET
  min_length PAY_AUTH_SECRET
  link_secret=$(env_value PREMIUM_LINK_SECRET)
  for key in CHAT_API_SERVICE_TOKEN CHAT_API_APP_TOKEN CHAT_API_TG_TOKEN CHAT_API_ENGINE_TOKEN; do
    if [ "$(env_value "$key")" = "$link_secret" ]; then
      fail "PREMIUM_LINK_SECRET должен отличаться от ${key}."
    fi
  done
  for key in PREM_SERVICE_VK_TOKEN PREM_SERVICE_TG_TOKEN PREM_SERVICE_WEB_TOKEN; do
    min_length "$key"
  done
  if [ "$(env_value PREM_SERVICE_VK_TOKEN)" = "$(env_value PREM_SERVICE_TG_TOKEN)" ] ||
     [ "$(env_value PREM_SERVICE_VK_TOKEN)" = "$(env_value PREM_SERVICE_WEB_TOKEN)" ] ||
     [ "$(env_value PREM_SERVICE_TG_TOKEN)" = "$(env_value PREM_SERVICE_WEB_TOKEN)" ]; then
    fail "PREM_SERVICE_VK_TOKEN, PREM_SERVICE_TG_TOKEN и PREM_SERVICE_WEB_TOKEN должны различаться."
  fi
fi

if [ -n "$remove_containers" ]; then
  # shellcheck disable=SC2086 # список контейнеров разделён пробелами
  docker rm -f $remove_containers >/dev/null 2>&1 || true
fi
export COMPOSE_PROFILES="$profiles"
for stale in STUDIO_HOST PAY_HOST API_HOST CORS_ORIGINS PREMIUM_URL PREMIUM_PUBLISH WEB_GATEWAY_PUBLISH WEB_GATEWAY_IMAGE VK_TOKEN VK_TOKENS TG_TOKEN TG_TOKENS TELEGRAM_BOT_TOKEN GUEST_JWT_SECRET CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET; do
  drop_env "$stale"
done

STUDIO_HOST="${STUDIO_HOST:-studio.chatwithyou.site}"

set_env STUDIO_PUBLIC_URL "https://${STUDIO_HOST}"
set_env CHAT_API_PUBLISH "127.0.0.1:8000"

if [ -n "${DOCKERHUB_USERNAME:-}" ] && [ -n "${DOCKERHUB_TOKEN:-}" ]; then
  printf '%s' "$DOCKERHUB_TOKEN" | docker login -u "$DOCKERHUB_USERNAME" --password-stdin
fi

docker compose pull
# rclone создаёт бакет, а образ профиля migrate обычным pull не скачивается; нужен до docker logout.
timeout 300 docker compose --profile migrate pull media-migrate
docker logout >/dev/null 2>&1 || true
timeout 300 docker compose up -d --no-build --wait db redis

pgdb=$(env_value POSTGRES_DB)
pgdb=${pgdb:-postgres}

# Диагностика перед хранилищем: только чтение, результат в лог.
echo "Диагностика Supabase Storage (только чтение):"
timeout 60 docker compose exec -T db psql -U supabase_admin -d "$pgdb" -Atc "
  SELECT 'роль ' || r || ': ' || CASE WHEN EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN 'есть' ELSE 'нет' END
  FROM unnest(ARRAY['anon', 'authenticated', 'service_role', 'supabase_storage_admin', 'postgres']) AS r
  UNION ALL
  SELECT 'схема ' || s || ': ' || CASE WHEN EXISTS (SELECT FROM pg_namespace WHERE nspname = s) THEN 'есть' ELSE 'нет' END
  FROM unnest(ARRAY['storage', 'auth']) AS s" || echo "Диагностика ролей не удалась."
docker_root=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || true)
df -h "${docker_root:-/var/lib/docker}" 2>/dev/null || df -h / || true

# Роли для миграций storage-api. Скрипт идемпотентен; chat-api прогоняет его же при старте.
timeout 120 docker compose exec -T db psql -v ON_ERROR_STOP=1 -U supabase_admin -d "$pgdb" -f /docker-entrypoint-initdb.d/003_storage_roles.sql
timeout 420 docker compose up -d --no-build --wait storage storage-gw

# Бакет chat-media создаём ДО переключения chat-api (идемпотентно; ошибка "уже существует" не фатальна).
# Старые медиа из Beget не переносятся. Шаг ограничен по времени, вывод ошибки печатается.
mkdir_out=$(timeout 180 docker compose --profile migrate run --rm -T media-migrate mkdir dst:chat-media 2>&1) || {
  if printf '%s' "$mkdir_out" | grep -qi "already"; then
    echo "Бакет chat-media уже существует."
  else
    printf '%s
' "$mkdir_out" | tail -n 30 >&2
    fail "Не удалось создать бакет chat-media в Supabase Storage."
  fi
}

docker compose up -d --no-build --force-recreate chat-api

if [ -n "$optional_services" ]; then
  # shellcheck disable=SC2086 # список сервисов разделён пробелами
  docker compose up -d --no-build --force-recreate $optional_services
fi
if [ "$payments" -eq 1 ]; then
  # premium_site пишет в bot_users, которую создаёт схема chat-api: ждём его healthcheck.
  tries=0
  until [ "$(docker inspect -f '{{.State.Health.Status}}' vk-chat-api 2>/dev/null)" = "healthy" ]; do
    tries=$((tries + 1))
    if [ "$tries" -gt 30 ]; then
      echo "chat-api не стал healthy, страница оплаты не запущена." >&2
      exit 1
    fi
    sleep 3
  done
  if [ -z "$forwarded" ]; then
    # cloudflared ходит к premium по сети Compose: доверяем X-Forwarded-For только из её подсети.
    network=$(docker inspect -f '{{range $name, $net := .NetworkSettings.Networks}}{{$name}} {{end}}' vk-chat-api | awk '{print $1}')
    forwarded=$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}},{{end}}' "$network" | sed 's/,$//')
  fi
  if [ -z "$forwarded" ]; then
    echo "Не удалось определить подсеть сети Compose; задайте PREMIUM_FORWARDED_ALLOW_IPS в ENV_FILE." >&2
    exit 1
  fi
  set_env PREMIUM_FORWARDED_ALLOW_IPS "$forwarded"
  echo "Страница оплаты доверяет X-Forwarded-For от ${forwarded}."
  docker compose up -d --no-build --no-deps --force-recreate premium premium-scheduler
fi
docker compose up -d --no-build studio meta cloudflared
sleep 12
docker logs vk-bot-cloudflared --tail 20 2>&1 | grep -E "Registered tunnel connection|ERR|error" || true
docker logs vk-chat-api --tail 40 2>&1 | grep -E "Схема базы|Application startup|Напоминания|ERROR|Traceback" || true
if [ "$payments" -eq 1 ]; then
  docker logs vk-premium-web --tail 30 2>&1 | grep -E "Application startup|bot_users|ERROR|Traceback" || true
  docker logs vk-premium-scheduler --tail 30 2>&1 | grep -E "bot_users|failed|ERROR|Traceback" || true
fi
if docker inspect vk-engine-bridge >/dev/null 2>&1; then
  docker logs vk-engine-bridge --tail 30 2>&1 | grep -E "Chat API|Аккаунт|Читаю|Не задан|WARNING|ERROR|Traceback" || true
fi

# SMOKE хранилища. set -x здесь не включать: в окружении лежат ключи.
wait_healthy() {
  tries=0
  until [ "$(docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null)" = "healthy" ]; do
    tries=$((tries + 1))
    if [ "$tries" -gt 30 ]; then
      echo "SMOKE: $1 не стал healthy." >&2
      exit 1
    fi
    sleep 3
  done
  echo "SMOKE: $1 healthy."
}
wait_healthy vk-bot-storage
wait_healthy vk-bot-storage-gw
wait_healthy vk-chat-api

smoke_s3='
import asyncio, os, sys, time
import aioboto3
from botocore.config import Config

async def main():
    kw = dict(signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 3, "mode": "standard"})
    try:
        cfg = Config(**kw, request_checksum_calculation="when_required", response_checksum_validation="when_required")
    except TypeError:
        cfg = Config(**kw)
    key = "smoke/%d.txt" % int(time.time())
    body = os.urandom(64)
    async with aioboto3.Session().client(
        "s3",
        endpoint_url="http://storage:5000/s3",
        region_name="local",
        aws_access_key_id=os.environ["SMOKE_KEY"],
        aws_secret_access_key=os.environ["SMOKE_SECRET"],
        config=cfg,
    ) as client:
        await client.put_object(Bucket="chat-media", Key=key, Body=body)
        resp = await client.get_object(Bucket="chat-media", Key=key)
        got = await resp["Body"].read()
        await client.delete_object(Bucket="chat-media", Key=key)
    print("S3 put/get/delete: " + ("bytes match" if got == body else "BYTES DIFFER"))
    sys.exit(0 if got == body else 1)

asyncio.run(main())
'
SMOKE_KEY=$(env_value STORAGE_S3_ACCESS_KEY_ID)
SMOKE_SECRET=$(env_value STORAGE_S3_SECRET_KEY)
export SMOKE_KEY SMOKE_SECRET
tries=0
until timeout 60 docker exec -e SMOKE_KEY -e SMOKE_SECRET vk-chat-api python -c "$smoke_s3"; do
  tries=$((tries + 1))
  if [ "$tries" -ge 3 ]; then
    echo "SMOKE: put/get/delete через storage из chat-api не прошёл." >&2
    exit 1
  fi
  sleep 5
done
unset SMOKE_KEY SMOKE_SECRET

# Studio видит бакет через шлюз (ключ service_role берётся из окружения контейнера).
smoke_studio='
const k = process.env.SUPABASE_SERVICE_KEY;
fetch("http://storage-gw:8000/storage/v1/bucket", { headers: { Authorization: "Bearer " + k, apikey: k } })
  .then(async (r) => {
    const t = await r.text();
    console.log("Studio -> storage-gw /storage/v1/bucket: HTTP " + r.status + (t.includes("chat-media") ? ", chat-media found" : ", chat-media MISSING"));
    process.exit(r.status === 200 && t.includes("chat-media") ? 0 : 1);
  })
  .catch((e) => { console.error(e.message); process.exit(1); });
'
tries=0
until timeout 60 docker exec vk-bot-studio node -e "$smoke_studio"; do
  tries=$((tries + 1))
  if [ "$tries" -ge 10 ]; then
    echo "SMOKE: Studio не видит бакет chat-media через storage-gw." >&2
    exit 1
  fi
  sleep 6
done

# Размер бакета и записи в базе — для сверки в логе.
size_out=$(timeout 120 docker compose --profile migrate run --rm -T media-migrate size --json dst:chat-media 2>&1) || size_out="не удалось прочитать: $(printf '%s' "$size_out" | tail -n 3)"
echo "chat-media: $(printf "%s" "$size_out" | grep "^{" || printf "%s" "$size_out" | tail -n 3)"
echo "stored_media (записей|байт): $(timeout 60 docker compose exec -T db psql -U supabase_admin -d "$pgdb" -Atc 'SELECT count(*), COALESCE(sum(size_bytes), 0) FROM stored_media' || echo 'не удалось прочитать')"
echo "история (диалоги|открыто|сообщения|части архива): $(timeout 60 docker compose exec -T db psql -U supabase_admin -d "$pgdb" -Atc 'SELECT (SELECT count(*) FROM dialogs), (SELECT count(*) FROM dialogs WHERE ended_at IS NULL), (SELECT count(*) FROM messages), (SELECT count(*) FROM message_archives)' || echo 'не удалось прочитать')"

docker image prune -af
