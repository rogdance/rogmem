"""Proof-count ranking and recall trace (PDF-43 / PDF-44)."""

from __future__ import annotations

from rogmem.retrieve import PROOF_COUNT_ALPHA, apply_proof_boost, proof_boost, proof_norm, rrf_fuse_scored


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


def test_proof_norm_matches_hindsight_curve():
    assert proof_norm(None) == 0.5
    assert proof_norm(0) == 0.5
    assert proof_norm(1) == 0.5
    assert proof_boost(1) == 1.0
    assert PROOF_COUNT_ALPHA == 0.1
    assert proof_boost(150) > proof_boost(2) > 1.0


def test_apply_proof_boost_reorders_equal_rrf():
    scored = rrf_fuse_scored(
        [
            [("weak", {"text": "a"}), ("strong", {"text": "b"})],
            [],
            [],
            [],
        ]
    )
    # Same RRF positions artificially equalised by feeding one lane only —
    # boost must lift the multi-source statement above the single-source one
    # when we force equal base scores.
    equal = [
        ("weak", {"text": "a"}, 0.01, {"fts": 1}),
        ("strong", {"text": "b"}, 0.01, {"fts": 2}),
    ]
    boosted = apply_proof_boost(equal, {"weak": 1, "strong": 8})
    assert boosted[0][0] == "strong"
    assert boosted[0][3]["proof_count"] == 8
    assert boosted[0][3]["final_score"] > boosted[1][3]["final_score"]
    assert scored  # smoke: scored helper still works


def test_repeat_wording_accumulates_statement_sources(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    now = "2026-06-01T00:00:00+00:00"
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES('c1','人物','证人','active','{}','{}')"
    )
    for i, src in enumerate(("s1", "s2", "s3")):
        con.execute(
            """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane)
               VALUES(?,?,?,?, '夹具','已萃取','fast')""",
            (src, "user", now, f"h-{src}",),
        )
        sid = engine._add_statement(
            con,
            "c1",
            "概述",
            None,
            "证人确认雾季断电",
            "证人确认雾季断电",
            src,
            "user",
            "written",
            now,
        )
        assert sid  # same id after first
    con.commit()
    rows = con.execute("SELECT source_id FROM statement_sources WHERE statement_id=?", (sid,)).fetchall()
    assert {row["source_id"] for row in rows} == {"s1", "s2", "s3"}
    assert engine._statement_proof_count(con, sid) == 3


def test_search_trace_exposes_lanes_and_rrf(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    now = "2026-06-01T00:00:00+00:00"
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES('c1','概念','雾季课题','active','{}','{}')"
    )
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane)
           VALUES('src','user',?,'h','夹具','已萃取','fast')""",
        (now,),
    )
    con.execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,event_at,created_at)
           VALUES('st1','c1','概述','雾季课题春季故障','雾季课题春季故障','src','written','user',?,?)""",
        (now, now),
    )
    con.execute("INSERT INTO fts_stmt(stmt_id,text) VALUES(?,?)", ("st1", engine._tokens("雾季课题春季故障")))
    con.execute(
        "INSERT INTO statement_sources(statement_id,source_id,quote,created_at) VALUES('st1','src','雾季课题春季故障',?)",
        (now,),
    )
    con.commit()
    found = engine.search("雾季课题", limit=5, trace=True)
    assert "trace" in found
    names = [lane["name"] for lane in found["trace"]["lanes"]]
    assert names[:4] == ["identity", "fts", "graph", "temporal"]
    assert found["trace"]["lane_cap"] >= 20
    assert isinstance(found["trace"]["rrf"], list)
    plain = engine.search("雾季课题", limit=5)
    assert "trace" not in plain


def test_temporal_trace_count_is_full_in_window_pool(tmp_path, monkeypatch):
    """Trace count must reflect all in-window candidates, not the post-budget slice."""
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES('c1','概念','雾季课题','active','{}','{}')"
    )
    for i in range(25):
        src = f"s{i}"
        con.execute(
            """INSERT INTO sources(id,kind,event_at,event_basis,ingested_at,content_hash,body,status,lane)
               VALUES(?,?, '2025-02-10T00:00:00+00:00','utterance','2026-11-01T00:00:00+00:00',?, 'x','已萃取','fast')""",
            (src, "user", f"h{i}"),
        )
        con.execute(
            """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,event_at,created_at)
               VALUES(?,?, '概述',?,?,?,'written','user','2025-02-10T00:00:00+00:00','2026-11-01T00:00:00+00:00')""",
            (f"st{i}", "c1", f"雾季课题事项{i}", f"雾季课题事项{i}", src),
        )
    con.commit()
    found = engine.search("去年春天雾季课题", limit=5, trace=True)
    temporal = next(lane for lane in found["trace"]["lanes"] if lane["name"] == "temporal")
    assert temporal["count"] == 25
    assert temporal["capped_to"] <= temporal["cap"]
    assert temporal["truncated"] is True
