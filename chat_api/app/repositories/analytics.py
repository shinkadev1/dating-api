"""SQL продуктовой аналитики для Bot CRM. Дни считаются по Москве.

Активность — таблица user_activity (день, когда человек писал боту или искал собеседника).
Люди — bot_users (дата прихода started_at), диалоги — dialogs.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from psycopg import AsyncConnection

MSK = "Europe/Moscow"
_DAY = f"(%s AT TIME ZONE '{MSK}')::date"


def _msk(col: str) -> str:
    return _DAY.replace("%s", col)


async def today(conn: AsyncConnection) -> date:
    cursor = await conn.execute(f"SELECT {_msk('NOW()')} AS d")
    return (await cursor.fetchone())["d"]


async def summary(conn: AsyncConnection, platforms: list[str], day: date) -> dict[str, Any]:
    """Всего людей, новые за 1/7/30 дней и прошлые такие же периоды, премиум, DAU/WAU/MAU."""
    started = _msk("bu.started_at")
    cursor = await conn.execute(
        f"""
        SELECT
            COUNT(*) AS users,
            COUNT(*) FILTER (WHERE {started} = %(d)s) AS new_1,
            COUNT(*) FILTER (WHERE {started} = %(d)s - 1) AS new_1_prev,
            COUNT(*) FILTER (WHERE {started} > %(d)s - 7) AS new_7,
            COUNT(*) FILTER (WHERE {started} > %(d)s - 14 AND {started} <= %(d)s - 7) AS new_7_prev,
            COUNT(*) FILTER (WHERE {started} > %(d)s - 30) AS new_30,
            COUNT(*) FILTER (WHERE {started} > %(d)s - 60 AND {started} <= %(d)s - 30) AS new_30_prev,
            COUNT(*) FILTER (WHERE bu.premium_until > NOW()) AS premium_active,
            COUNT(*) FILTER (WHERE bu.premium_until IS NOT NULL) AS premium_ever
        FROM bot_users bu
        WHERE bu.platform = ANY(%(p)s)
        """,
        {"p": platforms, "d": day},
    )
    out = dict(await cursor.fetchone())
    cursor = await conn.execute(
        """
        SELECT
            COUNT(DISTINCT (platform, user_id)) FILTER (WHERE day = %(d)s) AS dau,
            COUNT(DISTINCT (platform, user_id)) FILTER (WHERE day > %(d)s - 7) AS wau,
            COUNT(DISTINCT (platform, user_id)) AS mau,
            MIN(day) AS since
        FROM user_activity
        WHERE platform = ANY(%(p)s) AND day > %(d)s - 30
        """,
        {"p": platforms, "d": day},
    )
    out.update(await cursor.fetchone())
    cursor = await conn.execute(
        "SELECT MIN(day) AS first_day FROM user_activity WHERE platform = ANY(%(p)s)", {"p": platforms}
    )
    out["activity_since"] = (await cursor.fetchone())["first_day"]
    out.pop("since", None)
    return out


async def daily(conn: AsyncConnection, platforms: list[str], day: date, days: int) -> list[dict[str, Any]]:
    """По дням за последние `days` дней: новые, активные, диалоги."""
    cursor = await conn.execute(
        f"""
        WITH span AS (
            SELECT generate_series(%(d)s - (%(n)s - 1), %(d)s, INTERVAL '1 day')::date AS day
        ),
        fresh AS (
            SELECT {_msk('started_at')} AS day, COUNT(*) AS n
            FROM bot_users WHERE platform = ANY(%(p)s) AND {_msk('started_at')} > %(d)s - %(n)s
            GROUP BY 1
        ),
        act AS (
            SELECT day, COUNT(*) AS n FROM user_activity
            WHERE platform = ANY(%(p)s) AND day > %(d)s - %(n)s
            GROUP BY 1
        ),
        dlg AS (
            SELECT {_msk('started_at')} AS day, COUNT(*) AS n FROM dialogs
            WHERE (a_platform = ANY(%(p)s) OR b_platform = ANY(%(p)s))
              AND started_at > NOW() - make_interval(days => %(n)s + 1)
            GROUP BY 1
        )
        SELECT span.day, COALESCE(fresh.n, 0) AS new, COALESCE(act.n, 0) AS active, COALESCE(dlg.n, 0) AS dialogs
        FROM span
        LEFT JOIN fresh ON fresh.day = span.day
        LEFT JOIN act ON act.day = span.day
        LEFT JOIN dlg ON dlg.day = span.day
        ORDER BY span.day
        """,
        {"p": platforms, "d": day, "n": days},
    )
    return list(await cursor.fetchall())


async def day_retention(conn: AsyncConnection, platforms: list[str], day: date) -> dict[str, Any]:
    """Классический retention дня N: доля пришедших, кто был активен ровно на N-й день после прихода.

    Считаем по людям, пришедшим за последние 90 дней и не раньше начала журнала активности,
    у кого N-й день уже наступил.
    """
    out: dict[str, Any] = {}
    for n in (1, 7, 30):
        cursor = await conn.execute(
            f"""
            WITH cohort AS (
                SELECT platform, user_id, {_msk('started_at')} AS d0 FROM bot_users
                WHERE platform = ANY(%(p)s)
                  AND {_msk('started_at')} <= %(d)s - %(n)s
                  AND {_msk('started_at')} > %(d)s - 90 - %(n)s
                  AND {_msk('started_at')} >= (SELECT COALESCE(MIN(day), %(d)s) FROM user_activity)
            )
            SELECT COUNT(*) AS base,
                   COUNT(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM user_activity a
                       WHERE a.platform = c.platform AND a.user_id = c.user_id AND a.day = c.d0 + %(n)s
                   )) AS kept
            FROM cohort c
            """,
            {"p": platforms, "d": day, "n": n},
        )
        row = await cursor.fetchone()
        out[f"d{n}"] = {"base": int(row["base"]), "kept": int(row["kept"])}
    return out


async def cohorts(conn: AsyncConnection, platforms: list[str], day: date, weeks: int = 8) -> list[dict[str, Any]]:
    """Недельные когорты: неделя прихода (с понедельника) и сколько из них были активны на неделе 0..N."""
    monday = day - timedelta(days=day.weekday())
    first = monday - timedelta(weeks=weeks - 1)
    cursor = await conn.execute(
        f"""
        WITH people AS (
            SELECT platform, user_id, date_trunc('week', {_msk('started_at')})::date AS w0
            FROM bot_users
            WHERE platform = ANY(%(p)s) AND {_msk('started_at')} >= %(first)s
        ),
        sizes AS (SELECT w0, COUNT(*) AS size FROM people GROUP BY w0),
        kept AS (
            SELECT p.w0, ((date_trunc('week', a.day)::date - p.w0) / 7) AS k,
                   COUNT(DISTINCT (p.platform, p.user_id)) AS n
            FROM people p
            JOIN user_activity a ON a.platform = p.platform AND a.user_id = p.user_id AND a.day >= p.w0
            GROUP BY 1, 2
        )
        SELECT s.w0, s.size, COALESCE(json_object_agg(k.k, k.n) FILTER (WHERE k.k IS NOT NULL), '{{}}') AS kept
        FROM sizes s LEFT JOIN kept k ON k.w0 = s.w0
        GROUP BY s.w0, s.size
        ORDER BY s.w0
        """,
        {"p": platforms, "first": first},
    )
    rows = await cursor.fetchall()
    result = []
    for r in rows:
        age = (monday - r["w0"]).days // 7  # сколько полных недель прошло: недели 0..age
        kept = {int(k): int(v) for k, v in (r["kept"] or {}).items()}
        result.append({
            "week": r["w0"],
            "size": int(r["size"]),
            "kept": [kept.get(i, 0) for i in range(age + 1)],
        })
    return result


async def funnel(conn: AsyncConnection, platforms: list[str]) -> list[dict[str, Any]]:
    """Пришли → искали → был диалог → 3+ диалога → покупали премиум."""
    cursor = await conn.execute(
        """
        WITH people AS (
            SELECT bu.platform, bu.user_id, bu.search_count, bu.premium_until,
                   (SELECT COUNT(*) FROM dialogs d
                    WHERE (d.a_platform = bu.platform AND d.a_user_id = bu.user_id)
                       OR (d.b_platform = bu.platform AND d.b_user_id = bu.user_id)) AS dialogs
            FROM bot_users bu WHERE bu.platform = ANY(%(p)s)
        )
        SELECT COUNT(*) AS joined,
               COUNT(*) FILTER (WHERE search_count > 0 OR dialogs > 0) AS searched,
               COUNT(*) FILTER (WHERE dialogs > 0) AS dialog,
               COUNT(*) FILTER (WHERE dialogs >= 3) AS dialog3,
               COUNT(*) FILTER (WHERE premium_until IS NOT NULL) AS premium
        FROM people
        """,
        {"p": platforms},
    )
    row = await cursor.fetchone()
    labels = [
        ("joined", "Написали боту"),
        ("searched", "Искали собеседника"),
        ("dialog", "Был хотя бы один диалог"),
        ("dialog3", "3 и больше диалогов"),
        ("premium", "Покупали премиум"),
    ]
    return [{"step": k, "label": label, "users": int(row[k])} for k, label in labels]


async def engagement(conn: AsyncConnection, platforms: list[str]) -> dict[str, Any]:
    """Диалоги за 30 дней: сколько, средняя длительность, доля закончившихся жалобой; поиски на человека."""
    cursor = await conn.execute(
        """
        SELECT COUNT(*) AS dialogs_30,
               PERCENTILE_CONT(0.5) WITHIN GROUP (
                   ORDER BY EXTRACT(EPOCH FROM (ended_at - started_at)) / 60
               ) FILTER (WHERE ended_at IS NOT NULL) AS median_minutes,
               COUNT(*) FILTER (WHERE end_reason = 'reported') AS reported_30
        FROM dialogs
        WHERE (a_platform = ANY(%(p)s) OR b_platform = ANY(%(p)s))
          AND started_at > NOW() - INTERVAL '30 days'
        """,
        {"p": platforms},
    )
    out = dict(await cursor.fetchone())
    cursor = await conn.execute(
        "SELECT AVG(search_count)::float AS searches_per_user FROM bot_users WHERE platform = ANY(%(p)s)",
        {"p": platforms},
    )
    out.update(await cursor.fetchone())
    out["median_minutes"] = float(out["median_minutes"]) if out["median_minutes"] is not None else None
    out["searches_per_user"] = float(out["searches_per_user"] or 0)
    return out
