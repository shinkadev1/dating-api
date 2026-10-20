-- Схема анонимного чата. Файл идемпотентный: Chat API прогоняет его при каждом старте.
-- Человек в bot_users уникален в пределах платформы: один vk id — одна строка.
-- Чаты, куда подключён бот, лежат в channels. Через какие из них человек писал — в user_channels.
-- Живой диалог по-прежнему адрес (platform, bot_key, user_id) в chat_sessions:
-- ответ уходит в то сообщество, куда человек написал.

CREATE TABLE IF NOT EXISTS chat_sessions (
    platform TEXT NOT NULL DEFAULT 'vk',
    bot_key TEXT NOT NULL DEFAULT '',
    user_id BIGINT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('idle', 'searching', 'chatting')),
    partner_platform TEXT,
    partner_bot_key TEXT,
    partner_id BIGINT,
    searching_since TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (platform, bot_key, user_id)
);

-- Базы, созданные до мультиплатформы: недостающие колонки и составной ключ сессии.
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS platform TEXT NOT NULL DEFAULT 'vk';
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS bot_key TEXT NOT NULL DEFAULT '';
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS partner_platform TEXT;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS partner_bot_key TEXT;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS partner_id BIGINT;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS searching_since TIMESTAMPTZ;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indrelid
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY (i.indkey)
    WHERE c.relname = 'chat_sessions' AND i.indisprimary AND a.attname = 'platform'
  ) THEN
    ALTER TABLE chat_sessions DROP CONSTRAINT chat_sessions_pkey;
    ALTER TABLE chat_sessions ADD PRIMARY KEY (platform, bot_key, user_id);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS chat_sessions_queue_idx
    ON chat_sessions (searching_since)
    WHERE phase = 'searching' AND searching_since IS NOT NULL;

CREATE TABLE IF NOT EXISTS channels (
    id BIGSERIAL PRIMARY KEY,
    platform TEXT NOT NULL CHECK (platform IN ('vk', 'tg', 'web')),
    external_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    UNIQUE (platform, external_id)
);

-- Аккаунты chat-engine не люди: у всех user_id = 1, различаются они bot_key.
CREATE TABLE IF NOT EXISTS engine_presence (
    bot_key TEXT PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bot_users (
    platform TEXT NOT NULL DEFAULT 'vk',
    user_id BIGINT NOT NULL,
    handle TEXT,
    search_count INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (platform, user_id)
);

ALTER TABLE bot_users
    ADD COLUMN IF NOT EXISTS is_premium BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE bot_users
    ADD COLUMN IF NOT EXISTS premium_until TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS user_channels (
    user_platform TEXT NOT NULL CHECK (user_platform IN ('vk', 'tg', 'web')),
    user_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL REFERENCES channels (id),
    search_count INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_platform, user_id, channel_id)
);

-- Старый bot_users хранил человека отдельно для каждого бота: (platform, bot_key, user_id).
-- Здесь строки одного человека сливаются, чаты переносятся в user_channels,
-- а аккаунты chat-engine уходят в engine_presence.
DO $$
DECLARE
    has_bot_key boolean;
    pk_cols text[];
    pk_name text;
    profile_column text;
BEGIN
    SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'bot_users'
          AND column_name = 'bot_key'
    ) INTO has_bot_key;

    SELECT COALESCE(ARRAY(
        SELECT attribute.attname::text
        FROM pg_index AS index
        JOIN LATERAL unnest(index.indkey) WITH ORDINALITY AS key(attnum, ord) ON true
        JOIN pg_attribute AS attribute
          ON attribute.attrelid = index.indrelid
         AND attribute.attnum = key.attnum
        WHERE index.indrelid = 'bot_users'::regclass
          AND index.indisprimary
          AND key.attnum > 0
        ORDER BY key.ord
    ), ARRAY[]::text[]) INTO pk_cols;

    IF pk_cols = ARRAY['platform', 'user_id']::text[] AND NOT has_bot_key THEN
        RETURN;
    END IF;

    ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS platform TEXT NOT NULL DEFAULT 'vk';
    ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS search_count INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
    ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
    ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
    ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS handle TEXT;
    IF NOT has_bot_key THEN
        ALTER TABLE bot_users ADD COLUMN bot_key TEXT NOT NULL DEFAULT '';
    END IF;

    SELECT conname INTO pk_name
    FROM pg_constraint
    WHERE conrelid = 'bot_users'::regclass
      AND contype = 'p';

    IF pk_name IS NOT NULL AND NOT ('bot_key' = ANY (pk_cols)) THEN
        EXECUTE format('ALTER TABLE bot_users DROP CONSTRAINT %I', pk_name);
        pk_name := NULL;
    END IF;

    IF pk_name IS NULL THEN
        ALTER TABLE bot_users ADD PRIMARY KEY (platform, bot_key, user_id);
    END IF;

    CREATE TEMP TABLE bot_users_snapshot ON COMMIT DROP AS
    SELECT * FROM bot_users;

    INSERT INTO engine_presence (bot_key, started_at, last_seen_at)
    SELECT bot_key, MIN(started_at), MAX(last_seen_at)
    FROM bot_users_snapshot
    WHERE platform = 'engine' AND bot_key <> ''
    GROUP BY bot_key
    ON CONFLICT (bot_key) DO UPDATE SET
        started_at = LEAST(engine_presence.started_at, EXCLUDED.started_at),
        last_seen_at = GREATEST(engine_presence.last_seen_at, EXCLUDED.last_seen_at);

    INSERT INTO channels (platform, external_id)
    SELECT DISTINCT platform, bot_key
    FROM bot_users_snapshot
    WHERE platform IN ('vk', 'tg', 'web') AND bot_key <> ''
    ON CONFLICT (platform, external_id) DO NOTHING;

    INSERT INTO user_channels (
        user_platform, user_id, channel_id, search_count, started_at, last_seen_at
    )
    SELECT snapshot.platform, snapshot.user_id, channel.id,
           snapshot.search_count, snapshot.started_at, snapshot.last_seen_at
    FROM bot_users_snapshot AS snapshot
    JOIN channels AS channel
      ON channel.platform = snapshot.platform
     AND channel.external_id = snapshot.bot_key
    WHERE snapshot.platform IN ('vk', 'tg', 'web') AND snapshot.bot_key <> ''
    ON CONFLICT (user_platform, user_id, channel_id) DO UPDATE SET
        search_count = EXCLUDED.search_count,
        started_at = LEAST(user_channels.started_at, EXCLUDED.started_at),
        last_seen_at = GREATEST(user_channels.last_seen_at, EXCLUDED.last_seen_at);

    -- vk_url мог быть вычисляемой колонкой «https://vk.com/id» || user_id для всех платформ.
    -- Обычная колонка нужна, чтобы у Telegram и сайта ссылки ВК не было.
    IF EXISTS (
        SELECT 1
        FROM pg_attribute AS attribute
        WHERE attribute.attrelid = 'bot_users'::regclass
          AND attribute.attname = 'vk_url'
          AND NOT attribute.attisdropped
          AND attribute.attgenerated <> ''
    ) THEN
        ALTER TABLE bot_users DROP COLUMN vk_url;
        ALTER TABLE bot_users ADD COLUMN vk_url TEXT;
    END IF;

    DELETE FROM bot_users WHERE platform = 'engine';

    DELETE FROM bot_users AS extra
    USING (
        SELECT platform, user_id, bot_key
        FROM (
            SELECT
                platform,
                user_id,
                bot_key,
                row_number() OVER (
                    PARTITION BY platform, user_id
                    ORDER BY updated_at DESC NULLS LAST, bot_key DESC
                ) AS rn
            FROM bot_users
            WHERE platform <> 'engine'
        ) AS ranked
        WHERE rn > 1
    ) AS drop_rows
    WHERE extra.platform = drop_rows.platform
      AND extra.user_id = drop_rows.user_id
      AND extra.bot_key = drop_rows.bot_key;

    UPDATE bot_users AS person
    SET search_count = merged.search_count,
        started_at = merged.started_at,
        updated_at = merged.updated_at,
        last_seen_at = merged.last_seen_at,
        handle = CASE
            WHEN person.platform = 'vk' THEN 'https://vk.com/id' || person.user_id::text
            ELSE person.handle
        END
    FROM (
        SELECT
            platform,
            user_id,
            SUM(search_count)::integer AS search_count,
            MIN(started_at) AS started_at,
            MAX(updated_at) AS updated_at,
            MAX(last_seen_at) AS last_seen_at
        FROM bot_users_snapshot
        WHERE platform <> 'engine'
        GROUP BY platform, user_id
    ) AS merged
    WHERE person.platform = merged.platform
      AND person.user_id = merged.user_id;

    FOR profile_column IN
        SELECT columns.column_name
        FROM information_schema.columns AS columns
        WHERE columns.table_schema = current_schema()
          AND columns.table_name = 'bot_users'
          AND columns.column_name IN ('gender', 'age', 'partner_gender', 'is_premium', 'vk_url')
    LOOP
        EXECUTE format(
            $q$
            UPDATE bot_users AS person
            SET %1$I = picked.val
            FROM (
                SELECT DISTINCT ON (platform, user_id)
                       platform,
                       user_id,
                       %1$I AS val
                FROM bot_users_snapshot
                WHERE platform <> 'engine'
                  AND %1$I IS NOT NULL
                  AND btrim(%1$I::text) <> ''
                ORDER BY platform, user_id, updated_at DESC NULLS LAST
            ) AS picked
            WHERE person.platform = picked.platform
              AND person.user_id = picked.user_id
            $q$,
            profile_column
        );
    END LOOP;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'bot_users'
          AND column_name = 'vk_url'
    ) THEN
        UPDATE bot_users SET vk_url = NULL WHERE platform <> 'vk';
    END IF;

    SELECT conname INTO pk_name
    FROM pg_constraint
    WHERE conrelid = 'bot_users'::regclass
      AND contype = 'p';
    IF pk_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE bot_users DROP CONSTRAINT %I', pk_name);
    END IF;
    ALTER TABLE bot_users DROP COLUMN bot_key;
    ALTER TABLE bot_users ADD PRIMARY KEY (platform, user_id);
END $$;

INSERT INTO channels (platform, external_id, title)
VALUES ('web', 'site', 'Сайт')
ON CONFLICT (platform, external_id) DO UPDATE SET
    title = CASE WHEN channels.title = '' THEN EXCLUDED.title ELSE channels.title END;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'user_channels_user_fk'
    ) THEN
        ALTER TABLE user_channels
            ADD CONSTRAINT user_channels_user_fk
            FOREIGN KEY (user_platform, user_id)
            REFERENCES bot_users (platform, user_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS bot_users_last_seen_idx ON bot_users (last_seen_at);
CREATE INDEX IF NOT EXISTS user_channels_channel_idx ON user_channels (channel_id);
CREATE INDEX IF NOT EXISTS engine_presence_last_seen_idx ON engine_presence (last_seen_at);

-- Кука сайта. id живёт в браузере, user_id — тот же человек в bot_users (platform = web).
CREATE TABLE IF NOT EXISTS web_visitors (
    id UUID PRIMARY KEY,
    platform TEXT NOT NULL DEFAULT 'web' CHECK (platform = 'web'),
    user_id BIGINT NOT NULL UNIQUE,
    visits INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (platform, user_id)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'web_visitors_user_fk'
    ) THEN
        ALTER TABLE web_visitors
            ADD CONSTRAINT web_visitors_user_fk
            FOREIGN KEY (platform, user_id)
            REFERENCES bot_users (platform, user_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS web_visitors_last_seen_idx ON web_visitors (last_seen_at);

COMMENT ON TABLE web_visitors IS 'Посетители сайта: кука iskra_id и пользователь web';
COMMENT ON COLUMN web_visitors.id IS 'Значение куки iskra_id';
COMMENT ON COLUMN web_visitors.visits IS 'Сколько раз сайт представил эту куку';
COMMENT ON COLUMN web_visitors.last_seen_at IS 'Последний заход на сайт, не признак онлайна в чате';

COMMENT ON COLUMN chat_sessions.partner_id IS 'С кем пользователь сейчас говорит';
COMMENT ON COLUMN chat_sessions.searching_since IS 'Когда пользователь встал в очередь поиска';
COMMENT ON COLUMN chat_sessions.platform IS 'vk, tg, web или engine — аккаунт chat-engine вместо живого собеседника';
COMMENT ON COLUMN bot_users.handle IS 'Как найти человека: https://vk.com/id… или @username Telegram';
COMMENT ON COLUMN bot_users.search_count IS 'Сколько раз запускал поиск собеседника, сумма по всем чатам';
COMMENT ON COLUMN bot_users.last_seen_at IS 'Последнее сообщение или поиск: по нему считается онлайн';
COMMENT ON COLUMN user_channels.search_count IS 'Сколько раз искал собеседника через этот чат';
COMMENT ON COLUMN user_channels.last_seen_at IS 'Последняя активность человека в этом чате';
COMMENT ON TABLE channels IS 'Чаты, куда подключён бот: сообщества ВК, бот Telegram, сайт';
COMMENT ON TABLE user_channels IS 'Через какие чаты уже общался человек';
COMMENT ON TABLE engine_presence IS 'Аккаунты chat-engine. Это не пользователи: у всех один user_id';

-- Файл уже лежит в S3. Пересылка собеседнику сюда не входит: её делает следующий шаг.
CREATE TABLE IF NOT EXISTS stored_media (
    id UUID PRIMARY KEY,
    platform TEXT NOT NULL,
    bot_key TEXT NOT NULL,
    user_id BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('photo', 'video', 'video_note', 'voice')),
    object_key TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS stored_media_owner_idx
    ON stored_media (platform, bot_key, user_id, created_at DESC);

COMMENT ON TABLE stored_media IS 'Медиа, которое человек прислал в диалог. Объект лежит в S3 по object_key';
COMMENT ON COLUMN stored_media.kind IS 'photo, video, video_note — кружок, voice — голосовое';
