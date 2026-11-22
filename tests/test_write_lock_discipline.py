"""一个写事务不许活过它的锁，读永远不因为写锁失败。

真机事故（2026-11-22）：``CrystalStore(con)`` 每次构造都跑一条
``UPDATE crystallizations SET type='工程' WHERE type='项目'``。哪怕一行都没改，Python
sqlite3 也会在它前面隐式 BEGIN，这条连接就拿走了 WAL 唯一的写锁。读路径上构造它的线程
从不提交，写锁就留在线程池里一条闲着的连接上——整库 30 多分钟没有一次写入落地，检索的
调用计数、文件导入、管线全部报 ``database is locked``。

这里锁死几条规则：
- 无变化的迁移/种子不开写事务；
- ``engine.LOCK`` 最外层释放时，本线程遗留的写事务当场收口（正常提交、异常回滚）；
- 不持锁遗留的写事务，看门狗过了阈值替它提交，把写锁还回去；
- 读调用的计数不阻塞、不报错，写不进去就先记在内存里；
- 文件导入写不进去时回「数据库正忙」，扫描目录里不留没有行的文件。
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time

import pytest

from rogmem import engine
from rogmem.crystal_store import CrystalStore


def _tg():
    """The guard module engine is wired to right now (another test may have reloaded modules)."""
    return engine.txguard


@pytest.fixture()
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    engine.db().commit()
    # 别的测试可能 reload 过 engine：新 LOCK 会把守卫指向它，这里钉回本文件用的那把锁。
    monkeypatch.setattr(_tg(), "_LOCK_REF", engine.LOCK)
    _tg().reset()
    return tmp_path


def _other_writer(path) -> sqlite3.Connection:
    """A connection that is not this process's: it only waits busy_timeout, never LOCK."""
    con = sqlite3.connect(path / "rogmem.sqlite", timeout=0.2, check_same_thread=False)
    return con


def _can_write(path) -> bool:
    con = _other_writer(path)
    try:
        con.execute("INSERT INTO settings(k, v) VALUES('probe', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (secrets.token_hex(4),))
        con.commit()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


def _in_thread(fn):
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # pragma: no cover - surfaced below
            box["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(10)
    if "error" in box:
        raise box["error"]
    return box.get("value")


def test_constructing_a_crystal_store_does_not_take_the_write_lock(hub):
    def build():
        con = engine.db()
        CrystalStore(con)
        return con.in_transaction

    assert _in_thread(build) is False
    assert _can_write(hub)


def test_seeding_standing_sketches_twice_does_not_open_a_write(hub):
    from rogmem.standing_sketches import ensure_registry

    con = engine.db()
    ensure_registry(con)
    con.commit()
    ensure_registry(con)
    assert con.in_transaction is False


def test_a_write_left_open_inside_lock_is_committed_when_the_lock_is_released(hub):
    def leak():
        with engine.LOCK:
            engine.db().execute("INSERT INTO settings(k, v) VALUES('leaked', '1')")
        return engine.db().in_transaction

    assert _in_thread(leak) is False
    assert _can_write(hub)
    row = engine.db().execute("SELECT v FROM settings WHERE k='leaked'").fetchone()
    assert row is not None and row["v"] == "1"
    assert _tg().snapshot()["stats"]["leak_closed"] >= 1


def test_a_write_left_open_by_an_exception_is_rolled_back_at_release(hub):
    def boom():
        with engine.LOCK:
            engine.db().execute("INSERT INTO settings(k, v) VALUES('half', '1')")
            raise RuntimeError("stop")

    with pytest.raises(RuntimeError):
        _in_thread(boom)
    assert _can_write(hub)
    assert engine.db().execute("SELECT v FROM settings WHERE k='half'").fetchone() is None


def test_nested_lock_does_not_close_the_outer_write(hub):
    def nested():
        with engine.LOCK:
            con = engine.db()
            con.execute("INSERT INTO settings(k, v) VALUES('outer', '1')")
            with engine.LOCK:
                pass
            still = con.in_transaction
            con.rollback()
            return still

    assert _in_thread(nested) is True


def test_the_watchdog_returns_a_write_lock_left_open_outside_lock(hub, monkeypatch):
    monkeypatch.setattr(_tg(), "HOLD_WARN_S", 0.0)
    monkeypatch.setattr(_tg(), "HEAL_AFTER_S", 0.05)
    ready = threading.Event()
    done = threading.Event()

    def idle_pool_thread():
        engine.db().execute("INSERT INTO settings(k, v) VALUES('pool', '1')")
        ready.set()
        done.wait(10)

    thread = threading.Thread(target=idle_pool_thread)
    thread.start()
    try:
        assert ready.wait(5)
        assert not _can_write(hub)
        time.sleep(0.1)
        _tg()._watch_once()
        assert _can_write(hub)
        assert _tg().snapshot()["stats"]["healed"] >= 1
        assert engine.db().execute("SELECT v FROM settings WHERE k='pool'").fetchone()["v"] == "1"
    finally:
        done.set()
        thread.join(5)


def test_the_watchdog_leaves_a_write_alone_while_its_thread_holds_lock(hub, monkeypatch):
    monkeypatch.setattr(_tg(), "HOLD_WARN_S", 0.0)
    monkeypatch.setattr(_tg(), "HEAL_AFTER_S", 0.0)
    ready = threading.Event()
    done = threading.Event()

    def writer():
        with engine.LOCK:
            engine.db().execute("INSERT INTO settings(k, v) VALUES('inside', '1')")
            ready.set()
            done.wait(10)
            engine.db().commit()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert ready.wait(5)
        _tg()._watch_once()
        assert _tg().snapshot()["stats"]["healed"] == 0
    finally:
        done.set()
        thread.join(5)


def _add_agent(agent_id: str) -> None:
    with engine.LOCK:
        engine.db().execute(
            """INSERT INTO agents(id, device_id, device_name, program, display_name, status, pubkey, created_at)
               VALUES(?, 'd1', '设备', 'workbuddy', '测试', '启用', '', ?)""",
            (agent_id, engine.utcnow()),
        )
        engine.db().commit()


def _stored_calls(agent_id: str) -> int:
    return int(engine.db().execute("SELECT call_count FROM agents WHERE id=?", (agent_id,)).fetchone()["call_count"])


def test_a_read_count_never_fails_while_another_writer_holds_the_database(hub):
    _add_agent("a1")
    other = _other_writer(hub)
    other.execute("INSERT INTO settings(k, v) VALUES('blocker', '1')")
    try:
        started = time.monotonic()
        _in_thread(lambda: engine.note_memory_call("a1"))
        assert time.monotonic() - started < 3
        assert engine.pending_memory_calls("a1") == 1
    finally:
        other.rollback()
        other.close()
    _in_thread(lambda: engine.note_memory_call("a1"))
    assert engine.pending_memory_calls("a1") == 0
    assert _stored_calls("a1") == 2


def test_a_read_count_lands_at_once_when_the_database_is_free(hub):
    _add_agent("a2")
    engine.note_memory_call("a2")
    assert _stored_calls("a2") == 1
    assert engine.pending_memory_calls("a2") == 0


def test_an_import_that_cannot_write_answers_busy_and_leaves_no_orphan_file(hub):
    body = "一份导入的文件。".encode()
    staged = hub / "scan" / f".part-{secrets.token_hex(8)}"
    staged.write_bytes(body)
    other = _other_writer(hub)
    other.execute("INSERT INTO settings(k, v) VALUES('blocker', '1')")
    try:
        result = _in_thread(lambda: engine.queue_staged("note.txt", staged, hashlib.sha256(body).hexdigest()))
    finally:
        other.rollback()
        other.close()
    assert result["ok"] is False
    assert result["error"] == engine._BUSY_WRITE
    assert not (hub / "scan" / "note.txt").exists()
    assert staged.exists()
    again = _in_thread(lambda: engine.queue_staged("note.txt", staged, hashlib.sha256(body).hexdigest()))
    assert again["ok"] is True and again.get("queued") is True
    assert (hub / "scan" / "note.txt").exists()


def test_a_write_left_open_before_a_model_call_is_committed_first(hub):
    def call():
        con = engine.db()
        con.execute("INSERT INTO settings(k, v) VALUES('slow', '1')")
        _tg().slow_call("chat", con)
        return con.in_transaction

    assert _in_thread(call) is False
    assert _can_write(hub)
    assert _tg().snapshot()["stats"]["slow_in_tx"] >= 1


def test_a_model_call_inside_lock_keeps_the_write_and_reports_it(hub):
    def call():
        with engine.LOCK:
            con = engine.db()
            con.execute("INSERT INTO settings(k, v) VALUES('slow2', '1')")
            _tg().slow_call("chat", con)
            still = con.in_transaction
            con.rollback()
            return still

    assert _in_thread(call) is True
    stats = _tg().snapshot()["stats"]
    assert stats["slow_in_tx"] >= 1 and stats["slow_under_lock"] >= 1


def test_many_readers_and_writers_never_see_database_is_locked(hub):
    """多个智能体同时读、同时写：读不失败，写在锁里排队而不是报错。"""
    _add_agent("reader")
    errors: list[BaseException] = []
    stop = time.monotonic() + 3.0

    def reader():
        try:
            while time.monotonic() < stop:
                engine.db().execute("SELECT COUNT(*) FROM sources").fetchone()
                CrystalStore(engine.db()).get("owner")
                engine.note_memory_call("reader")
                engine.settle_thread_transaction("test")
        except BaseException as exc:
            errors.append(exc)

    def writer(tag: str):
        try:
            n = 0
            while time.monotonic() < stop:
                with engine.LOCK:
                    engine.db().execute(
                        "INSERT INTO settings(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (f"w-{tag}", str(n)),
                    )
                    # 有一半故意不提交：锁的释放替它收口。
                    if n % 2:
                        engine.db().commit()
                n += 1
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(6)]
    threads += [threading.Thread(target=writer, args=(str(i),)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert not errors, errors[:3]
    assert _can_write(hub)
    engine.flush_memory_calls(wait=1.0)
    assert _stored_calls("reader") > 0
