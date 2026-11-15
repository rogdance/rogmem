"""一组对话一次蒸馏，进行中的调用不被另一条工人重开。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rogmem.exchange import Side, exchange_ready, form_exchanges, join_exchange_text


def _side(
    source_id: str,
    kind: str,
    *,
    session_id: str = "s",
    turn_id: str = "",
    event_at: str = "",
    ingested_at: str = "",
    rowid: int = 0,
    pair_ready: int = 0,
    wait_until: str = "",
    qid: int = 0,
    lane: str = "p0",
    attempts: int = 0,
) -> Side:
    return Side(
        source_id=source_id,
        session_id=session_id,
        turn_id=turn_id or f"{session_id}:{source_id}",
        kind=kind,
        event_at=event_at,
        ingested_at=ingested_at or event_at,
        rowid=rowid,
        pair_ready=pair_ready,
        wait_until=wait_until,
        qid=qid or rowid,
        lane=lane,
        attempts=attempts,
    )


def _ids(groups) -> list[list[str]]:
    return [[side.source_id for side in group.sides] for group in groups]


def test_alternating_rounds_stay_separate_exchanges():
    groups = form_exchanges(
        [
            _side("u1", "user", event_at="2026-01-01T00:00:00+00:00", rowid=1),
            _side("a1", "agent", event_at="2026-01-01T00:00:01+00:00", rowid=2),
            _side("u2", "user", event_at="2026-01-01T00:00:02+00:00", rowid=3),
            _side("a2", "agent", event_at="2026-01-01T00:00:03+00:00", rowid=4),
        ]
    )
    assert _ids(groups) == [["u1", "a1"], ["u2", "a2"]]


def test_a_burst_before_the_reply_is_one_exchange():
    groups = form_exchanges(
        [
            _side("u1", "user", event_at="2026-01-01T00:00:00+00:00", rowid=1),
            _side("u2", "user", event_at="2026-01-01T00:00:01+00:00", rowid=2),
            _side("a1", "agent", event_at="2026-01-01T00:00:02+00:00", rowid=3),
        ]
    )
    assert _ids(groups) == [["u1", "u2", "a1"]]


def test_explicit_turn_id_is_not_split_by_a_nearer_line():
    groups = form_exchanges(
        [
            _side("u-other", "user", turn_id="other", event_at="2026-01-01T00:00:00+00:00", rowid=1),
            _side("u-t", "user", turn_id="t", event_at="2026-01-01T00:00:01+00:00", rowid=2),
            _side("a-t", "agent", turn_id="t", event_at="2026-01-01T00:00:02+00:00", rowid=3),
        ]
    )
    found = {tuple(ids) for ids in _ids(groups)}
    assert ("u-t", "a-t") in found or ("a-t", "u-t") in found
    assert ["u-other"] in _ids(groups)


def test_different_sessions_do_not_join():
    groups = form_exchanges(
        [
            _side("u", "user", session_id="s1", event_at="2026-01-01T00:00:00+00:00", rowid=1),
            _side("a", "agent", session_id="s2", event_at="2026-01-01T00:00:01+00:00", rowid=2),
        ]
    )
    assert _ids(groups) == [["u"], ["a"]]


def test_blank_session_stays_its_own_unit():
    groups = form_exchanges(
        [
            _side("u", "user", session_id="", event_at="2026-01-01T00:00:00+00:00", rowid=1),
            _side("a", "agent", session_id="", event_at="2026-01-01T00:00:01+00:00", rowid=2),
        ]
    )
    assert _ids(groups) == [["u"], ["a"]]


def test_a_pair_is_ready_before_either_deadline():
    later = "2099-01-01T00:00:00+00:00"
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    groups = form_exchanges(
        [
            _side("u", "user", event_at="2026-01-01T00:00:00+00:00", rowid=1, wait_until=later),
            _side("a", "agent", event_at="2026-01-01T00:00:01+00:00", rowid=2, wait_until=later),
        ]
    )
    assert len(groups) == 1
    assert exchange_ready(groups[0], now) is True


def test_a_lone_side_waits_out_its_deadline():
    now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    waiting = form_exchanges(
        [
            _side(
                "u",
                "user",
                event_at="2026-01-01T00:00:00+00:00",
                rowid=1,
                wait_until=(now + timedelta(seconds=30)).isoformat(),
            )
        ]
    )
    assert exchange_ready(waiting[0], now) is False
    due = form_exchanges(
        [
            _side(
                "u",
                "user",
                event_at="2026-01-01T00:00:00+00:00",
                rowid=1,
                wait_until=(now - timedelta(seconds=1)).isoformat(),
            )
        ]
    )
    assert exchange_ready(due[0], now) is True


def test_join_keeps_one_side_unlabeled_and_both_sides_readable():
    alone = join_exchange_text([{"kind": "user", "body": "合成句：只有一边。", "event_at": "t", "rowid": 1}])
    assert alone == "合成句：只有一边。"
    both = join_exchange_text(
        [
            {"kind": "agent", "body": "合成句：助手后说。", "event_at": "2026-01-01T00:00:01+00:00", "rowid": 2},
            {"kind": "user", "body": "合成句：用户先说。", "event_at": "2026-01-01T00:00:00+00:00", "rowid": 1},
        ]
    )
    assert both.index("用户先说") < both.index("助手后说")
    assert "合成句：用户先说。" in both and "合成句：助手后说。" in both


def _open(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_TURN_WAIT", "600")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _agent(engine):
    engine.db().execute(
        """INSERT INTO agents(id,device_id,device_name,program,display_name,status,pubkey,created_at)
           VALUES('ag','dev','机','Cursor','Cursor','启用','k',?)""",
        (engine.utcnow(),),
    )
    engine.db().commit()
    return engine.db().execute("SELECT * FROM agents WHERE id='ag'").fetchone()


def test_history_round_is_one_distill(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    imported = engine.ingest_history(
        [
            {
                "role": "user",
                "text": "合成句：用户把这一轮交给蒸馏。",
                "event_at": "2026-01-01T00:00:00+00:00",
                "session_id": "sess",
                "turn_id": "sess:user-only",
                "seq": 1,
            },
            {
                "role": "assistant",
                "text": "合成句：助手把同一轮交给蒸馏。",
                "event_at": "2026-01-01T00:00:01+00:00",
                "session_id": "sess",
                "turn_id": "sess:agent-only",
                "seq": 2,
            },
        ]
    )
    assert imported["ok"] is True
    calls: list[str] = []

    def fake(source, qid):
        del qid
        calls.append(engine._material_for_distill(source))
        return True

    monkeypatch.setattr(engine, "_distill", fake)
    first = engine.pump_one()
    second = engine.pump_one()
    assert first
    assert second is None
    assert len(calls) == 1
    assert "用户把这一轮" in calls[0] and "助手把同一轮" in calls[0]
    states = [row["state"] for row in engine.db().execute("SELECT state FROM queue ORDER BY id")]
    assert states == ["done", "done"]


def test_a_lone_side_leaves_once_its_deadline_passes(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.ingest_history(
        [
            {
                "role": "user",
                "text": "合成句：等到点了再单独送。",
                "event_at": "2026-01-01T00:00:00+00:00",
                "session_id": "late",
                "turn_id": "late:1",
                "seq": 1,
            }
        ]
    )
    engine.db().execute(
        "UPDATE sources SET wait_until=? WHERE session_id='late'",
        ("2000-01-01T00:00:00+00:00",),
    )
    engine.db().commit()
    leased = engine.lease_next()
    assert leased is not None
    assert leased["source_ids"] == [leased["source_id"]]


def test_history_without_a_mate_is_not_leased_immediately(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.ingest_history(
        [
            {
                "role": "user",
                "text": "合成句：这一边先到，先别送去蒸馏。",
                "event_at": "2026-01-01T00:00:00+00:00",
                "session_id": "alone",
                "turn_id": "alone:1",
                "seq": 1,
            }
        ]
    )
    row = engine.db().execute("SELECT pair_ready, wait_until FROM sources WHERE session_id='alone'").fetchone()
    assert row["pair_ready"] == 0
    assert row["wait_until"]
    assert engine.lease_next() is None


def test_live_sides_with_different_turn_ids_distill_together(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    agent = _agent(engine)
    user = engine.submit_text(
        kind="user",
        text="合成句：现场用户这一句。",
        session_id="live",
        turn_id="live:hashed-user",
        seq=11,
        agent=agent,
        event_at="2026-11-13T00:00:00+00:00",
        project=None,
    )
    assert engine.lease_next() is None
    agent_row = engine.submit_text(
        kind="agent",
        text="合成句：现场助手接着这一句。",
        session_id="live",
        turn_id="live:hashed-agent",
        seq=12,
        agent=agent,
        event_at="2026-11-13T00:00:01+00:00",
        project=None,
    )
    assert user["ok"] and agent_row["ok"]
    calls: list[str] = []

    def fake(source, qid):
        del qid
        calls.append(engine._material_for_distill(source))
        return True

    monkeypatch.setattr(engine, "_distill", fake)
    done = engine.pump_one()
    assert done
    assert engine.pump_one() is None
    assert len(calls) == 1
    assert "现场用户这一句" in calls[0] and "现场助手接着这一句" in calls[0]


def test_a_waiting_round_does_not_block_another_unit(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    agent = _agent(engine)
    engine.submit_text(
        kind="user",
        text="合成句：还在等另一边。",
        session_id="wait",
        turn_id="wait:1",
        seq=1,
        agent=agent,
        event_at="2026-11-13T00:00:00+00:00",
        project=None,
    )
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,filename)
           VALUES('file-1','file',?,'h','合成句：文件可以先走。',?,'p2',1,'note.txt')""",
        (engine.utcnow(), engine.ST_VEC_READY),
    )
    engine.db().execute("INSERT INTO queue(source_id,lane,state) VALUES('file-1','p2','queued')")
    engine.db().commit()
    leased = engine.lease_next()
    assert leased is not None
    assert leased["source_id"] == "file-1"


def test_a_running_distill_is_not_opened_again(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES('solo','user',?,'h','合成句：这一条正在蒸馏。',?,'p0',1)""",
        (engine.utcnow(), engine.ST_VEC_READY),
    )
    engine.db().execute("INSERT INTO queue(source_id,lane,state) VALUES('solo','p0','queued')")
    engine.db().commit()
    started: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    def fake(source, qid):
        del qid
        started.append(source["id"])
        with engine.LOCK:
            engine.db().execute(
                "UPDATE queue SET leased_until=? WHERE source_id=?",
                ("2000-01-01T00:00:00+00:00", source["id"]),
            )
            engine.db().commit()
        entered.set()
        assert release.wait(timeout=2)
        return True

    monkeypatch.setattr(engine, "_distill", fake)
    worker = threading.Thread(target=engine.pump_one)
    worker.start()
    assert entered.wait(timeout=2)
    stolen = engine.lease_next()
    release.set()
    worker.join(timeout=2)
    assert stolen is None
    assert started == ["solo"]
    assert engine.db().execute("SELECT state FROM queue WHERE source_id='solo'").fetchone()["state"] == "done"


def test_two_exchanges_lease_without_sharing_a_row(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.ingest_history(
        [
            {
                "role": "user",
                "text": "合成句：第一轮用户。",
                "event_at": "2026-01-01T00:00:00+00:00",
                "session_id": "s",
                "turn_id": "s:u1",
                "seq": 1,
            },
            {
                "role": "assistant",
                "text": "合成句：第一轮助手。",
                "event_at": "2026-01-01T00:00:01+00:00",
                "session_id": "s",
                "turn_id": "s:a1",
                "seq": 2,
            },
            {
                "role": "user",
                "text": "合成句：第二轮用户。",
                "event_at": "2026-01-01T00:00:02+00:00",
                "session_id": "other",
                "turn_id": "other:u2",
                "seq": 3,
            },
            {
                "role": "assistant",
                "text": "合成句：第二轮助手。",
                "event_at": "2026-01-01T00:00:03+00:00",
                "session_id": "other",
                "turn_id": "other:a2",
                "seq": 4,
            },
        ]
    )
    barrier = threading.Barrier(2)
    got: list[set[str]] = []
    lock = threading.Lock()

    def take():
        leased = engine.lease_next()
        assert leased is not None
        barrier.wait(timeout=2)
        with lock:
            got.append(set(leased["source_ids"]))

    threads = [threading.Thread(target=take) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert len(got) == 2
    assert got[0].isdisjoint(got[1])
    assert len(got[0]) == 2 and len(got[1]) == 2
    assert len(got[0] | got[1]) == 4
