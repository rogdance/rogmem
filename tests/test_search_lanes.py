"""Graph + temporal search lanes and per-lane caps (P0-1 / Hindsight parity)."""

from __future__ import annotations

from datetime import datetime, timezone

from rogmem.retrieve import (
    DEFAULT_LANE_CAP,
    cap_per_source,
    rrf_fuse,
    select_with_temporal_coverage,
)
from rogmem.temporal_query import extract_temporal_window, strip_temporal_phrases


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.delenv("ROGMEM_LLM_BASE", raising=False)
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def test_cap_per_source_matches_hindsight_semantics():
    rows = [("a", {}), ("b", {}), ("c", {}), ("d", {})]
    assert [k for k, _ in cap_per_source(rows, 2)] == ["a", "b"]
    assert cap_per_source(rows, 0) is rows
    assert cap_per_source(rows, -1) is rows
    assert cap_per_source(rows, 10) is rows
    assert DEFAULT_LANE_CAP > 0


def test_temporal_coverage_round_robin_spreads_buckets():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 12, 31, tzinfo=timezone.utc)
    jan = datetime(2025, 1, 15, tzinfo=timezone.utc)
    jul = datetime(2025, 7, 15, tzinfo=timezone.utc)
    pool = [
        {"id": "j1", "similarity": 1.0, "event_at": jan},
        {"id": "j2", "similarity": 0.99, "event_at": jan},
        {"id": "j3", "similarity": 0.98, "event_at": jan},
        {"id": "u1", "similarity": 0.95, "event_at": jul},
    ]
    selected = select_with_temporal_coverage(pool, start, end, limit=2, n_buckets=8)
    months = {row["event_at"].month for row in selected}
    assert months == {1, 7}


def test_extract_temporal_window_chinese_season_and_english():
    ref = datetime(2026, 11, 20, tzinfo=timezone.utc)
    spring = extract_temporal_window("去年春天项目故障", ref)
    assert spring is not None
    assert spring[0].year == 2025 and spring[0].month == 1
    assert spring[1].month == 3
    assert "项目故障" in strip_temporal_phrases("去年春天项目故障")
    week = extract_temporal_window("last week", ref)
    assert week is not None
    assert week[0].date().isoformat() == "2026-11-09"
    assert extract_temporal_window("没有时间线索", ref) is None


def test_graph_lane_reaches_neighbour_statement(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    now = "2026-06-01T00:00:00+00:00"
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane)
           VALUES('g-src','user',?,'g-h','夹具','已萃取','fast')""",
        (now,),
    )
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES('alice','人物','爱丽丝','active','{}','{}')"
    )
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES('proj','工程','灯塔计划','active','{}','{}')"
    )
    con.execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,event_at,created_at)
           VALUES('st-alice','alice','概述','爱丽丝负责灯塔计划','爱丽丝负责灯塔计划','g-src','written','user',?,?)""",
        (now, now),
    )
    con.execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,event_at,created_at)
           VALUES('st-proj','proj','概述','灯塔计划在雾季断电','灯塔计划在雾季断电','g-src','written','user',?,?)""",
        (now, now),
    )
    con.execute("INSERT INTO fts_stmt(stmt_id,text) VALUES(?,?)", ("st-alice", engine._tokens("爱丽丝负责灯塔计划")))
    con.execute(
        "INSERT INTO edges(id,src,dst,kind,statement_id,ended) VALUES('e1','alice','proj','关联','st-alice',0)"
    )
    con.commit()
    found = engine.search("爱丽丝", limit=5)
    texts = [item.get("text", "") for item in found.get("crystals", [])]
    assert any("雾季断电" in text for text in texts), found


def test_temporal_lane_prefers_in_window_event_at_not_ingested(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,event_at,event_basis,ingested_at,content_hash,body,status,lane)
           VALUES('t-old','user','2025-02-10T00:00:00+00:00','utterance','2026-11-01T00:00:00+00:00','t-old-h','旧','已萃取','fast')"""
    )
    con.execute(
        """INSERT INTO sources(id,kind,event_at,event_basis,ingested_at,content_hash,body,status,lane)
           VALUES('t-new','user','2025-08-10T00:00:00+00:00','utterance','2026-11-01T00:00:00+00:00','t-new-h','新','已萃取','fast')"""
    )
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES('topic','概念','雾季课题','active','{}','{}')"
    )
    con.execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,event_at,created_at)
           VALUES('spring-hit','topic','概述','雾季课题春季故障复盘','雾季课题春季故障复盘','t-old','written','user',
                  '2025-02-10T00:00:00+00:00','2026-11-01T00:00:00+00:00')"""
    )
    con.execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,event_at,created_at)
           VALUES('summer-miss','topic','概述','雾季课题夏季扩容','雾季课题夏季扩容','t-new','written','user',
                  '2025-08-10T00:00:00+00:00','2026-11-01T00:00:00+00:00')"""
    )
    # No FTS rows: identity/FTS miss; temporal lane must surface the spring hit alone.
    found = engine.search("去年春天雾季课题", limit=5)
    texts = [item.get("text", "") for item in found.get("crystals", [])]
    assert any("春季故障" in text for text in texts), found
    assert all("夏季扩容" not in text for text in texts)


def test_search_still_ranks_strong_fts_first(tmp_path, monkeypatch):
    """Regression: existing FTS ranking must not regress after extra lanes."""
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    query = "暹罗猫每天吃鱼"
    weak = "每天吃一点青菜"
    strong = "暹罗猫每天吃鱼 暹罗猫每天吃鱼 暹罗猫每天吃鱼"
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES('rank-c','人物','夹具人','active','{}','{}')"
    )
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane)
           VALUES('rank-src','user','2026-10-25T00:00:00+00:00','rank-h','夹具','已萃取','fast')"""
    )
    for i in range(24):
        sid = f"weak-{i:02d}"
        text = f"{weak} {i}"
        con.execute(
            """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,created_at)
               VALUES(?,?,?,?,?,?, 'written','user',?)""",
            (sid, "rank-c", "概述", text, text, "rank-src", "2026-10-25T00:00:00+00:00"),
        )
        con.execute("INSERT INTO fts_stmt(stmt_id,text) VALUES(?,?)", (sid, engine._tokens(text)))
    con.execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,created_at)
           VALUES('strong-late','rank-c','概述',?,?, 'rank-src','written','user',?)""",
        (strong, strong, "2026-10-25T00:00:01+00:00"),
    )
    con.execute("INSERT INTO fts_stmt(stmt_id,text) VALUES(?,?)", ("strong-late", engine._tokens(strong)))
    con.commit()
    found = engine.search(query, limit=5)
    texts = [item.get("text", "") for item in found.get("crystals", [])]
    assert texts and strong in texts[0]


def test_rrf_fuse_still_k60():
    from rogmem import retrieve

    assert retrieve.RRF_K == 60
    fused = rrf_fuse([[("only", {"n": 1})], []])
    assert fused == [{"n": 1}]


def test_graph_per_seed_fanout_cap():
    from rogmem.search_lanes import GRAPH_PER_SEED_LIMIT, _neighbours

    assert GRAPH_PER_SEED_LIMIT == 200

    class _Row(dict):
        def __getitem__(self, key):
            return dict.__getitem__(self, key)

    class FakeCon:
        def execute(self, sql, params=()):
            # One hub with 250 neighbours; cap must keep 200.
            rows = [_Row({"src": "hub", "dst": f"n{i:03d}"}) for i in range(250)]

            class Result:
                def fetchall(self_inner):
                    return rows

            return Result()

    reached = _neighbours(FakeCon(), ["hub"], hops=1, per_seed_limit=200)
    assert len(reached) == 200


def test_temporal_pool_prefers_similarity_not_ingest_order(tmp_path, monkeypatch):
    """In-window hits must be similarity-ranked before TEMPORAL_POOL truncation."""
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES('topic','概念','雾季课题','active','{}','{}')"
    )
    # Many weak recent rows + one older strong match; all inside 去年春天.
    for i in range(70):
        sid = f"weak-{i:02d}"
        src = f"src-w-{i:02d}"
        con.execute(
            """INSERT INTO sources(id,kind,event_at,event_basis,ingested_at,content_hash,body,status,lane)
               VALUES(?,?, '2025-02-20T00:00:00+00:00','utterance','2026-11-01T00:00:00+00:00',?, '弱','已萃取','fast')""",
            (src, "user", f"h-{src}"),
        )
        con.execute(
            """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,event_at,created_at)
               VALUES(?,?, '概述',?,?,?,'written','user','2025-02-20T00:00:00+00:00','2026-11-02T00:00:00+00:00')""",
            (sid, "topic", f"无关絮语 {i}", f"无关絮语 {i}", src),
        )
    con.execute(
        """INSERT INTO sources(id,kind,event_at,event_basis,ingested_at,content_hash,body,status,lane)
           VALUES('src-strong','user','2025-02-01T00:00:00+00:00','utterance','2026-10-01T00:00:00+00:00','h-strong','强','已萃取','fast')"""
    )
    con.execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,event_at,created_at)
           VALUES('strong','topic','概述','雾季课题春季故障复盘','雾季课题春季故障复盘','src-strong','written','user',
                  '2025-02-01T00:00:00+00:00','2026-10-01T00:00:00+00:00')"""
    )
    con.commit()
    found = engine.search("去年春天雾季课题故障", limit=5)
    texts = [item.get("text", "") for item in found.get("crystals", [])]
    assert any("春季故障" in text for text in texts), texts
