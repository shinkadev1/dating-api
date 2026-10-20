-- Премиум для отправки медиа. Повторный запуск только подтверждает флаг.
INSERT INTO bot_users (platform, user_id, handle, is_premium)
VALUES ('vk', 208891943, 'https://vk.com/id208891943', TRUE)
ON CONFLICT (platform, user_id) DO UPDATE SET
    is_premium = TRUE;
