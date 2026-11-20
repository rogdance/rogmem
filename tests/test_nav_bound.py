"""Navigation reads a window or one page. It does not materialize the ledger."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from rogmem.crystal_store import CrystalStore, ThinCrystal


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_SKIP_SCHEDULER", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def test_source_order_does_not_read_the_body(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    body = "句" * 30
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES('talk','user','2026-01-01T00:00:00+00:00','h',?,'已萃取','fast',1)""",
        (body,),
    )
    engine.db().commit()
    seen: list[str] = []
    engine.db().set_trace_callback(lambda statement: seen.append(statement) if statement else None)
    listed = engine.list_sources("session")
    engine.db().set_trace_callback(None)
    ordering = [sql for sql in seen if "ORDER BY s.ingested_at" in sql]
    assert ordering
    assert all("s.body" not in sql for sql in ordering)
    assert listed["rows"][0]["preview"] == body


def test_graph_reads_live_mentions_and_skips_ended_history(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    store = CrystalStore(engine.db())
    store.put(ThinCrystal(id="org", type="组织", name="南港书院", status="active", revision=0))
    store.put(ThinCrystal(id="person", type="人物", name="云岚甲", status="active", revision=0))
    engine.db().execute(
        "INSERT INTO edges(id,src,dst,kind,statement_id,ended) VALUES('old','person','org','提及','',1)"
    )
    engine.db().execute(
        "INSERT INTO edges(id,src,dst,kind,statement_id,ended) VALUES('now','person','org','提及','',0)"
    )
    engine.db().commit()
    edges = engine.graph()["edges"]
    assert {item["src"] + item["dst"] for item in edges} == {"personorg"}
    named = engine.db().execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='edges_mention'"
    ).fetchone()
    assert named is not None


def test_queue_window_uses_the_stored_instant(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    now = datetime(2026, 11, 14, 6, 0, tzinfo=timezone.utc)
    inside = (now - timedelta(hours=1)).isoformat()
    ancient = (now - timedelta(days=40)).isoformat()
    engine.db().executemany(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?,?,?,?,'字','已萃取','p1',1)""",
        [("new", "file", inside, "n"), ("old", "file", ancient, "o")],
    )
    engine.db().executemany(
        """INSERT INTO queue(source_id,lane,state,attempts,enqueued_at,finished_at)
           VALUES(?, 'p1', 'done', 1, ?, ?)""",
        [("new", inside, inside), ("old", ancient, ancient)],
    )
    engine.db().commit()
    from rogmem.queue_board import view

    seen: list[str] = []
    engine.db().set_trace_callback(lambda statement: seen.append(statement) if statement else None)
    board = view("24h", "all", 1, now)
    engine.db().set_trace_callback(None)
    assert board["summary"]["done_tasks"] == 1
    assert {row["source_id"] for row in board["rows"]} == {"new"}
    joined = "\n".join(seen)
    assert "q.finished_jd >=" in joined


def test_chart_stops_at_the_window(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem import cost
    from rogmem.availability import for_chart

    start = datetime(2026, 11, 14, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=2)
    cost.record(
        at=(start - timedelta(days=40)).isoformat(),
        worker="草稿",
        provider_id="p",
        model="m",
        usage=(1, 1, 0),
        prompt="远",
        answer="远",
    )
    cost.record(
        at=(start + timedelta(minutes=30)).isoformat(),
        worker="草稿",
        provider_id="p",
        model="m",
        usage=(1, 1, 0),
        prompt="近",
        answer="近",
    )
    seen: list[str] = []
    real = sqlite3.connect

    def wrapped(*args, **kwargs):
        con = real(*args, **kwargs)
        if args and str(args[0]).endswith("cost.sqlite"):
            con.set_trace_callback(lambda statement: seen.append(statement) if statement else None)
        return con

    monkeypatch.setattr(sqlite3, "connect", wrapped)
    found = for_chart(
        [{"at": start.isoformat(), "grain": "hour"}],
        {"start": start.isoformat(), "end": end.isoformat(), "grain": "hour"},
    )
    assert found["buckets"]
    assert any("at_jd" in sql for sql in seen)
    assert not any("ORDER BY id" in sql and "at_jd" not in sql for sql in seen)
