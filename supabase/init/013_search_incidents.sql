-- Журнал инцидентов поиска: человек ждал собеседника и вышел без пары (стоп или смена режима).
-- Файл идемпотентный и безопасный для живой базы. Поведение ботов не меняется.

CREATE TABLE IF NOT EXISTS search_incidents (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    platform TEXT NOT NULL,
    bot_key TEXT NOT NULL DEFAULT '',
    user_id BIGINT NOT NULL,
    mode TEXT NOT NULL,
    searching_since TIMESTAMPTZ,
    ended_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    wait_seconds INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('stopped', 'mode_changed')),
    reason TEXT NOT NULL CHECK (reason IN (
        'no_account_for_mode', 'all_accounts_unavailable', 'engine_searched_no_match',
        'bridge_not_hunting', 'left_quickly'
    )),
    snapshot JSONB
);

CREATE INDEX IF NOT EXISTS search_incidents_created_idx ON search_incidents (created_at);
CREATE INDEX IF NOT EXISTS search_incidents_reason_idx ON search_incidents (reason, created_at);

-- Аккаунты chat-engine: режим и последняя ошибка (их сообщает engine_bridge через POST /engine/{account}/status),
-- время последнего поиска по режимам (ставит hunt).
ALTER TABLE engine_presence ADD COLUMN IF NOT EXISTS mode TEXT;
ALTER TABLE engine_presence ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE engine_presence ADD COLUMN IF NOT EXISTS last_error_at TIMESTAMPTZ;
ALTER TABLE engine_presence ADD COLUMN IF NOT EXISTS last_hunt_normal_at TIMESTAMPTZ;
ALTER TABLE engine_presence ADD COLUMN IF NOT EXISTS last_hunt_adult_at TIMESTAMPTZ;
