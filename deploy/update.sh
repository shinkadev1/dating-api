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

# Ключ, который знает не только сервер: из отдельного секрета GitHub, из ENV_FILE или из .env.
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
    echo "Добавьте $1 в секреты GitHub или в секрет ENV_FILE." >&2
    exit 1
  fi
}

drop_env() {
  tmp=$(mktemp)
  grep -v "^$1=" .env > "$tmp" || true
  mv "$tmp" .env
  chmod 600 .env
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

# Фото, видео, кружки и голосовые. Без ключей API стартует, загрузка отвечает 503.
if optional_secret S3_ACCESS_KEY && optional_secret S3_SECRET_KEY && optional_secret S3_BUCKET; then
  if ! optional_secret S3_ENDPOINT_URL; then
    set_env S3_ENDPOINT_URL "https://s3.ru1.storage.beget.cloud"
  fi
  if ! optional_secret S3_REGION; then
    set_env S3_REGION "ru1"
  fi
else
  echo "Нет ключей S3: медиа не сохраняется."
  drop_env S3_ACCESS_KEY
  drop_env S3_SECRET_KEY
  drop_env S3_BUCKET
  drop_env S3_ENDPOINT_URL
  drop_env S3_REGION
fi

# Мост к chat-engine запускается, только если есть токен chat-engine.
profiles=proxy
optional_services=""
if optional_secret CHAT_ENGINE_TOKEN; then
  ensure_secret CHAT_API_ENGINE_TOKEN
  profiles="${profiles},engine"
  optional_services="${optional_services} engine-bridge"
else
  echo "Нет CHAT_ENGINE_TOKEN: собеседники из chat-engine не подключаются."
  drop_env CHAT_API_ENGINE_TOKEN
  docker rm -f vk-engine-bridge >/dev/null 2>&1 || true
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
docker logout >/dev/null 2>&1 || true
docker compose up -d --no-build db redis
docker compose up -d --no-build --force-recreate chat-api
if [ -n "$optional_services" ]; then
  # shellcheck disable=SC2086 # список сервисов разделён пробелами
  docker compose up -d --no-build --force-recreate $optional_services
fi
docker compose up -d --no-build studio meta cloudflared
sleep 12
docker logs vk-bot-cloudflared --tail 20 2>&1 | grep -E "Registered tunnel connection|ERR|error" || true
docker logs vk-chat-api --tail 40 2>&1 | grep -E "Схема базы|Application startup|ERROR|Traceback" || true
if docker inspect vk-engine-bridge >/dev/null 2>&1; then
  docker logs vk-engine-bridge --tail 30 2>&1 | grep -E "Chat API|Аккаунт|Читаю|Не задан|WARNING|ERROR|Traceback" || true
fi
docker image prune -af
