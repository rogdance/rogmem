"""In-process sliding-window limits for enroll and admin writes."""

from __future__ import annotations

import os
import threading
import time
from collections import deque

_LOCK = threading.Lock()
_HITS: dict[str, deque[float]] = {}
_DEFAULTS = {"enroll": (10, 60), "admin": (60, 60)}
_ENV = {"enroll": "ROGMEM_RATE_ENROLL", "admin": "ROGMEM_RATE_ADMIN"}


def reset() -> None:
    with _LOCK:
        _HITS.clear()


def default_quota(kind: str) -> tuple[int, int]:
    return _DEFAULTS[kind]


def configured_quota(kind: str) -> tuple[int, int]:
    default_limit, default_window = default_quota(kind)
    raw = os.environ.get(_ENV[kind], "").strip()
    if "/" not in raw:
        return default_limit, default_window
    limit_text, window_text = raw.split("/", 1)
    if not limit_text.isdigit() or not window_text.isdigit():
        return default_limit, default_window
    limit, window = int(limit_text), int(window_text)
    if limit < 1 or window < 1 or limit > 100_000 or window > 86_400:
        return default_limit, default_window
    return limit, window


def allow(bucket: str, limit: int, window_s: float, *, now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    with _LOCK:
        hits = _HITS.setdefault(bucket, deque())
        while hits and current - hits[0] >= window_s:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(current)
        return True
