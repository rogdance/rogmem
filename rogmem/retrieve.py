"""Reciprocal rank fusion and per-lane caps for hybrid search.

RRF k=60 matches Hindsight. ``cap_per_source`` is ported from
Hindsight ``engine/search/fusion.py`` so one over-expanding lane cannot
crowd the others before fusion. ``select_with_temporal_coverage`` is
ported from Hindsight ``memories/pg/recall.py`` so a temporal window
does not collapse onto the densest recent slice.

``proof_norm`` / ``apply_proof_boost`` mirror Hindsight
``reranking._PROOF_COUNT_ALPHA`` (±5% multiplicative evidence boost)
without requiring a cross-encoder — we fold the boost into RRF scores.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

RRF_K = 60

# Default per-lane cap before RRF. Hindsight defaults this to 0 (off) via
# config; RogMem turns it on so graph/temporal cannot flood identity/FTS.
DEFAULT_LANE_CAP = 20

# Hindsight ``_PROOF_COUNT_ALPHA``: max ±5% relative adjustment.
PROOF_COUNT_ALPHA = 0.1


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


# Positional defaults only for the crystal four-arm call. Callers that fuse a
# different set (e.g. chunks+vectors) must pass ``lane_names`` explicitly —
# lane identity is part of the fusion contract, not an index into a global list.
DEFAULT_CRYSTAL_LANE_NAMES = ("identity", "fts", "graph", "temporal")
DEFAULT_RAW_LANE_NAMES = ("chunks", "vectors")


def rrf_fuse_scored(
    rankings: list[list[tuple[str, dict]]],
    *,
    k: int = RRF_K,
    lane_names: list[str] | tuple[str, ...] | None = None,
) -> list[tuple[str, dict, float, dict[str, int]]]:
    """Fuse ranked lanes and keep RRF scores plus per-lane ranks.

    Returns ``(key, payload, rrf_score, source_ranks)`` best-first.
    ``source_ranks`` maps lane name → 1-based rank within that lane.
    """
    if k < 1:
        raise ValueError("k 必须是正整数")
    names = list(lane_names) if lane_names is not None else list(DEFAULT_CRYSTAL_LANE_NAMES)
    scores: dict[str, float] = {}
    payload: dict[str, dict] = {}
    origin: dict[str, tuple[int, int]] = {}
    source_ranks: dict[str, dict[str, int]] = {}
    for lane, ranking in enumerate(rankings):
        name = names[lane] if lane < len(names) else f"lane_{lane}"
        for rank, (key, item) in enumerate(ranking, start=1):
            if not key:
                continue
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            payload.setdefault(key, item)
            origin.setdefault(key, (lane, rank))
            source_ranks.setdefault(key, {})[name] = rank
    order = sorted(scores, key=lambda key: (-scores[key], origin[key]))
    return [(key, payload[key], scores[key], source_ranks[key]) for key in order]


def rrf_fuse(
    rankings: list[list[tuple[str, dict]]],
    *,
    k: int = RRF_K,
    lane_names: list[str] | tuple[str, ...] | None = None,
) -> list[dict]:
    """Fuse ranked lanes. Rank 1 is the best hit in that lane. k=60.

    A hit that shows up in more than one lane keeps the payload from the
    earlier lane and adds the later lane's score.
    """
    return [
        item
        for _key, item, _score, _ranks in rrf_fuse_scored(
            rankings, k=k, lane_names=lane_names
        )
    ]


def proof_norm(proof_count: int | None) -> float:
    """Log-normalized evidence strength centered at 0.5 (Hindsight formula).

    proof_count=1 → 0.5 (neutral); large counts asymptote toward 1.0.
    """
    if proof_count is None or proof_count < 1:
        return 0.5
    return min(1.0, max(0.0, 0.5 + (math.log(proof_count) / 10.0)))


def proof_boost(proof_count: int | None, *, alpha: float = PROOF_COUNT_ALPHA) -> float:
    """Multiplicative boost in ``[1 - alpha/2, 1 + alpha/2]``."""
    return 1.0 + alpha * (proof_norm(proof_count) - 0.5)


def apply_proof_boost(
    scored: list[tuple[str, dict, float, dict[str, int]]],
    proof_counts: dict[str, int],
    *,
    alpha: float = PROOF_COUNT_ALPHA,
) -> list[tuple[str, dict, float, dict[str, Any]]]:
    """Re-rank RRF hits by ``rrf_score * proof_boost``. Returns enriched rows."""
    enriched: list[tuple[str, dict, float, dict[str, Any]]] = []
    for key, item, rrf_score, ranks in scored:
        count = int(proof_counts.get(key) or 0)
        boost = proof_boost(count if count >= 1 else None, alpha=alpha)
        meta = {
            "source_ranks": ranks,
            "rrf_score": rrf_score,
            "proof_count": count,
            "proof_norm": proof_norm(count if count >= 1 else None),
            "proof_boost": boost,
            "final_score": rrf_score * boost,
        }
        enriched.append((key, item, meta["final_score"], meta))
    enriched.sort(key=lambda row: (-row[2], row[0]))
    return enriched


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
