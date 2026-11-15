"""Distill queue priority: map sources onto P0–P3 and pick the next lease.

Scheduling only. Does not import the merge gate, and never relaxes
evidence / continuous-substring checks.

Default mapping (v3 §5.2 numbers; architecture file is not in this branch):

- P0 即时会话
- P1 历史会话
- P2 文件夹扫
- P3 批量 / 网络 / 收藏及同类 slow

High band (effective P0–P1) and low band (effective P2–P3) interleave 8:1.
Within a band or an interleave slot, the longest wait is ``ingested_at``, not ``qid``.
Each 30 minutes in queue may promote one band, but never to P0.

Legacy ``fast`` / ``slow`` values still read through ``normalize_lane``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

JUMP = -1
P0 = 0
P1 = 1
P2 = 2
P3 = 3
PRIORITIES = (P0, P1, P2, P3)
LANE_NAMES = ("p0", "p1", "p2", "p3")

HIGH_RATIO = 8
AGE_MINUTES = 30
MAX_PROMOTE_TO = P1

ORIGIN_LIVE = "live"
ORIGIN_HISTORY = "history"
ORIGIN_SCAN = "scan"
ORIGIN_BATCH = "batch"
ORIGIN_RETRY = "retry"
ORIGIN_REBENCH = "rebench"

_STREAK_KEY = "queue_high_streak"
_UNKNOWN_WAIT = datetime.max.replace(tzinfo=timezone.utc)


def format_lane(rank: int) -> str:
    if rank == JUMP:
        return "jump"
    if rank < P0 or rank > P3:
        return "p3"
    return LANE_NAMES[rank]


def normalize_lane(lane: str | None, kind: str | None = None) -> int:
    key = (lane or "").strip().lower()
    if key == "jump":
        return JUMP
    if key in {"p0", "0", "fast"}:
        return P0
    if key in {"p1", "1"}:
        return P1
    if key in {"p2", "2"}:
        return P2
    if key in {"p3", "3"}:
        return P3
    if key == "slow":
        if kind in {"user", "agent"}:
            return P1
        if kind == "file":
            return P2
        return P3
    return P3


def assign_priority(*, kind: str, origin: str) -> int:
    if origin == ORIGIN_LIVE and kind in {"user", "agent"}:
        return P0
    if origin == ORIGIN_HISTORY:
        return P1
    if origin == ORIGIN_SCAN:
        return P2
    return P3


def lane_for(*, kind: str, origin: str | None = None, legacy_lane: str | None = None) -> str:
    if origin == ORIGIN_RETRY and legacy_lane:
        return format_lane(normalize_lane(legacy_lane, kind))
    if origin:
        return format_lane(assign_priority(kind=kind, origin=origin))
    if kind == "summary":
        return "p3"
    if legacy_lane:
        return format_lane(normalize_lane(legacy_lane, kind))
    if kind in {"user", "agent"}:
        return "p0"
    if kind == "file":
        return "p2"
    return "p3"


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def waited_since(enqueued_at: str | datetime | None) -> datetime:
    """Clock used for 「等待最久」. Missing / unparsable is not treated as oldest."""
    when = enqueued_at if isinstance(enqueued_at, datetime) else parse_iso(enqueued_at)
    if when is None:
        return _UNKNOWN_WAIT
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def effective_priority(base: int, enqueued_at: str | datetime | None, now: datetime | None = None) -> int:
    rank = int(base)
    if rank < P0:
        return rank
    if rank <= P0:
        return P0
    when = enqueued_at if isinstance(enqueued_at, datetime) else parse_iso(enqueued_at)
    if when is None:
        return rank
    clock = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    elapsed = (clock - when).total_seconds()
    if elapsed < 0:
        return rank
    steps = int(elapsed // (AGE_MINUTES * 60))
    return max(MAX_PROMOTE_TO, rank - steps)


@dataclass(frozen=True)
class QueueCandidate:
    qid: int
    source_id: str
    lane: str
    kind: str = ""
    ingested_at: str = ""
    attempts: int = 0

    @property
    def base(self) -> int:
        return normalize_lane(self.lane, self.kind)

    def effective(self, now: datetime | None = None) -> int:
        return effective_priority(self.base, self.ingested_at, now)

    @property
    def waiting_since(self) -> datetime:
        return waited_since(self.ingested_at)


def choose_next(
    candidates: list[QueueCandidate],
    *,
    now: datetime | None = None,
    high_streak: int = 0,
) -> tuple[QueueCandidate | None, int]:
    """Pick one candidate. Same-band / interleave wait uses ingested_at, not qid."""
    if not candidates:
        return None, high_streak
    clock = now or datetime.now(timezone.utc)
    scored = [(item.effective(clock), item.waiting_since, item.qid, item) for item in candidates]
    high = [row for row in scored if row[0] <= P1]
    low = [row for row in scored if row[0] >= P2]
    take_high = bool(high) and (not low or high_streak < HIGH_RATIO)
    pool = high if take_high else (low or high)

    def _order(row: tuple, *, high_band: bool) -> tuple:
        effective, waiting, qid, item = row
        # A unit that already had an attempt waits until every due unit
        # has had the same number. Priority and age only break that tie,
        # so one slow failure cannot sit on the front of the line.
        tries = item.attempts if item.attempts > 0 else 0
        if high_band:
            return (tries, effective, waiting, qid)
        return (tries, -effective, waiting, qid)

    pool.sort(key=lambda row: _order(row, high_band=take_high))
    picked = pool[0][3]
    if take_high:
        return picked, high_streak + 1
    return picked, 0


def read_high_streak(con) -> int:
    row = con.execute("SELECT v FROM settings WHERE k=?", (_STREAK_KEY,)).fetchone()
    if not row:
        return 0
    try:
        return max(0, int(row["v"]))
    except (TypeError, ValueError, KeyError):
        return 0


def write_high_streak(con, value: int) -> None:
    con.execute(
        "INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (_STREAK_KEY, str(max(0, int(value)))),
    )
