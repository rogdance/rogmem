"""一个写事务不许活过它的锁。

SQLite（WAL）同一时刻只有一个写者。Python sqlite3 在第一条 INSERT/UPDATE/DELETE
前隐式 BEGIN，直到这条连接 commit/rollback 才放掉写锁。中枢每个线程一条连接：
哪个线程写了一行忘了提交、或者把「写了一半」的事务带去调模型/嵌入，写锁就一直在它
手里，别的线程（检索计数、文件导入、管线）等满 busy_timeout 后一律报
``database is locked``。真机上一次这样的泄漏把整库写入停了 30 分钟以上。

这里把「事务的寿命」变成可执行的规则，而不是靠 116 处调用点各自记得：

1. ``WriteLock``：``engine.LOCK`` 的替身。最外层释放时，本线程连接上还开着的写事务
   当场收口——正常退出就提交（调用方迟早也会提交），带着异常退出就回滚——并记下
   这个事务是在哪一行开的。写事务从此活不过锁的作用域。
2. ``opened``/``closed``：连接在事务开、关时登记时刻与开启位置（只在开的那一下取栈）。
   不持锁就开写事务的位置也会登记。
3. ``slow_call``：模型/嵌入调用开始前检查本线程是否正开着写事务或握着锁，有就登记。
4. 看门狗：每几秒巡一遍所有连接。写事务开着超过阈值就喊；超过自愈阈值、且开它的
   线程此刻没有握锁（说明它已经离开了写代码，只是忘了收口），就替它提交，把写锁
   还回去。最坏情况被限制在几十秒，而不是无限期。

所有登记都留在内存里，``snapshot()`` 给运维/控制台看；同一位置只打印一次日志。
``ROGMEM_TX_REPORT=<path>`` 时每条登记追加一行 JSON，供测试全量审计用。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import Callable

HOLD_WARN_S = float(os.environ.get("ROGMEM_TX_HOLD_WARN", "10") or 10)
HEAL_AFTER_S = float(os.environ.get("ROGMEM_TX_HEAL_AFTER", "30") or 30)
WATCH_EVERY_S = float(os.environ.get("ROGMEM_TX_WATCH_EVERY", "5") or 5)
STRICT = os.environ.get("ROGMEM_TX_STRICT", "") == "1"

_CONNS: "weakref.WeakSet[sqlite3.Connection]" = weakref.WeakSet()
_CONNS_GUARD = threading.Lock()
_STATS: dict[str, int] = {
    "leak_closed": 0,
    "unlocked_write": 0,
    "slow_in_tx": 0,
    "slow_under_lock": 0,
    "long_tx": 0,
    "healed": 0,
    "left_open": 0,
}
_RECENT: list[dict] = []
_RECENT_MAX = 60
_SEEN: set[tuple[str, str]] = set()
_NOTE_GUARD = threading.Lock()
_LOCK_REF: "WriteLock | None" = None
_WATCHER: threading.Thread | None = None
_WATCHER_GUARD = threading.Lock()


_PKG = str(Path(__file__).resolve().parent).replace("\\", "/").lower() + "/"


def _ours(filename: str) -> bool:
    path = filename.replace("\\", "/").lower()
    return path.startswith(_PKG) and not path.endswith("/txguard.py") and "/vendor/" not in path


def site(skip: int = 2, depth: int = 7) -> str:
    """Where we are, as a short chain of rogmem frames. Cheap: called once per transaction."""
    frames: list[str] = []
    try:
        frame = sys._getframe(skip)
    except ValueError:
        return ""
    outside = ""
    while frame is not None and len(frames) < depth:
        code = frame.f_code
        if _ours(code.co_filename):
            frames.append(f"{Path(code.co_filename).name}:{frame.f_lineno}:{code.co_name}")
        elif not outside and not code.co_filename.replace("\\", "/").endswith("/txguard.py"):
            outside = f"{Path(code.co_filename).name}:{frame.f_lineno}:{code.co_name}"
        frame = frame.f_back
    return " < ".join(frames) or outside


def note(kind: str, where: str = "", **fields) -> None:
    """Remember one finding. Print a given (kind, where) once; count every one."""
    entry = {"kind": kind, "site": where, "at": time.time(), "thread": threading.current_thread().name}
    entry.update(fields)
    with _NOTE_GUARD:
        _STATS[kind] = _STATS.get(kind, 0) + 1
        _RECENT.append(entry)
        if len(_RECENT) > _RECENT_MAX:
            del _RECENT[: len(_RECENT) - _RECENT_MAX]
        first = (kind, where) not in _SEEN
        _SEEN.add((kind, where))
    report = os.environ.get("ROGMEM_TX_REPORT", "")
    if report:
        try:
            test = os.environ.get("PYTEST_CURRENT_TEST", "")
            with open(report, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({**entry, "test": test}, ensure_ascii=False) + "\n")
        except OSError:
            pass
    if first:
        extra = " ".join(f"{key}={value}" for key, value in fields.items())
        print(f"ROGMEM_TX {kind} site={where or '?'} {extra}".rstrip(), flush=True)


def snapshot() -> dict:
    """Counters, the transactions open right now, and the latest findings."""
    now = time.monotonic()
    open_now = []
    with _CONNS_GUARD:
        conns = list(_CONNS)
    for con in conns:
        since = getattr(con, "_tx_since", None)
        if since is None or not _in_tx(con):
            continue
        open_now.append(
            {"seconds": round(now - since, 1), "site": getattr(con, "_tx_site", ""), "thread": getattr(con, "_tx_thread_name", "")}
        )
    open_now.sort(key=lambda item: -item["seconds"])
    with _NOTE_GUARD:
        recent = list(_RECENT[-20:])
        stats = dict(_STATS)
    return {"stats": stats, "open": open_now[:10], "recent": recent}


def reset() -> None:
    with _NOTE_GUARD:
        for key in list(_STATS):
            _STATS[key] = 0
        _RECENT.clear()
        _SEEN.clear()


def _in_tx(con) -> bool:
    try:
        return bool(con.in_transaction)
    except Exception:
        return False


def register(con: sqlite3.Connection) -> None:
    con._tx_since = None  # type: ignore[attr-defined]
    con._tx_site = ""  # type: ignore[attr-defined]
    con._tx_thread = 0  # type: ignore[attr-defined]
    con._tx_thread_name = ""  # type: ignore[attr-defined]
    with _CONNS_GUARD:
        _CONNS.add(con)
    _ensure_watcher()


def opened(con: sqlite3.Connection) -> None:
    """The connection just entered a write transaction."""
    where = site(3)
    con._tx_since = time.monotonic()  # type: ignore[attr-defined]
    con._tx_site = where  # type: ignore[attr-defined]
    con._tx_thread = threading.get_ident()  # type: ignore[attr-defined]
    con._tx_thread_name = threading.current_thread().name  # type: ignore[attr-defined]
    lock = _LOCK_REF
    if lock is not None and not lock.owned():
        note("unlocked_write", where)


def closed(con: sqlite3.Connection) -> None:
    if not _in_tx(con):
        con._tx_since = None  # type: ignore[attr-defined]


def slow_call(kind: str, con: sqlite3.Connection | None) -> None:
    """A model or embedding call is about to wait on the network or the CPU.

    A write still open here would hold SQLite's only write lock for the whole call
    (a streamed chat may stay silent for many minutes). Outside ``LOCK`` nothing
    atomic is in progress, so the write is committed now, before the wait. Inside
    ``LOCK`` the caller is mid-write; that is only reported.
    """
    lock = _LOCK_REF
    under_lock = lock is not None and lock.owned()
    if con is not None and _in_tx(con):
        where = getattr(con, "_tx_site", "")
        if STRICT:
            raise AssertionError(f"{kind} called inside an open write transaction: {where}")
        action = "kept"
        if not under_lock:
            try:
                con.commit()
                action = "commit"
            except sqlite3.Error as err:
                action = f"commit-failed:{type(err).__name__}"
        note("slow_in_tx", where, call=kind, at_site=site(3), action=action)
    if under_lock:
        note("slow_under_lock", site(3), call=kind)


def settle(con: sqlite3.Connection | None, boundary: str, *, failed: bool = False) -> None:
    """A request or tool call is over. Nothing it started may keep the write lock."""
    if con is None or not _in_tx(con):
        return
    lock = _LOCK_REF
    if lock is not None and lock.owned():
        return
    where = getattr(con, "_tx_site", "")
    action = "rollback" if failed else "commit"
    try:
        if failed:
            con.rollback()
        else:
            con.commit()
    except sqlite3.Error as err:
        action = f"{action}-failed:{type(err).__name__}"
        try:
            con.rollback()
        except sqlite3.Error:
            pass
    note("left_open", where, boundary=boundary, action=action)


class WriteLock:
    """Re-entrant lock whose outermost release closes this thread's write transaction."""

    def __init__(self, current_connection: Callable[[], sqlite3.Connection | None]):
        global _LOCK_REF
        self._lock = threading.RLock()
        self._depth = threading.local()
        self._current = current_connection
        self.owner = 0
        self.since = 0.0
        _LOCK_REF = self

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if not self._lock.acquire(blocking, timeout):
            return False
        depth = getattr(self._depth, "n", 0)
        if depth == 0:
            self.owner = threading.get_ident()
            self.since = time.monotonic()
        self._depth.n = depth + 1
        return True

    def release(self, exc: BaseException | None = None) -> None:
        depth = getattr(self._depth, "n", 0) - 1
        try:
            if depth == 0:
                self._close_leak(exc)
        finally:
            self._depth.n = max(depth, 0)
            if depth <= 0:
                self.owner = 0
            self._lock.release()

    def __enter__(self) -> "WriteLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release(exc)
        return False

    def owned(self) -> bool:
        return getattr(self._depth, "n", 0) > 0

    def _close_leak(self, exc: BaseException | None) -> None:
        try:
            con = self._current()
        except Exception:
            return
        if con is None or not _in_tx(con):
            return
        where = getattr(con, "_tx_site", "")
        action = "rollback" if exc is not None else "commit"
        try:
            if exc is not None:
                con.rollback()
            else:
                con.commit()
        except sqlite3.Error as err:
            action = f"{action}-failed:{type(err).__name__}"
            try:
                con.rollback()
            except sqlite3.Error:
                pass
        note("leak_closed", where, action=action, released=site(3))


def _watch_once() -> None:
    now = time.monotonic()
    with _CONNS_GUARD:
        conns = list(_CONNS)
    lock = _LOCK_REF
    for con in conns:
        since = getattr(con, "_tx_since", None)
        if since is None or not _in_tx(con):
            continue
        age = now - since
        if age < HOLD_WARN_S:
            continue
        where = getattr(con, "_tx_site", "")
        if not getattr(con, "_tx_warned", False):
            con._tx_warned = True  # type: ignore[attr-defined]
            note("long_tx", where, seconds=round(age, 1), thread=getattr(con, "_tx_thread_name", ""))
        if age < HEAL_AFTER_S or lock is None:
            continue
        owner_thread = getattr(con, "_tx_thread", 0)
        if lock.owner and lock.owner == owner_thread:
            # Still inside its own write block. Not ours to end.
            continue
        if not lock.acquire(timeout=2.0):
            continue
        try:
            if getattr(con, "_tx_since", None) != since or not _in_tx(con):
                continue
            try:
                con.commit()
                con._tx_since = None  # type: ignore[attr-defined]
                note("healed", where, seconds=round(age, 1), action="commit")
            except sqlite3.Error as err:
                note("healed", where, seconds=round(age, 1), action=f"failed:{type(err).__name__}")
        finally:
            # The watcher never opens a connection of its own, so this release has nothing to settle.
            lock.release()
    for con in conns:
        if getattr(con, "_tx_warned", False) and getattr(con, "_tx_since", None) is None:
            con._tx_warned = False  # type: ignore[attr-defined]


def _watch_forever() -> None:
    while True:
        time.sleep(WATCH_EVERY_S)
        try:
            _watch_once()
        except Exception as exc:  # the watcher must never die
            print(f"ROGMEM_TX watcher_error {type(exc).__name__}: {exc}", flush=True)


def _ensure_watcher() -> None:
    global _WATCHER
    if _WATCHER is not None and _WATCHER.is_alive():
        return
    with _WATCHER_GUARD:
        if _WATCHER is not None and _WATCHER.is_alive():
            return
        _WATCHER = threading.Thread(target=_watch_forever, name="rogmem-tx-watch", daemon=True)
        _WATCHER.start()
