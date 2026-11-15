"""What the distill line is doing, read the same way the worker leases it.

Completed work is counted inside a time window. The live line — running,
waiting, and stopped — is the queue as it is now. A stopped row is still a
task; hiding it would make the line look shorter than it is.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rogmem.cost import buckets, span_bounds
from rogmem.progress import STAGES, flow_states
from rogmem.queue_priority import effective_priority, normalize_lane

SHANGHAI = timezone(timedelta(hours=8))
PAGE_SIZE = 50
SPANS = ("24h", "7d", "30d", "all")
TABS = ("all", "done", "queued", "leased")
TAB_LABELS = {"all": "全部", "done": "已完成", "queued": "排队中", "leased": "进行中"}
STATE_LABELS = {"queued": "排队中", "leased": "进行中", "done": "已完成", "failed": "未跑完"}


def _parse(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clock(value: str | None) -> str:
    moment = _parse(value)
    if moment is None:
        return ""
    return moment.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")


def _spent(start: str | None, end: str | None) -> str:
    opened = _parse(start)
    closed = _parse(end)
    if opened is None or closed is None:
        return ""
    seconds = int((closed - opened).total_seconds())
    if seconds < 0:
        return ""
    if seconds < 60:
        return "不到 1 分钟"
    minutes = seconds // 60
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} 小时 {rest} 分钟"
    if hours:
        return f"{hours} 小时"
    return f"{minutes} 分钟"


def _stage(pipeline: str, status: str) -> str:
    states = flow_states(pipeline or "", status or "")
    for name, state in states:
        if state in {"run", "retry", "fail"}:
            return name
    last_ok = ""
    for name, state in states:
        if state == "ok":
            last_ok = name
        elif state == "wait":
            return name
    if last_ok == STAGES[-1]:
        return "完成"
    return last_ok or STAGES[0]


def _priority(lane: str, kind: str, ingested_at: str, now: datetime) -> tuple[int, str]:
    base = normalize_lane(lane, kind)
    rank = effective_priority(base, ingested_at, now)
    if base < 0:
        return rank, "插队"
    return rank, f"P{rank}"


def _name(filename: str, preview: str, source_id: str) -> str:
    text = (filename or "").strip() or " ".join((preview or "").split()) or source_id
    return text


_COLUMNS = """q.id AS qid, q.state, q.lane, q.attempts, q.last_error,
             q.enqueued_at, q.started_at, q.finished_at,
             q.source_id AS source_id, IFNULL(c.kind, '') AS kind, IFNULL(c.filename, '') AS filename,
             IFNULL(c.body_preview, '') AS preview,
             COALESCE(c.body_chars, 0) AS chars,
             IFNULL(c.ingested_at, '') AS ingested_at,
             IFNULL(c.pipeline, '') AS pipeline,
             IFNULL(c.status, '') AS status"""
_FROM = "FROM queue q LEFT JOIN source_cards c ON c.id = q.source_id"
# Card text stays in this statement so a reader can see the page does not scan sources.body.
_ROW_SQL = f"SELECT {_COLUMNS} {_FROM}"
_PAGE_COLUMNS = (
    "qid, state, lane, attempts, last_error, enqueued_at, started_at, finished_at, "
    "source_id, kind, filename, preview, chars, ingested_at, pipeline, status"
)


def _jd(col: str) -> str:
    """Julian day of a clock, including the lowercase-t form writers sometimes store."""

    return f"julianday(replace(trim({col}), 't', 'T'))"


def _bucket_expr(col: str, grain: str) -> str:
    clock = f"datetime(({_jd(col)} - 2440587.5) * 86400, 'unixepoch', '+8 hours')"
    shape = "%Y-%m-%d %H:00:00" if grain == "hour" else "%Y-%m-%d 00:00:00"
    return f"strftime('{shape}', {clock})"


def _base_sql() -> str:
    lane = "lower(trim(IFNULL(q.lane, '')))"
    kind = "IFNULL(c.kind, '')"
    return f"""CASE
        WHEN {lane} = 'jump' THEN -1
        WHEN {lane} IN ('p0', '0', 'fast') THEN 0
        WHEN {lane} IN ('p1', '1') THEN 1
        WHEN {lane} IN ('p2', '2') THEN 2
        WHEN {lane} IN ('p3', '3') THEN 3
        WHEN {lane} = 'slow' AND {kind} IN ('user', 'agent') THEN 1
        WHEN {lane} = 'slow' AND {kind} = 'file' THEN 2
        ELSE 3
    END"""


def _rank_sql() -> str:
    """Same promotion as effective_priority, using the card's ingested clock."""

    base = _base_sql()
    ingested = _jd("c.ingested_at")
    return f"""CASE
        WHEN ({base}) < 0 THEN ({base})
        WHEN ({base}) <= 0 THEN 0
        WHEN {ingested} IS NULL OR {ingested} > julianday(?) THEN ({base})
        ELSE max(1, ({base}) - CAST((julianday(?) - {ingested}) * 86400.0 / 1800.0 AS INTEGER))
    END"""


def _fetch(con, sql: str, params=()):
    return con.execute(sql, tuple(params)).fetchall()


def _num(value) -> int:
    return int(value or 0)


def _rate(count: float, start: datetime, end: datetime) -> float:
    hours = max((end - start).total_seconds() / 3600, 1)
    return count / hours


def _public_row(row: dict, now: datetime) -> dict:
    rank, label = _priority(row["lane"], row["kind"], row["ingested_at"], now)
    end = row.get("finished_at") or ""
    if row["state"] == "leased" and not end:
        end = now.isoformat()
    return {
        "qid": row["qid"],
        "source_id": row["source_id"],
        "name": _name(row["filename"], row["preview"], row["source_id"]),
        "imported_at": _clock(row["ingested_at"]),
        "enqueued_at": _clock(row.get("enqueued_at") or row["ingested_at"]),
        "stage": _stage(row["pipeline"], row["status"]),
        "state": row["state"],
        "state_label": STATE_LABELS.get(row["state"], row["state"]),
        "priority": label,
        "rank": rank,
        "spent": _spent(row.get("started_at"), end) if row["state"] in {"leased", "done"} else "",
        "note": (row.get("last_error") or "").strip(),
        "chars": int(row["chars"] or 0),
    }


def _slot_key(slot: datetime, grain: str) -> str:
    if grain == "hour":
        return slot.strftime("%Y-%m-%d %H:00:00")
    return slot.strftime("%Y-%m-%d 00:00:00")


def _earliest(con) -> datetime | None:
    queued = f"COALESCE({_jd('q.enqueued_at')}, {_jd('c.ingested_at')})"
    found = _fetch(
        con,
        f"""SELECT MIN(jd) FROM (
                SELECT MIN(finished_jd) AS jd FROM queue WHERE state = 'done' AND finished_jd IS NOT NULL
                UNION ALL
                SELECT MIN({queued}) AS jd {_FROM} WHERE q.state = 'queued'
            )""",
    )
    jd = found[0][0] if found else None
    if jd is None:
        return None
    return datetime.fromtimestamp((float(jd) - 2440587.5) * 86400, timezone.utc)


def _done_window(span: str, start: datetime, end: datetime) -> tuple[str, list]:
    """Finished rows in this span. The instant is stored so the read can stop.

    A string bound is not a superset of +08:00, fractional seconds, and lowercase t.
    """

    if span == "all":
        return "q.state = 'done'", []
    return (
        "q.state = 'done' AND q.finished_jd IS NOT NULL "
        "AND q.finished_jd >= julianday(?) AND q.finished_jd <= julianday(?)",
        [start.isoformat(), end.isoformat()],
    )


def _pair(con, where: str, params) -> tuple[int, int]:
    row = _fetch(
        con,
        f"SELECT COUNT(*), SUM(COALESCE(c.body_chars, 0)) {_FROM} WHERE {where}",
        params,
    )[0]
    return _num(row[0]), _num(row[1])


def _totals(con, span: str, start: datetime, end: datetime) -> dict[str, int]:
    queued, queued_chars = _pair(con, "q.state = 'queued'", ())
    leased, _chars = _pair(con, "q.state = 'leased'", ())
    failed, _chars = _pair(con, "q.state = 'failed'", ())
    other, _chars = _pair(con, "q.state NOT IN ('queued', 'leased', 'failed', 'done')", ())
    where, params = _done_window(span, start, end)
    done, done_chars = _pair(con, where, params)
    return {
        "queued": queued,
        "queued_chars": queued_chars,
        "leased": leased,
        "failed": failed,
        "other": other,
        "done": done,
        "done_chars": done_chars,
    }


def _series(con, span: str, start: datetime, end: datetime, grain: str) -> tuple[list[dict], int, int]:
    slots = buckets(start, end, grain)
    task_counts = {slot: 0 for slot in slots}
    char_counts = {slot: 0 for slot in slots}
    index = {_slot_key(slot, grain): slot for slot in slots}
    untimed_tasks = 0
    untimed_chars = 0
    # A bounded span has no place to put a clock that does not parse. History does.
    if span == "all":
        blank = _fetch(
            con,
            f"""SELECT COUNT(*), SUM(COALESCE(c.body_chars, 0))
                {_FROM}
                WHERE q.state = 'done' AND q.finished_jd IS NULL""",
        )[0]
        untimed_tasks += _num(blank[0])
        untimed_chars += _num(blank[1])
    dated_where, dated_params = _done_window(span, start, end)
    if span == "all":
        dated_where = "q.state = 'done' AND q.finished_jd IS NOT NULL"
        dated_params = []
    for slot_key, tasks, chars in _fetch(
        con,
        f"""SELECT {_bucket_expr('q.finished_at', grain)}, COUNT(*), SUM(COALESCE(c.body_chars, 0))
            {_FROM}
            WHERE {dated_where}
            GROUP BY 1""",
        dated_params,
    ):
        slot = index.get(str(slot_key or ""))
        if slot is None:
            untimed_tasks += _num(tasks)
            untimed_chars += _num(chars)
            continue
        task_counts[slot] += _num(tasks)
        char_counts[slot] += _num(chars)
    series = [
        {
            "at": slot.isoformat(),
            "label": slot.strftime("%m-%d %H:%M") if grain == "hour" else slot.strftime("%m-%d"),
            "tasks": task_counts[slot],
            "chars": char_counts[slot],
        }
        for slot in slots
    ]
    return series, untimed_tasks, untimed_chars


def _ranked_select(where: str, wait: str, finished: str) -> str:
    return f"""SELECT {_COLUMNS},
                   CASE q.state
                       WHEN 'leased' THEN 0
                       WHEN 'failed' THEN 1
                       WHEN 'queued' THEN 2
                       WHEN 'done' THEN 3
                       ELSE 4
                   END AS group_ord,
                   CASE WHEN q.state = 'done' THEN {finished} ELSE ({_rank_sql()}) END AS sort_main,
                   CASE WHEN q.state = 'done' THEN q.id ELSE COALESCE(q.attempts, 0) END AS sort_attempts,
                   CASE WHEN q.state = 'done' THEN 0 ELSE {wait} END AS sort_wait
            {_FROM}
            WHERE {where}"""


def _page_branches(tab: str, span: str, start: datetime, end: datetime) -> list[tuple[str, list]]:
    """Each branch names one state. History is not mixed into the live line."""

    live = (
        ("q.state = 'queued'", []),
        ("q.state = 'leased'", []),
        ("q.state = 'failed'", []),
        ("q.state NOT IN ('queued', 'leased', 'failed', 'done')", []),
    )
    if tab == "queued":
        return [live[0]]
    if tab == "leased":
        return [live[1]]
    if tab == "done":
        return [_done_window(span, start, end)]
    return [*live, _done_window(span, start, end)]


def _page_rows(con, tab: str, span: str, start: datetime, end: datetime, moment: datetime, offset: int) -> list[dict]:
    # Running and stopped sit in front of the waiting line. Inside the waiting
    # line: higher priority, fewer attempts, then the longer wait. Finished
    # rows are the most recent first. qid breaks the remaining ties.
    wait = f"COALESCE({_jd('q.enqueued_at')}, {_jd('c.ingested_at')}, 1000000000000)"
    finished = "-COALESCE(q.finished_jd, -1000000000000)"
    now = [moment.isoformat(), moment.isoformat()]
    parts: list[str] = []
    params: list = []
    for where, extra in _page_branches(tab, span, start, end):
        parts.append(_ranked_select(where, wait, finished))
        params.extend(now)
        params.extend(extra)
    params.extend((PAGE_SIZE, offset))
    found = _fetch(
        con,
        f"""SELECT {_PAGE_COLUMNS} FROM (
                {' UNION ALL '.join(parts)}
            )
            ORDER BY group_ord, sort_main, sort_attempts, sort_wait, qid
            LIMIT ? OFFSET ?""",
        params,
    )
    return [dict(row) for row in found]


def view(span: str = "24h", tab: str = "all", page: int = 1, now: datetime | None = None) -> dict:
    """One consistent snapshot: aggregates for the line, one page of cards."""

    from rogmem.engine import db

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    chosen_span = span if span in SPANS else "24h"
    chosen_tab = tab if tab in TABS else "all"
    end = moment.astimezone(timezone.utc)
    con = db()
    own = not con.in_transaction
    if own:
        con.execute("BEGIN")
    try:
        earliest = _earliest(con) if chosen_span == "all" else None
        start, grain = span_bounds(chosen_span, moment, earliest)
        totals = _totals(con, chosen_span, start, end)
        series, untimed_tasks, untimed_chars = _series(con, chosen_span, start, end, grain)
        done_tasks = totals["done"]
        queued_tasks = totals["queued"]
        total = {
            "done": done_tasks,
            "queued": queued_tasks,
            "leased": totals["leased"],
            "all": queued_tasks + totals["leased"] + totals["failed"] + totals["other"] + done_tasks,
        }[chosen_tab]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE) if total else 1
        current = page if page > 0 else 1
        if current > pages:
            current = pages
        offset = (current - 1) * PAGE_SIZE
        raw_rows = _page_rows(con, chosen_tab, chosen_span, start, end, moment, offset) if total else []
    finally:
        if own:
            con.rollback()
    page_rows = [_public_row(row, moment) for row in raw_rows]
    return {
        "span": chosen_span,
        "tab": chosen_tab,
        "page": current,
        "pages": pages,
        "page_size": PAGE_SIZE,
        "total": total,
        "rows": page_rows,
        "counts": {
            "all": queued_tasks + totals["leased"] + totals["failed"] + totals["other"] + done_tasks,
            "done": done_tasks,
            "queued": queued_tasks,
            "leased": totals["leased"],
        },
        "summary": {
            "done_tasks": done_tasks,
            "done_chars": totals["done_chars"],
            "queued_tasks": queued_tasks,
            "queued_chars": totals["queued_chars"],
            "per_hour_tasks": _rate(done_tasks, start, end),
            "per_hour_chars": _rate(totals["done_chars"], start, end),
            "running": totals["leased"],
            "stopped": totals["failed"],
            "untimed_tasks": untimed_tasks,
            "untimed_chars": untimed_chars,
        },
        "series": series,
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "grain": grain,
            "tz": "UTC+8",
        },
    }
