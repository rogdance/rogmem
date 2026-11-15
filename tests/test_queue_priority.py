"""B-QM / B-PR: P0–P3 mapping, 8:1 interleave, 30min aging never to P0."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rogmem.crystal_store import DISTILLER_STORE_WRITE_ENABLED
from rogmem.distill_gate import evaluate_integrate
from rogmem.queue_priority import (
    AGE_MINUTES,
    HIGH_RATIO,
    P0,
    P1,
    P2,
    P3,
    QueueCandidate,
    assign_priority,
    choose_next,
    effective_priority,
    lane_for,
    normalize_lane,
    waited_since,
)


def _open(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _enqueue(engine, sid: str, lane: str, kind: str = "file", ingested_at: str | None = None) -> None:
    now = ingested_at or engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?,?, 'qa','qa',?,?,?,?,?,1)""",
        (sid, kind, now, sid + "-h", "排队用的句子，确认优先级。", engine.ST_VEC_READY, lane),
    )
    engine.db().execute("INSERT INTO queue(source_id,lane,state) VALUES(?,?, 'queued')", (sid, lane))
    engine.db().commit()


def test_b_qm_maps_origins_onto_p0_p3():
    """B-QM: live / history / scan / batch(+slow kin) → P0–P3."""
    assert assign_priority(kind="user", origin="live") == P0
    assert assign_priority(kind="agent", origin="live") == P0
    assert assign_priority(kind="user", origin="history") == P1
    assert assign_priority(kind="file", origin="scan") == P2
    assert assign_priority(kind="file", origin="batch") == P3
    assert assign_priority(kind="summary", origin="live") == P3
    assert assign_priority(kind="file", origin="rebench") == P3
    assert lane_for(kind="user", origin="live") == "p0"
    assert lane_for(kind="user", origin="history") == "p1"
    assert lane_for(kind="file", origin="scan") == "p2"
    assert lane_for(kind="file", origin="batch") == "p3"
    assert normalize_lane("fast") == P0
    assert normalize_lane("slow", "user") == P1
    assert normalize_lane("slow", "file") == P2
    assert normalize_lane("slow", "summary") == P3


def test_a_later_turn_fills_an_empty_project_on_the_same_session(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO agents(id,device_id,device_name,program,display_name,status,pubkey,created_at)
           VALUES('ag','dev','机','Cursor','Cursor','启用','k',?)""",
        (now,),
    )
    engine.db().commit()
    agent = engine.db().execute("SELECT * FROM agents WHERE id='ag'").fetchone()
    first = engine.submit_text(
        kind="user",
        text="合成句：先到的一句还没有仓库。",
        session_id="s-same",
        turn_id="t-1",
        seq=1,
        agent=agent,
        event_at=now,
        project=None,
    )
    engine.submit_text(
        kind="user",
        text="合成句：后到的一句带上仓库。",
        session_id="s-same",
        turn_id="t-2",
        seq=2,
        agent=agent,
        event_at=now,
        project="https://github.com/yunlan/rogmem",
    )
    refs = {
        row["id"]: row["project_ref"]
        for row in engine.db().execute("SELECT id, project_ref FROM sources WHERE session_id='s-same'")
    }
    assert refs[first["id"]] == "https://github.com/yunlan/rogmem"
    again = engine.submit_text(
        kind="user",
        text="合成句：后到的一句带上仓库。",
        session_id="s-same",
        turn_id="t-2",
        seq=2,
        agent=agent,
        event_at=now,
        project="https://github.com/other/repo",
    )
    assert again["duplicate"] is True
    kept = engine.db().execute("SELECT project_ref FROM sources WHERE id=?", (first["id"],)).fetchone()
    assert kept["project_ref"] == "https://github.com/yunlan/rogmem"


def test_b_qm_ingest_paths_write_mapped_lanes(tmp_path, monkeypatch):
    """B-QM: live / history / folder scan / upload land on p0–p3."""
    engine = _open(tmp_path, monkeypatch)
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO agents(id,device_id,device_name,program,display_name,status,pubkey,created_at)
           VALUES('ag','dev','机','Cursor','Cursor','已启用','k',?)""",
        (now,),
    )
    engine.db().commit()
    agent = engine.db().execute("SELECT * FROM agents WHERE id='ag'").fetchone()
    live = engine.submit_text(
        kind="user",
        text="即时会话里的一句原文。",
        session_id="s-live",
        turn_id="t-live",
        seq=1,
        agent=agent,
        event_at=now,
        project=None,
    )
    history = engine.ingest_history(
        [
            {
                "role": "user",
                "text": "历史会话里的一句原文。",
                "event_at": "2026-01-01T00:00:00+00:00",
                "session_id": "s-hist",
                "seq": 1,
            }
        ]
    )
    scan_dir = tmp_path / "scan"
    scan_dir.mkdir(exist_ok=True)
    scan_path = scan_dir / "scan.txt"
    scan_path.write_text("文件夹扫到的一句原文。", encoding="utf-8")
    scanned = engine.ingest_file(scan_path)
    uploaded = engine.import_bytes("batch.txt", "批量上传的一句原文。".encode("utf-8"))
    lanes = {
        live["id"]: engine.db().execute("SELECT lane FROM sources WHERE id=?", (live["id"],)).fetchone()["lane"],
        history["imported"][0]["id"]: engine.db().execute(
            "SELECT lane FROM sources WHERE id=?", (history["imported"][0]["id"],)
        ).fetchone()["lane"],
        scanned["id"]: engine.db().execute("SELECT lane FROM sources WHERE id=?", (scanned["id"],)).fetchone()["lane"],
        uploaded["id"]: engine.db().execute("SELECT lane FROM sources WHERE id=?", (uploaded["id"],)).fetchone()["lane"],
    }
    assert lanes[live["id"]] == "p0"
    assert lanes[history["imported"][0]["id"]] == "p1"
    assert lanes[scanned["id"]] == "p2"
    assert lanes[uploaded["id"]] == "p3"
    queued = {
        row["source_id"]: row["lane"]
        for row in engine.db().execute("SELECT source_id, lane FROM queue")
    }
    assert queued[live["id"]] == "p0"
    assert queued[history["imported"][0]["id"]] == "p1"
    assert queued[scanned["id"]] == "p2"
    assert queued[uploaded["id"]] == "p3"


def test_b_pr_interleave_high_low_eight_to_one():
    """B-PR: high:low ≈ 8:1 when both bands have ready work."""
    highs = [QueueCandidate(qid=i, source_id=f"h{i}", lane="p0") for i in range(1, 17)]
    lows = [QueueCandidate(qid=100 + i, source_id=f"l{i}", lane="p3") for i in range(1, 5)]
    remaining = highs + lows
    streak = 0
    order = []
    for _ in range(18):
        picked, streak = choose_next(remaining, high_streak=streak)
        assert picked is not None
        order.append(normalize_lane(picked.lane))
        remaining = [item for item in remaining if item.qid != picked.qid]
    assert order[:8] == [P0] * 8
    assert order[8] == P3
    assert order[9:17] == [P0] * 8
    assert order[17] == P3
    assert HIGH_RATIO == 8


def test_b_pr_02_low_slot_picks_lowest_available_band():
    """B-PR-02: low-band slot emits P3 when a P3 candidate exists, not P2."""
    highs = [QueueCandidate(qid=i, source_id=f"h{i}", lane="p0") for i in range(1, 9)]
    scan = QueueCandidate(qid=20, source_id="scan-p2", lane="p2")
    batch = QueueCandidate(qid=21, source_id="batch-p3", lane="p3")
    remaining = highs + [scan, batch]
    streak = 0
    order = []
    for _ in range(9):
        picked, streak = choose_next(remaining, high_streak=streak)
        assert picked is not None
        order.append((normalize_lane(picked.lane), picked.source_id))
        remaining = [item for item in remaining if item.qid != picked.qid]
    assert [rank for rank, _sid in order[:8]] == [P0] * 8
    assert order[8] == (P3, "batch-p3")
    only_p2 = [QueueCandidate(qid=1, source_id="h", lane="p0")] + [
        QueueCandidate(qid=2, source_id="scan-only", lane="p2")
    ]
    picked_p2, _ = choose_next(only_p2, high_streak=HIGH_RATIO)
    assert picked_p2 is not None
    assert picked_p2.source_id == "scan-only"


@pytest.mark.queue_ingested_at
def test_queue_ingested_at_same_band_picks_oldest_not_qid():
    """档内等待最久看 ingested_at：qid 更小但更新的不得插队。"""
    now = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)
    older = (now - timedelta(hours=2)).isoformat()
    newer = (now - timedelta(minutes=1)).isoformat()
    still_p3_old = (now - timedelta(minutes=5)).isoformat()
    late_qid_old = QueueCandidate(qid=99, source_id="waited-long", lane="p0", ingested_at=older)
    early_qid_new = QueueCandidate(qid=1, source_id="just-in", lane="p0", ingested_at=newer)
    picked, _ = choose_next([early_qid_new, late_qid_old], now=now, high_streak=0)
    assert picked is not None
    assert picked.source_id == "waited-long"
    assert picked.qid == 99
    low_old = QueueCandidate(qid=50, source_id="old-p3", lane="p3", ingested_at=still_p3_old)
    low_new = QueueCandidate(qid=2, source_id="new-p3", lane="p3", ingested_at=newer)
    assert low_old.effective(now) == P3
    assert low_new.effective(now) == P3
    picked_low, _ = choose_next([low_new, low_old], now=now, high_streak=HIGH_RATIO)
    assert picked_low is not None
    assert picked_low.source_id == "old-p3"


@pytest.mark.queue_ingested_at
def test_queue_ingested_at_interleave_picks_oldest_in_slot():
    """穿插槽内等待最久也看 ingested_at，不看 qid。"""
    now = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)
    older = (now - timedelta(minutes=10)).isoformat()
    newer = now.isoformat()
    highs = [
        QueueCandidate(qid=i, source_id=f"h{i}", lane="p0", ingested_at=newer)
        for i in range(1, 9)
    ]
    fresh_low = QueueCandidate(qid=10, source_id="fresh-p3", lane="p3", ingested_at=newer)
    old_low = QueueCandidate(qid=20, source_id="old-p3", lane="p3", ingested_at=older)
    remaining = highs + [fresh_low, old_low]
    streak = 0
    order = []
    for _ in range(9):
        picked, streak = choose_next(remaining, now=now, high_streak=streak)
        assert picked is not None
        order.append(picked.source_id)
        remaining = [item for item in remaining if item.qid != picked.qid]
    assert order[:8] == [f"h{i}" for i in range(1, 9)]
    assert order[8] == "old-p3"
    late_old_high = QueueCandidate(qid=9, source_id="old-p0", lane="p0", ingested_at=older)
    early_new_high = QueueCandidate(qid=1, source_id="new-p0", lane="p0", ingested_at=newer)
    low = QueueCandidate(qid=100, source_id="p3", lane="p3", ingested_at=newer)
    picked_high, _ = choose_next([early_new_high, late_old_high, low], now=now, high_streak=0)
    assert picked_high is not None
    assert picked_high.source_id == "old-p0"


@pytest.mark.queue_ingested_at
def test_queue_ingested_at_missing_does_not_count_as_oldest():
    """缺 ingested_at 不能靠空串 / 小 qid 假装等得最久。"""
    now = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)
    known = QueueCandidate(qid=2, source_id="known", lane="p0", ingested_at=now.isoformat())
    blank = QueueCandidate(qid=1, source_id="blank", lane="p0", ingested_at="")
    picked, _ = choose_next([blank, known], now=now, high_streak=0)
    assert picked is not None
    assert picked.source_id == "known"


@pytest.mark.queue_ingested_at
def test_a_retry_waits_behind_work_that_has_not_been_tried():
    now = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
    older = QueueCandidate(
        qid=1,
        source_id="old",
        lane="p0",
        kind="user",
        ingested_at=(now - timedelta(hours=5)).isoformat(),
        attempts=2,
    )
    fresh = QueueCandidate(
        qid=2,
        source_id="new",
        lane="p0",
        kind="user",
        ingested_at=now.isoformat(),
        attempts=0,
    )
    picked, _streak = choose_next([older, fresh], now=now)
    assert picked is not None
    assert picked.source_id == "new"


def test_lease_prefers_an_untried_unit_over_an_older_retry(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    _enqueue(engine, "fresh", "p0", kind="user", ingested_at=now.isoformat())
    _enqueue(engine, "retry", "p0", kind="user", ingested_at=(now - timedelta(hours=5)).isoformat())
    engine.db().execute("UPDATE queue SET attempts=3, last_error='The read operation timed out' WHERE source_id='retry'")
    engine.db().commit()
    leased = engine.lease_next()
    assert leased is not None
    assert leased["source_id"] == "fresh"


def test_queue_ingested_at_lease_uses_source_ingested_at_not_qid(tmp_path, monkeypatch):
    """lease 路径也按 sources.ingested_at，不按后写入的小 qid。"""
    engine = _open(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    _enqueue(engine, "newer", "p0", kind="user", ingested_at=now.isoformat())
    _enqueue(engine, "older", "p0", kind="user", ingested_at=(now - timedelta(hours=3)).isoformat())
    first = engine.lease_next()
    assert first is not None
    assert first["source_id"] == "older"
    assert waited_since(first["source"]["ingested_at"]) < now


def test_b_pr_aging_promotes_but_never_to_p0():
    """B-PR: 30min/band aging; P1 and aged P3 never become P0."""
    now = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)
    aged_30 = (now - timedelta(minutes=AGE_MINUTES + 1)).isoformat()
    aged_60 = (now - timedelta(minutes=AGE_MINUTES * 2 + 1)).isoformat()
    aged_long = (now - timedelta(hours=12)).isoformat()
    assert effective_priority(P3, aged_30, now) == P2
    assert effective_priority(P3, aged_60, now) == P1
    assert effective_priority(P3, aged_long, now) == P1
    assert effective_priority(P2, aged_30, now) == P1
    assert effective_priority(P1, aged_long, now) == P1
    assert effective_priority(P0, aged_long, now) == P0
    aged = QueueCandidate(qid=2, source_id="old-p3", lane="p3", ingested_at=aged_long)
    fresh_p0 = QueueCandidate(qid=1, source_id="now-p0", lane="p0", ingested_at=now.isoformat())
    picked, _ = choose_next([aged, fresh_p0], now=now, high_streak=0)
    assert picked is not None
    assert picked.source_id == "now-p0"
    picked_old, _ = choose_next([aged], now=now, high_streak=0)
    assert picked_old is not None
    assert picked_old.base == P3
    assert picked_old.effective(now) == P1


def test_b_pr_lease_honors_aging_and_legacy_fast(tmp_path, monkeypatch):
    """B-PR: lease_next uses effective priority; legacy fast still maps to P0."""
    engine = _open(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    _enqueue(engine, "legacy-fast", "fast", kind="user", ingested_at=now.isoformat())
    _enqueue(
        engine,
        "old-p3",
        "p3",
        kind="file",
        ingested_at=(now - timedelta(hours=12)).isoformat(),
    )
    first = engine.lease_next()
    assert first is not None
    assert first["source_id"] == "legacy-fast"
    assert normalize_lane(first["lane"], "user") == P0
    second = engine.lease_next()
    assert second is not None
    assert second["source_id"] == "old-p3"
    assert normalize_lane(second["lane"], "file") == P3
    assert effective_priority(P3, second["source"]["ingested_at"], now) == P1


def test_b_pr_priority_does_not_relax_evidence_gate():
    """B-PR: priority is scheduling only; evidence / covering gates stay closed."""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    sig = inspect.signature(evaluate_integrate)
    assert "priority" not in sig.parameters
    assert "lane" not in sig.parameters
    corpus = "云岚测试员甲于 2019 年在北窗实验室任职。"
    payload = {
        "identity": "same",
        "matched_id": "c-old",
        "ops": [
            {
                "op": "update",
                "path": "facts.0",
                "claim": "改成用户增值部",
                "covering": True,
            }
        ],
        "conflicts": [
            {
                "verdict": "supersede",
                "path": "facts.0",
                "evidence_new": "",
            }
        ],
    }
    result = evaluate_integrate(payload, candidates=["c-old"], corpus=corpus)
    covering_left = [
        op
        for op in (result.payload or {}).get("ops", payload["ops"])
        if op.get("covering") is True
    ]
    assert result.decision != "accept" or not covering_left or any(
        item.code in {"missing_evidence", "evidence_not_continuous", "covering_op_without_conflict"}
        or "evidence" in item.code
        for item in result.issues
    )
    from rogmem import queue_priority

    assert "from rogmem.distill_gate" not in inspect.getsource(queue_priority)
    assert "evaluate_integrate" not in inspect.getsource(queue_priority)


def test_a_page_draft_jumps_ahead_of_waiting_live_turns():
    older = "2026-01-01T00:00:00+00:00"
    newer = "2026-06-01T00:00:00+00:00"
    waiting = QueueCandidate(qid=1, source_id="live", lane="p0", ingested_at=older)
    draft = QueueCandidate(qid=2, source_id="draft", lane="jump", ingested_at=newer)
    picked, _streak = choose_next([waiting, draft])
    assert picked is not None and picked.source_id == "draft"
    assert normalize_lane("jump") < P0
    assert effective_priority(normalize_lane("jump"), newer) < P0
    assert lane_for(kind="draft", origin="retry", legacy_lane="jump") == "jump"
