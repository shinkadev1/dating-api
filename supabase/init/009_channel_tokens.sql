-- Сообщества ВК, подключаемые из CRM: ключ (в зашифрованном виде), подсказка, фото, переключатель и статус.
-- Только добавление колонок: файл читается при каждом старте.
ALTER TABLE channels
    ADD COLUMN IF NOT EXISTS token_encrypted TEXT,
    ADD COLUMN IF NOT EXISTS token_hint TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS photo_url TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS status_error TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS token_updated_at TIMESTAMPTZ;
