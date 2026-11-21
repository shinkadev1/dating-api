"""Причина инцидента поиска: человек ждал собеседника и вышел без пары.

`classify_incident` — чистая функция без обращений к базе: все данные ей передают.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Вышел раньше — считаем, что человек не ждал (передумал), причину не ищем.
QUICK_LEAVE_SECONDS = 10
# Ошибка аккаунта моложе этого окна считается «аккаунт сейчас недоступен».
ERROR_WINDOW_SECONDS = 600
# Аккаунт без признаков жизни дольше этого окна считается отсутствующим.
PRESENCE_WINDOW_SECONDS = 300

REASONS = (
    "no_account_for_mode",
    "all_accounts_unavailable",
    "engine_searched_no_match",
    "bridge_not_hunting",
    "left_quickly",
)


@dataclass(frozen=True)
class AccountInfo:
    """Аккаунт chat-engine, как его видит Chat API. mode: normal, adult или any."""

    id: str
    mode: str
    state: str
    last_error: str | None = None
    last_error_at: datetime | None = None
    last_hunt_normal_at: datetime | None = None
    last_hunt_adult_at: datetime | None = None

    def serves(self, mode: str) -> bool:
        return self.mode in (mode, "any")

    def last_hunt_at(self, mode: str) -> datetime | None:
        return self.last_hunt_adult_at if mode == "adult" else self.last_hunt_normal_at

    def unavailable(self, now: datetime, error_window: float = ERROR_WINDOW_SECONDS) -> bool:
        """Недавняя ошибка (409, лимит, пауза) или лимит Telegram, о котором мост сообщил."""
        if not self.last_error:
            return False
        if self.last_error.startswith("limited"):
            return True
        return self.last_error_at is not None and (now - self.last_error_at).total_seconds() <= error_window


def account_from_row(row: dict[str, Any]) -> AccountInfo:
    # Мост ещё не сообщил режим (старый мост): chat-engine без поля mode — это normal.
    mode = row["mode"] if row.get("mode") in ("normal", "adult", "any") else "normal"
    return AccountInfo(
        id=str(row["bot_key"]),
        mode=mode,
        state=str(row.get("state") or "idle"),
        last_error=row.get("last_error"),
        last_error_at=row.get("last_error_at"),
        last_hunt_normal_at=row.get("last_hunt_normal_at"),
        last_hunt_adult_at=row.get("last_hunt_adult_at"),
    )


def classify_incident(
    *,
    wait_seconds: float,
    mode: str,
    searching_since: datetime | None,
    accounts: list[AccountInfo],
    now: datetime,
    quick_seconds: float = QUICK_LEAVE_SECONDS,
    error_window: float = ERROR_WINDOW_SECONDS,
) -> str:
    """Первое подходящее правило из пяти. `accounts` — только присутствующие аккаунты chat-engine."""
    if wait_seconds < quick_seconds:
        return "left_quickly"
    serving = [a for a in accounts if a.serves(mode)]
    if not serving:
        return "no_account_for_mode"
    if all(a.unavailable(now, error_window) for a in serving):
        return "all_accounts_unavailable"
    for account in serving:
        hunted_at = account.last_hunt_at(mode)
        if hunted_at is None:
            continue
        # Аккаунт искал после того, как человек встал в очередь, либо ищет прямо сейчас (начал раньше).
        if searching_since is None or hunted_at >= searching_since or account.state == "searching":
            return "engine_searched_no_match"
    return "bridge_not_hunting"


def build_snapshot(
    accounts: list[AccountInfo], queue: dict[str, dict[str, int]]
) -> dict[str, Any]:
    """Что видел Chat API в момент выхода: очередь по режимам и аккаунты."""
    return {
        "waiting": queue["waiting"],
        "hunting": queue["hunting"],
        "accounts": [
            {
                "id": a.id,
                "mode": a.mode,
                "state": a.state,
                "last_error": a.last_error,
                "last_error_at": a.last_error_at.isoformat() if a.last_error_at else None,
            }
            for a in accounts
        ],
    }
