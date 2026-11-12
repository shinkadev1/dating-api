-- Ключи сообществ ВК хранятся открытым текстом. Колонка token_encrypted остаётся для переноса старых записей.
ALTER TABLE channels ADD COLUMN IF NOT EXISTS token TEXT;
