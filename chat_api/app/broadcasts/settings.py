"""Параметры отправки рассылок. Живут в коде, как у напоминаний; после смены нужен деплой."""

from __future__ import annotations

from datetime import date

# Как часто проверять, каким рассылкам пора.
CHECK_EVERY_SECONDS = 60
# Сколько получателей ставить в очередь за один шаг и пауза между шагами.
# Сама доставка у ботов идёт около 10 сообщений в секунду.
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 2.0
# Рассылки после события не уходят ночью по Москве: [начало, конец), можно через полночь.
# Рассылка по времени уходит ровно тогда, когда её поставили.
QUIET_HOURS_MSK = (23, 10)

# Ежедневная рассылка-промо: момент отправки на каждый день случайный в окне [начало, конец) часов по Москве.
DAILY_PROMO_WINDOW_MSK = (19, 21)
# С этого часа по Москве за текущий день не отправляется.
DAILY_PROMO_UNTIL_MSK = 23
DAILY_PROMO_PLATFORMS = ("vk", "tg")
# Экраны бота (notice.screen) и запасные тексты чередуются по дням по Москве, одинаково для всех:
# день DAILY_PROMO_ROTATION_START — первый вариант, следующий — второй и т.д. по кругу.
DAILY_PROMO_ROTATION_START = date(2026, 11, 14)
DAILY_PROMO_ROTATION = (
    ("daily_hello", "Давай начнем с простого «Привет» и посмотрим, куда это нас приведет ☕✨"),
    ("daily_promo", "😏👇 В пошлом чате горячо, оценишь?"),
)


def daily_promo_for(day: date) -> tuple[str, str]:
    """Экран и запасной текст ежедневной рассылки на день по Москве."""
    index = (day - DAILY_PROMO_ROTATION_START).days % len(DAILY_PROMO_ROTATION)
    return DAILY_PROMO_ROTATION[index]
