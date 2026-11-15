"""Reciprocal rank fusion for statement text, chunk text, and chunk vectors."""

from __future__ import annotations

RRF_K = 60


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
