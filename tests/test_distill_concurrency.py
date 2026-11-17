"""Concurrent distill lease and pump. No live LLM."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from rogmem import engine


def _open(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.delenv("ROGMEM_LLM_BASE", raising=False)
    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _queue_user(sid: str, body: str = "这段句子只用来排队，确认并发租约。") -> None:
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?, 'user', 'qa', 'qa', ?, ?, ?, ?, 'fast', 1)""",
        (sid, engine.utcnow(), sid + "-h", body, engine.ST_VEC_READY),
    )
    con.execute("INSERT INTO queue(source_id,lane,state) VALUES(?, 'fast', 'queued')", (sid,))
    con.commit()


def test_distill_concurrency_default_is_three(monkeypatch):
    monkeypatch.delenv("ROGMEM_DISTILL_CONCURRENCY", raising=False)
    assert engine.distill_concurrency() == 3


def test_distill_concurrency_one_falls_back_to_serial(monkeypatch):
    monkeypatch.setenv("ROGMEM_DISTILL_CONCURRENCY", "1")
    assert engine.distill_concurrency() == 1


def test_distill_concurrency_invalid_uses_default(monkeypatch):
    monkeypatch.setenv("ROGMEM_DISTILL_CONCURRENCY", "nope")
    assert engine.distill_concurrency() == 3
    monkeypatch.setenv("ROGMEM_DISTILL_CONCURRENCY", "0")
    assert engine.distill_concurrency() == 1


def test_distill_concurrency_prefers_the_setting(monkeypatch):
    monkeypatch.setenv("ROGMEM_DISTILL_CONCURRENCY", "3")
    monkeypatch.setattr(engine, "get_setting", lambda *_a, **_k: "5")
    assert engine.distill_concurrency() == 5


def test_the_liveness_answer_never_touches_the_database(monkeypatch):
    """/healthz promises no database, so its number must come from the cached snapshot."""
    monkeypatch.setenv("ROGMEM_DISTILL_CONCURRENCY", "3")
    monkeypatch.setattr(engine, "_LIVE_CONCURRENCY", 7)
    called = []
    monkeypatch.setattr(engine, "get_setting", lambda *_a, **_k: called.append(1) or "9")
    snapshot = engine.health_snapshot()
    assert snapshot["distill_concurrency"] == 7
    assert called == []
    assert engine.distill_concurrency() == 9


def test_saving_the_pipeline_refreshes_the_liveness_number(tmp_path, monkeypatch):
    """The console changes the concurrency; /healthz must stop reporting the old one."""
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_DISTILL_CONCURRENCY", "3")
    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    assert engine.health_snapshot()["distill_concurrency"] == 3
    saved = engine.save_pipeline(
        {
            "scan_interval": "60",
            "probe_interval": "60",
            "reconcile_hours": "3",
            "sweep_at": "03:00",
            "distill_concurrency": "6",
        }
    )
    assert saved["ok"] is True
    assert engine.distill_concurrency() == 6
    assert engine.health_snapshot()["distill_concurrency"] == 6


def test_health_reports_concurrency(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DISTILL_CONCURRENCY", "3")
    _open(tmp_path, monkeypatch)
    health = engine.health()
    assert health["distill_concurrency"] == 3
    assert health["distill_leased"] == 0


def test_parallel_lease_does_not_share_a_row(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    for i in range(3):
        _queue_user(f"src-{i}")
    got: list[str] = []
    lock = threading.Lock()

    def take() -> None:
        leased = engine.lease_next()
        assert leased is not None
        with lock:
            got.append(leased["source_id"])

    threads = [threading.Thread(target=take) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(got) == ["src-0", "src-1", "src-2"]
    leased = engine.db().execute("SELECT COUNT(*) AS n FROM queue WHERE state='leased'").fetchone()["n"]
    assert leased == 3
    assert engine.lease_next() is None


def test_parallel_pump_advances_three_units(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    for i in range(3):
        _queue_user(f"pump-{i}")
    inflight = 0
    peak = 0
    gate = threading.Barrier(3)
    lock = threading.Lock()

    def fake_distill(source, qid):
        del qid
        nonlocal inflight, peak
        gate.wait(timeout=2)
        with lock:
            inflight += 1
            peak = max(peak, inflight)
        time.sleep(0.25)
        with engine.LOCK:
            engine.db().execute(
                """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,event_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    "st-" + source["id"],
                    "owner",
                    "概述",
                    "并发推进 " + source["id"],
                    "这段句子只用来排队，确认并发租约。",
                    source["id"],
                    "written",
                    "user",
                    engine.utcnow(),
                    engine.utcnow(),
                ),
            )
            engine.db().commit()
        with lock:
            inflight -= 1
        return True

    monkeypatch.setattr(engine, "_distill", fake_distill)
    done: list[str | None] = []
    done_lock = threading.Lock()

    def run() -> None:
        result = engine.pump_one()
        with done_lock:
            done.append(result)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 3
    assert sorted(done) == ["pump-0", "pump-1", "pump-2"]
    rows = engine.db().execute("SELECT source_id, state FROM queue ORDER BY source_id").fetchall()
    assert [row["state"] for row in rows] == ["done", "done", "done"]
    wrote = engine.db().execute("SELECT COUNT(*) AS n FROM statements WHERE id LIKE 'st-pump-%'").fetchone()["n"]
    assert wrote == 3


def test_failed_unit_retries_like_serial(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    _queue_user("fail-1")
    _queue_user("ok-1")

    def fake_distill(source, qid):
        del qid
        if source["id"] == "fail-1":
            raise RuntimeError("stub distill failed")
        return True

    monkeypatch.setattr(engine, "_distill", fake_distill)
    first = engine.pump_one()
    second = engine.pump_one()
    assert {first, second} == {"fail-1", "ok-1"}
    failed = engine.db().execute("SELECT state, attempts FROM queue WHERE source_id='fail-1'").fetchone()
    assert failed["state"] == "queued"
    assert failed["attempts"] == 1
    ok = engine.db().execute("SELECT state FROM queue WHERE source_id='ok-1'").fetchone()
    assert ok["state"] == "done"


def test_start_distill_workers_uses_configured_count(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DISTILL_CONCURRENCY", "3")
    monkeypatch.setenv("ROGMEM_POLL", "30")
    _open(tmp_path, monkeypatch)
    from rogmem import app

    stop = threading.Event()
    threads = app.start_distill_workers(stop=stop)
    try:
        assert len(threads) == 3
        assert all(thread.is_alive() for thread in threads)
        assert {thread.name for thread in threads} == {
            "rogmem-distill-0",
            "rogmem-distill-1",
            "rogmem-distill-2",
        }
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)


def test_restart_releases_orphaned_leases(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    _queue_user("orphan")
    leased = engine.lease_next()
    assert leased is not None
    assert engine.db().execute("SELECT state FROM queue WHERE source_id='orphan'").fetchone()["state"] == "leased"
    engine.release_orphaned_leases()
    row = engine.db().execute("SELECT state, leased_until FROM queue WHERE source_id='orphan'").fetchone()
    assert row["state"] == "queued"
    assert row["leased_until"] is None
