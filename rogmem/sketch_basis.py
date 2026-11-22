"""Which page body a sketch was written from, in a form small enough to keep.

A sketch is the short read of a page that serves memory beyond this week. It does
not need to follow every merge: a paragraph added to a long page leaves its sketch
true. Rewriting it on every touch pays a whole-page read for nothing.

The basis is a bottom-k sketch over character shingles of the body: the k smallest
shingle hashes. Two bases estimate how much of the page is still the same without
keeping the old text, in one hash per shingle.
"""

from __future__ import annotations

from rogmem.pipeline_knobs import DEFAULTS as _KNOB_DEFAULTS

import hashlib
import re
from datetime import datetime, timedelta, timezone

SHINGLE = 4
KEEP = 128
# 正文和写速写时的那一版差得少于这个比例，速写仍然成立，不重写。
REWRITE_BELOW_SIMILARITY = _KNOB_DEFAULTS["sketch_rewrite_below_similarity"] / 100
# 正文有改动、但改得少时，最多拖这么久就重写一次。
STALE_AFTER = timedelta(hours=_KNOB_DEFAULTS["sketch_stale_hours"])


def _normal(body: str) -> str:
    from rogmem.pages_md import strip_source_marks

    # A page that only gained or lost 〔源 …〕 marks says the same thing; its sketch still holds.
    return re.sub(r"\s+", " ", strip_source_marks(body or "")).strip()


def body_digest(body: str) -> str:
    return hashlib.sha256(_normal(body).encode("utf-8")).hexdigest()


def signature(body: str) -> list[int]:
    """The KEEP smallest shingle hashes, ascending."""
    text = _normal(body)
    if len(text) < SHINGLE:
        text = text.ljust(SHINGLE)
    hashed = {
        int.from_bytes(hashlib.blake2b(text[i : i + SHINGLE].encode("utf-8"), digest_size=8).digest(), "big")
        for i in range(len(text) - SHINGLE + 1)
    }
    return sorted(hashed)[:KEEP]


def similarity(left: list[int], right: list[int]) -> float:
    """Bottom-k estimate of the shingle Jaccard between two bodies."""
    if not left or not right:
        return 0.0
    first, second = set(left), set(right)
    union = sorted(first | second)[: max(len(left), len(right))]
    if not union:
        return 0.0
    return sum(1 for item in union if item in first and item in second) / len(union)


def basis(body: str, at: datetime) -> dict:
    return {"digest": body_digest(body), "minhash": signature(body), "chars": len(body or ""), "at": at.isoformat()}


def _when(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def needs_rewrite(
    body: str,
    previous: dict | None,
    *,
    has_sketch: bool,
    now: datetime,
    below: float = REWRITE_BELOW_SIMILARITY,
    stale_after: timedelta = STALE_AFTER,
) -> tuple[bool, str]:
    """Whether this body deserves a new sketch, and the reason in words."""
    if not has_sketch:
        return True, "还没有速写"
    if not isinstance(previous, dict) or not previous.get("digest"):
        return True, "没有记下速写依据"
    if previous.get("digest") == body_digest(body):
        return False, "正文没有变"
    shown = similarity(signature(body), [int(x) for x in previous.get("minhash") or []])
    if shown < below:
        return True, f"正文改动较大（相似度 {shown:.2f}）"
    written = _when(previous.get("at") or "")
    hours = stale_after.total_seconds() / 3600
    if written is None or now - written >= stale_after:
        return True, f"正文有小改动且速写已超过 {hours:g} 小时"
    return False, f"正文只小改（相似度 {shown:.2f}），{hours:g} 小时内不重写"
