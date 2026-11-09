"""Сообщение в бот после оплаты: «Премиум активен до …».

billing.process() в той же транзакции, что и начисление, кладёт строку в premium_notices (outbox).
drain() отправляет их в поток уведомлений Chat API `chat:broadcast:<platform>` (Redis), который
боты VK и Telegram уже читают и показывают как событие `notice`. Адресаты — чаты, где человек
уже писал боту (user_channels/channels схемы Chat API). Доставка «хотя бы один раз».
"""
import json
import logging
import os
from datetime import timedelta, timezone

BROADCAST_PREFIX = 'chat:broadcast:'
MSK = timezone(timedelta(hours=3))
# Уведомление, не ушедшее за сутки (Redis был недоступен), уже не новость.
MAX_AGE = '1 day'
_redis = None


def schema(c):
    c.execute('CREATE TABLE IF NOT EXISTS premium_notices(id BIGSERIAL PRIMARY KEY,account_id UUID NOT NULL,until TIMESTAMPTZ NOT NULL,renewal BOOLEAN NOT NULL DEFAULT FALSE,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),sent_at TIMESTAMPTZ)')
    c.execute('CREATE INDEX IF NOT EXISTS premium_notices_unsent ON premium_notices(created_at) WHERE sent_at IS NULL')


def enqueue(c, account_id, until, renewal):
    """Вызывается внутри транзакции начисления."""
    c.execute('INSERT INTO premium_notices(account_id,until,renewal) VALUES(%s,%s,%s)', (account_id, until, renewal))


def text(until, renewal):
    when = until.astimezone(MSK).strftime('%d.%m.%Y %H:%M')
    head = 'Премиум продлён' if renewal else 'Оплата прошла, премиум активен'
    return f'👑 {head} до {when} (МСК).\nТеперь можно отправлять фото, видео и голосовые.'


def redis_client():
    global _redis
    url = os.getenv('REDIS_URL', '')
    if not url: return None
    if _redis is None:
        import redis
        _redis = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
    return _redis


def recipients(c, account_id):
    """(platform, bot_key, user_id) всех чатов VK/TG, где привязанные к аккаунту профили писали боту."""
    if not c.execute("SELECT to_regclass('user_channels') IS NOT NULL AND to_regclass('channels') IS NOT NULL AS ok").fetchone()['ok']:
        return []
    rows = c.execute("""SELECT ch.platform, ch.external_id, i.user_id FROM premium_identities i
        JOIN user_channels l ON l.user_platform=i.platform AND l.user_id=i.user_id
        JOIN channels ch ON ch.id=l.channel_id
        WHERE i.account_id=%s AND ch.platform IN ('vk','tg') ORDER BY ch.platform, ch.external_id""", (account_id,)).fetchall()
    return [(row['platform'], row['external_id'], int(row['user_id'])) for row in rows]


def drain(client=None, limit=50):
    """Отправляет неотправленные уведомления. Без REDIS_URL ничего не делает. Возвращает число адресатов."""
    client = client or redis_client()
    if client is None: return 0
    from .billing import connect
    sent = 0
    with connect() as c:
        for _ in range(limit):
            # One notice per transaction: a Redis failure keeps the ones already sent marked as sent.
            with c.transaction():
                row = c.execute(f"SELECT * FROM premium_notices WHERE sent_at IS NULL AND created_at>NOW()-INTERVAL '{MAX_AGE}' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED").fetchone()
                if not row: break
                body_text = text(row['until'], row['renewal'])
                for platform, bot_key, user_id in recipients(c, row['account_id']):
                    body = {'to': {'platform': platform, 'bot_key': bot_key, 'user_id': user_id}, 'type': 'notice', 'text': body_text}
                    client.xadd(BROADCAST_PREFIX + platform, {'data': json.dumps(body, ensure_ascii=False)}, maxlen=1_000_000, approximate=True)
                    sent += 1
                c.execute('UPDATE premium_notices SET sent_at=NOW() WHERE id=%s', (row['id'],))
    return sent


def drain_quietly(limit=50):
    """Сбой Redis не должен ломать оплату: уведомление останется в очереди до следующего раза."""
    try: return drain(limit=limit)
    except Exception as exc:
        logging.warning('Premium notice not sent yet: %s', type(exc).__name__)
        return 0
