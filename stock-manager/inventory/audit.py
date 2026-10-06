
import logging
import re

import duckdb

from .models import normalize_name

log = logging.getLogger("inventory.audit")


NAME = re.compile(r"^[A-Z0-9][A-Z0-9 ._-]{0,31}$")
UNKNOWN = "?"


def clean_name(name):
    candidate = normalize_name(name)
    return candidate if candidate and NAME.match(candidate) else None


def seen(db_path, user):
    name = clean_name(user)
    if not name:
        return
    try:
        with duckdb.connect(str(db_path)) as con:
            _touch(con, name)
    except duckdb.Error:
        log.exception("could not record user activity")


def record(db_path, user, action, target=None, detail=None, address=None):

    name = clean_name(user) or UNKNOWN
    try:
        with duckdb.connect(str(db_path)) as con:
            con.execute(
                "INSERT INTO audit (user_name, action, target, detail, address) "
                "VALUES (?, ?, ?, ?, ?)",
                [name, action, target, detail or None, address],
            )
            if name != UNKNOWN:
                _touch(con, name)
    except duckdb.Error:
        log.exception("could not write audit event %s", action)


def _touch(con, name):
    con.execute(
        "INSERT INTO users (user_name) VALUES (?) "
        "ON CONFLICT (user_name) DO UPDATE SET last_seen_at = now()",
        [name],
    )


def events(db_path, limit=100, user=None):
    name = clean_name(user)
    where = "WHERE user_name = ?" if name else ""
    args = ([name] if name else []) + [int(limit)]
    with duckdb.connect(str(db_path), read_only=True) as con:
        rows = con.execute(
            f"SELECT event_id, happened_at, user_name, action, target, detail, address "
            f"FROM audit {where} ORDER BY event_id DESC LIMIT ?", args,
        ).fetchall()
    return [{"event_id": event_id, "at": at.isoformat(timespec="seconds") if at else None,
             "user": user_name, "action": action, "target": target, "detail": detail,
             "address": address}
            for event_id, at, user_name, action, target, detail, address in rows]


def users(db_path):
   
    with duckdb.connect(str(db_path), read_only=True) as con:
        rows = con.execute(
            """
            WITH activity AS (
                SELECT user_name,
                       count(*)                                  AS events,
                       max(happened_at)                          AS last_event,
                       count(*) FILTER (action = 'note.save')    AS notes,
                       count(*) FILTER (action IN ('order.save', 'list.save')) AS orders,
                       count(*) FILTER (action LIKE '%.edit'
                                        OR action IN ('order.tick', 'list.tick'))
                                                                AS corrections,
                       count(*) FILTER (action = 'login')        AS logins
                FROM audit GROUP BY user_name
            )
            SELECT coalesce(u.user_name, a.user_name),
                   coalesce(a.events, 0), coalesce(a.notes, 0), coalesce(a.orders, 0),
                   coalesce(a.corrections, 0), coalesce(a.logins, 0),
                   greatest(coalesce(a.last_event, u.last_seen_at),
                            coalesce(u.last_seen_at, a.last_event)),
                   u.created_at
            FROM users u FULL OUTER JOIN activity a ON u.user_name = a.user_name
            ORDER BY 7 DESC NULLS LAST
            """
        ).fetchall()
    return [{"user": name, "events": events_, "notes": notes, "orders": orders,
             "corrections": corrections, "logins": logins,
             "last_seen": last.isoformat(timespec="seconds") if last else None,
             "since": since.isoformat(timespec="minutes") if since else None}
            for name, events_, notes, orders, corrections, logins, last, since in rows]


def summary(db_path, limit=100):
    return {"users": users(db_path), "events": events(db_path, limit)}
