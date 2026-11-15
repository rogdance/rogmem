"""One distill unit is a run of a session, not one stored row.

Writers often mint a fresh turn id per line. That string does not decide the
unit. Lines that share a turn id and both roles are one unit. Everything else
in a session is walked in time order: a run closes when it contains both a
user line and an agent line. A run that is still one role stays together and
waits. An empty session id is not a session, so those rows stay single units.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Side:
    source_id: str
    session_id: str
    turn_id: str
    kind: str
    event_at: str
    ingested_at: str
    rowid: int
    pair_ready: int = 0
    wait_until: str = ""
    qid: int = 0
    lane: str = ""
    attempts: int = 0
    chars: int = 0


@dataclass(frozen=True)
class Exchange:
    sides: tuple[Side, ...]

    @property
    def paired(self) -> bool:
        kinds = {side.kind for side in self.sides}
        return "user" in kinds and "agent" in kinds


def _instant_key(value: str) -> tuple:
    """Missing clocks sort first. A real clock is the instant, not the spelling of the offset."""
    parsed = _parse_wait(str(value or ""))
    if parsed is None:
        return (0, str(value or ""))
    return (1, parsed.timestamp())


def _order_key(side: Side) -> tuple:
    return (
        _instant_key(side.event_at or side.ingested_at or ""),
        _instant_key(side.ingested_at or ""),
        side.rowid,
        side.source_id,
    )


def _parse_wait(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# History is already finished, so one session is one distill until it is too long to read.
# A single ask-and-answer run stays together even when it is longer than this.
HISTORY_LANES = frozenset({"p1", "slow", "1"})
SESSION_DISTILL_CHARS = 80_000


def is_history_lane(lane: str) -> bool:
    return (lane or "").strip().lower() in HISTORY_LANES


def form_exchanges(sides: list[Side]) -> list[Exchange]:
    """Group rows that must leave the queue as one distill.

    A live session still closes each run. A history session stays one dialogue,
    and splits only between runs when the joined text is too long to read at once.
    """
    history = [side for side in sides if is_history_lane(side.lane)]
    live = [side for side in sides if not is_history_lane(side.lane)]
    exchanges = _form_turn_runs(live)
    exchanges.extend(_form_history_sessions(history))
    exchanges.sort(key=lambda item: _order_key(item.sides[0]))
    return exchanges


def _form_turn_runs(sides: list[Side]) -> list[Exchange]:
    """Group rows that must leave the queue as one distill."""
    by_turn: dict[str, list[Side]] = {}
    for side in sides:
        turn = (side.turn_id or "").strip()
        if not turn:
            continue
        by_turn.setdefault(turn, []).append(side)
    taken: set[str] = set()
    exchanges: list[Exchange] = []
    for group in by_turn.values():
        kinds = {side.kind for side in group}
        if "user" not in kinds or "agent" not in kinds:
            continue
        ordered = tuple(sorted(group, key=_order_key))
        exchanges.append(Exchange(ordered))
        taken.update(side.source_id for side in group)

    by_session: dict[str, list[Side]] = {}
    loose: list[Side] = []
    for side in sides:
        if side.source_id in taken:
            continue
        session = (side.session_id or "").strip()
        if not session:
            loose.append(side)
            continue
        by_session.setdefault(session, []).append(side)
    for side in loose:
        exchanges.append(Exchange((side,)))
    for session_sides in by_session.values():
        exchanges.extend(_close_runs(session_sides))
    return exchanges


def _form_history_sessions(sides: list[Side]) -> list[Exchange]:
    """One finished session is one dialogue. An empty session id is not a session."""
    by_session: dict[str, list[Side]] = {}
    loose: list[Side] = []
    for side in sides:
        session = (side.session_id or "").strip()
        if not session:
            loose.append(side)
            continue
        by_session.setdefault(session, []).append(side)
    exchanges = [Exchange((side,)) for side in loose]
    for session_sides in by_session.values():
        exchanges.extend(_pack_history_session(session_sides))
    return exchanges


def _pack_history_session(sides: list[Side]) -> list[Exchange]:
    """Pack whole ask-and-answer runs. The newest run is its own read when the rest does not fit."""
    runs = _close_runs(sides)
    bins: list[list[Exchange]] = []
    current: list[Exchange] = []
    current_chars = 0
    for run in reversed(runs):
        weight = sum(max(0, int(side.chars or 0)) for side in run.sides)
        if current and current_chars + weight > SESSION_DISTILL_CHARS:
            bins.append(current)
            current = []
            current_chars = 0
        current.append(run)
        current_chars += weight
    if current:
        bins.append(current)
    packed: list[Exchange] = []
    for batch in bins:
        ordered = tuple(side for run in reversed(batch) for side in run.sides)
        packed.append(Exchange(ordered))
    return packed


def order_history_newest_first(items: list[dict]) -> list[dict]:
    """Sessions closer to now come first. Inside a session, the later line comes first.

    The distill read puts the lines back into time order. This order is only
    which material enters the queue before the rest.
    """
    groups: dict[str, list[dict]] = {}
    seen: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("session_id") or "")
        if key not in groups:
            groups[key] = []
            seen.append(key)
        groups[key].append(item)

    def latest(key: str) -> tuple:
        return max((_instant_key(row.get("event_at") or "") for row in groups[key]), default=(0, ""))

    ordered: list[dict] = []
    for key in sorted(seen, key=latest, reverse=True):
        lines = groups[key]
        lines.sort(key=lambda row: _instant_key(row.get("event_at") or ""), reverse=True)
        ordered.extend(lines)
    return ordered


def chunk_whole_sessions(items: list[dict], limit: int = 40) -> list[list[dict]]:
    """Upload batches keep each session together. A long session is one batch, not a cut at ``limit``."""
    size = limit if isinstance(limit, int) and limit > 0 else 40
    groups: list[list[dict]] = []
    current_key = None
    buf: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("session_id") or "")
        if buf and key != current_key:
            groups.append(buf)
            buf = []
        current_key = key
        buf.append(item)
    if buf:
        groups.append(buf)
    batches: list[list[dict]] = []
    current: list[dict] = []
    for group in groups:
        if current and len(current) + len(group) > size:
            batches.append(current)
            current = []
        current.extend(group)
        if len(current) >= size:
            batches.append(current)
            current = []
    if current:
        batches.append(current)
    return batches


def _close_runs(sides: list[Side]) -> list[Exchange]:
    """Close a run the moment it holds both roles. The rest stays one open run."""
    pending: list[Side] = []
    closed: list[Exchange] = []
    for side in sorted(sides, key=_order_key):
        pending.append(side)
        kinds = {item.kind for item in pending}
        if "user" in kinds and "agent" in kinds:
            closed.append(Exchange(tuple(pending)))
            pending = []
    if pending:
        closed.append(Exchange(tuple(pending)))
    return closed


def _side_due(side: Side, now: datetime) -> bool:
    if int(side.pair_ready or 0):
        return True
    deadline = _parse_wait(side.wait_until)
    if deadline is None:
        return False
    clock = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return deadline <= clock.astimezone(timezone.utc)


def exchange_ready(exchange: Exchange, now: datetime) -> bool:
    """A live run with both roles can leave now.

    A history session stays until each line is due. Both roles being present
    is not enough: a later upload batch may still belong to the same session.
    """
    if not exchange.sides:
        return False
    if exchange.paired and not all(is_history_lane(side.lane) for side in exchange.sides):
        return True
    return all(_side_due(side, now) for side in exchange.sides)


def join_exchange_text(rows: list[dict]) -> str:
    """One read for the model. A single side stays the original text."""
    if len(rows) <= 1:
        return str((rows[0].get("body") if rows else "") or "")

    def key(row: dict) -> tuple:
        return (
            _instant_key(str(row.get("event_at") or row.get("ingested_at") or "")),
            _instant_key(str(row.get("ingested_at") or "")),
            int(row.get("rowid") or 0),
            str(row.get("id") or ""),
        )

    labels = {"user": "用户", "agent": "助手", "summary": "摘要"}
    parts: list[str] = []
    for row in sorted(rows, key=key):
        label = labels.get(str(row.get("kind") or ""), "材料")
        body = str(row.get("body") or "").strip()
        parts.append(f"{label}\n{body}" if body else label)
    return "\n\n".join(parts)


def flag_updates(exchanges: list[Exchange], *, now_iso: str, wait_seconds: int) -> list[tuple[str, int, str | None, bool]]:
    """How to store readiness after a write.

    Returns (source_id, pair_ready, wait_until, keep_existing_wait).
    A closed run is ready. An open run keeps the deadline it already has.
    """
    from datetime import timedelta

    fresh = ""
    updates: list[tuple[str, int, str | None, bool]] = []
    for exchange in exchanges:
        if exchange.paired:
            for side in exchange.sides:
                updates.append((side.source_id, 1, None, False))
            continue
        for side in exchange.sides:
            if (side.wait_until or "").strip():
                updates.append((side.source_id, 0, side.wait_until, True))
                continue
            if not fresh:
                start = _parse_wait(now_iso) or datetime.now(timezone.utc)
                fresh = (start + timedelta(seconds=max(0, int(wait_seconds)))).isoformat()
            updates.append((side.source_id, 0, fresh, False))
    return updates
