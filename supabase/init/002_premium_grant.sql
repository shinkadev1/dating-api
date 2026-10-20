-- Срок премиума. Повторный запуск не продлевает уже записанную дату.
-- Неделя — 7 дней, месяц — 30 дней.
UPDATE bot_users
SET premium_until = NOW() + INTERVAL '30 days'
WHERE is_premium AND premium_until IS NULL;

INSERT INTO bot_users (platform, user_id, handle, is_premium, premium_until)
VALUES ('vk', 208891943, 'https://vk.com/id208891943', TRUE, NOW() + INTERVAL '30 days')
ON CONFLICT (platform, user_id) DO UPDATE SET
    premium_until = COALESCE(bot_users.premium_until, EXCLUDED.premium_until);
