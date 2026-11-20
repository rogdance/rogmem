"""Reciprocal rank fusion and per-lane caps for hybrid search.

RRF k=60 matches Hindsight. ``cap_per_source`` is ported from
Hindsight ``engine/search/fusion.py`` so one over-expanding lane cannot
crowd the others before fusion. ``select_with_temporal_coverage`` is
ported from Hindsight ``memories/pg/recall.py`` so a temporal window
does not collapse onto the densest recent slice.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

RRF_K = 60

# Default per-lane cap before RRF. Hindsight defaults this to 0 (off) via
# config; RogMem turns it on so graph/temporal cannot flood identity/FTS.
DEFAULT_LANE_CAP = 20


def cap_per_source(results: list, cap: int) -> list:
    """Truncate a single retrieval arm to its top-``cap`` results.

    Applied per source before fusion so that one over-expanding backend
    cannot crowd out the others. The caller is responsible for sorting
    ``results`` by relevance first; this only slices.

    Ported from Hindsight ``fusion.cap_per_source``.
    """
    if cap <= 0 or len(results) <= cap:
        return results
    return results[:cap]


def rrf_fuse(rankings: list[list[tuple[str, dict]]], *, k: int = RRF_K) -> list[dict]:
    """Fuse ranked lanes. Rank 1 is the best hit in that lane. k=60.

    A hit that shows up in more than one lane keeps the payload from the
    earlier lane and adds the later lane's score.
    """
    if k < 1:
        raise ValueError("k 必须是正整数")
    scores: dict[str, float] = {}
    payload: dict[str, dict] = {}
    origin: dict[str, tuple[int, int]] = {}
    for lane, ranking in enumerate(rankings):
        for rank, (key, item) in enumerate(ranking, start=1):
            if not key:
                continue
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            payload.setdefault(key, item)
            origin.setdefault(key, (lane, rank))
    order = sorted(scores, key=lambda key: (-scores[key], origin[key]))
    return [payload[key] for key in order]


def select_with_temporal_coverage(
    pool: list[dict[str, Any]],
    start_date: datetime,
    end_date: datetime,
    limit: int,
    n_buckets: int = 8,
    *,
    date_key: str = "event_at",
    score_key: str = "similarity",
) -> list[dict[str, Any]]:
    """Pick ``limit`` entry points from a similarity-ranked pool, spread across the window.

    Ported from Hindsight ``_select_with_temporal_coverage``: the window is
    split into equal time-buckets; candidates are taken round-robin across
    populated buckets so a dense recent cluster cannot monopolise the lane.
    """
    if len(pool) <= limit:
        return list(pool)

    ranked = sorted(pool, key=lambda r: float(r.get(score_key) or 0.0), reverse=True)
    span = (end_date - start_date).total_seconds()

    def _bucket(row: dict[str, Any]) -> int:
        d = row.get(date_key)
        if not isinstance(d, datetime) or span <= 0:
            return 0
        frac = (d - start_date).total_seconds() / span
        return max(0, min(int(frac * n_buckets), n_buckets - 1))

    buckets: dict[int, list] = {}
    for row in ranked:
        buckets.setdefault(_bucket(row), []).append(row)

    selected: list[dict[str, Any]] = []
    tier = 0
    while len(selected) < limit and any(len(b) > tier for b in buckets.values()):
        tier_rows = [b[tier] for b in buckets.values() if len(b) > tier]
        tier_rows.sort(key=lambda r: float(r.get(score_key) or 0.0), reverse=True)
        for row in tier_rows:
            if len(selected) < limit:
                selected.append(row)
        tier += 1
    return selected
