"""此刻有哪些模型调用在飞。

系统里消耗模型的不止萃取队列：向量补齐、词条整合都在跑模型，却不写 ``queue``。
但每一次模型请求都从同一个收口发出。这里只登记那次请求的进出，不写库、不落盘，
读它也不碰数据库。队列状态页据此回答「系统在不在调用模型」，才不会和成本明细各说各话。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

# 一次调用结束后，这段时间内仍算「刚刚还在调用」。
RECENT_SECONDS = 120
UNKNOWN = "模型调用"

_LOCK = threading.Lock()
_OPEN: dict[str, dict] = {}
_ENDED: dict[str, float] = {}


@contextmanager
def track(worker: str):
    """登记一次模型调用。任何出口都清掉，包括抛错。"""

    name = (worker or "").strip() or UNKNOWN
    with _LOCK:
        item = _OPEN.get(name)
        if item is None:
            _OPEN[name] = {"count": 1, "since": time.monotonic()}
        else:
            item["count"] += 1
    try:
        yield
    finally:
        with _LOCK:
            item = _OPEN.get(name)
            if item is not None:
                item["count"] -= 1
                if item["count"] <= 0:
                    _OPEN.pop(name, None)
            _ENDED[name] = time.monotonic()


def snapshot(now: float | None = None, *, recent_seconds: float = RECENT_SECONDS) -> dict:
    """此刻在飞的调用，以及刚刚结束的调用。"""

    mark = time.monotonic() if now is None else now
    with _LOCK:
        opening = [
            {"worker": name, "seconds": max(0, int(mark - item["since"]))}
            for name, item in _OPEN.items()
            if item["count"] > 0
        ]
        recent = []
        for name, ended in _ENDED.items():
            if name in _OPEN:
                continue
            ago = max(0, int(mark - ended))
            if ago <= recent_seconds:
                recent.append({"worker": name, "ago": ago})
    opening.sort(key=lambda item: (-item["seconds"], item["worker"]))
    recent.sort(key=lambda item: (item["ago"], item["worker"]))
    return {"open": opening, "recent": recent, "recent_seconds": int(recent_seconds)}


def reset() -> None:
    """清空登记。给测试用。"""

    with _LOCK:
        _OPEN.clear()
        _ENDED.clear()
