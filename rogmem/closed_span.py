"""Shanghai buckets for a closed interval plus the still-open tail.

Cards fold on this clock (UTC+8). A bucket is closed only when its end is at
or before `now`. The hour that contains `now` stays open, and so does any hour
the window only clips. Callers seal a closed hour after folding just that hour,
and drop the seal when a later write lands inside it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def floor_shanghai(moment: datetime, grain: str = "hour") -> datetime:
    local = as_utc(moment).astimezone(SHANGHAI)
    if grain == "hour":
        return local.replace(minute=0, second=0, microsecond=0)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def hour_key(moment: datetime) -> str:
    return floor_shanghai(moment, "hour").strftime("%Y-%m-%d %H:00:00")


def hour_bounds(key: str) -> tuple[datetime, datetime]:
    """UTC instants [start, end) for one Shanghai hour key."""

    local = datetime.strptime(key, "%Y-%m-%d %H:00:00").replace(tzinfo=SHANGHAI)
    start = local.astimezone(timezone.utc)
    return start, (local + timedelta(hours=1)).astimezone(timezone.utc)


def hour_slot_sql(clock_sql: str) -> str:
    """Same Shanghai hour key SQLite uses when it stores the sealed bucket."""

    parsed = f"julianday(replace(trim({clock_sql}), 't', 'T'))"
    return (
        "strftime('%Y-%m-%d %H:00:00', datetime(("
        + parsed
        + " - 2440587.5) * 86400, 'unixepoch', '+8 hours'))"
    )


def closed_hour_keys(window_start: datetime, now: datetime) -> list[str]:
    """Hours fully inside the window whose end is already past.

    The hour containing `now` is open. An hour that starts before the window
    is only partly inside it, so it is not sealed as a whole.
    """

    now_u = as_utc(now)
    start_u = as_utc(window_start)
    open_local = floor_shanghai(now_u, "hour")
    open_start = open_local.astimezone(timezone.utc)
    if start_u >= open_start:
        return []
    local = floor_shanghai(start_u, "hour")
    if start_u > local.astimezone(timezone.utc):
        local += timedelta(hours=1)
    keys: list[str] = []
    while local < open_local:
        keys.append(local.strftime("%Y-%m-%d %H:00:00"))
        local += timedelta(hours=1)
    return keys


def partial_prefix(window_start: datetime, now: datetime) -> tuple[datetime, datetime] | None:
    """The clipped start of the window, exclusive of the next Shanghai hour."""

    now_u = as_utc(now)
    start_u = as_utc(window_start)
    open_start = floor_shanghai(now_u, "hour").astimezone(timezone.utc)
    if start_u >= open_start:
        return None
    local = floor_shanghai(start_u, "hour")
    hour_start = local.astimezone(timezone.utc)
    if start_u <= hour_start:
        return None
    end = min((local + timedelta(hours=1)).astimezone(timezone.utc), open_start)
    if start_u >= end:
        return None
    return start_u, end


def open_tail(window_start: datetime, now: datetime) -> tuple[datetime, datetime] | None:
    """Rows in the hour that contains `now`, clipped to the window."""

    now_u = as_utc(now)
    start_u = as_utc(window_start)
    tail_start = max(start_u, floor_shanghai(now_u, "hour").astimezone(timezone.utc))
    if tail_start > now_u:
        return None
    return tail_start, now_u
