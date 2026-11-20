"""Belief units + near-dup reconcile (Hindsight observation spirit)."""

from __future__ import annotations

from rogmem.belief import (
    DEDUP_TEXT_RATIO,
    attach_statement_belief,
    belief_proof_count,
    proof_count_for_statement,
    reinforce_restated_statements,
    should_merge,
    text_affinity,
    texts_conflict,
)


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


def test_text_affinity_and_conflict_gates():
    assert text_affinity("证人确认雾季断电", "证人确认雾季断电") == 1.0
    assert text_affinity("证人确认雾季断电", "证人确认了雾季断电") >= DEDUP_TEXT_RATIO
    assert texts_conflict("任期到2025年", "任期到2026年")
    assert texts_conflict("他在深圳任职", "他不在深圳任职")
    assert not texts_conflict("证人确认雾季断电", "证人确认了雾季断电")
    assert should_merge(
        "证人确认雾季断电",
        "证人确认了雾季断电",
        text_affinity("证人确认雾季断电", "证人确认了雾季断电"),
        threshold=DEDUP_TEXT_RATIO,
    )
    assert not should_merge(
        "任期到2025年",
        "任期到2026年",
        text_affinity("任期到2025年", "任期到2026年"),
        threshold=0.5,
    )


def test_near_dup_lands_one_belief_and_raises_proof(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    now = "2026-06-01T00:00:00+00:00"
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) "
        "VALUES('c1','人物','证人','active','{}','{}')"
    )
    for src, wording, quote in (
        ("s1", "证人确认雾季断电", "证人确认雾季断电"),
        ("s2", "证人确认了雾季断电", "证人确认了雾季断电"),
        ("s3", "证人确认雾季断电。", "证人确认雾季断电。"),
    ):
        con.execute(
            """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane)
               VALUES(?,?,?,?,?,'已萃取','fast')""",
            (src, "user", now, f"h-{src}", quote),
        )
        sid = engine._add_statement(
            con, "c1", "概述", None, wording, quote, src, "user", "written", now
        )
        assert sid
    con.commit()
    beliefs = con.execute(
        "SELECT id, text FROM beliefs WHERE crystal_id='c1' AND invalid_at IS NULL "
        "AND IFNULL(merged_into,'')=''"
    ).fetchall()
    assert len(beliefs) == 1
    bid = beliefs[0]["id"]
    sources = {
        row["source_id"]
        for row in con.execute(
            "SELECT source_id FROM belief_sources WHERE belief_id=?", (bid,)
        )
    }
    assert sources == {"s1", "s2", "s3"}
    assert belief_proof_count(con, bid) == 3
    # Search proof reads belief strength, not only exact statement_sources.
    member = con.execute(
        "SELECT statement_id FROM belief_statements WHERE belief_id=? LIMIT 1",
        (bid,),
    ).fetchone()
    assert proof_count_for_statement(con, member["statement_id"]) == 3
    assert engine._statement_proof_count(con, member["statement_id"]) == 3


def test_digit_conflict_keeps_separate_beliefs(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    now = "2026-06-01T00:00:00+00:00"
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) "
        "VALUES('c1','人物','证人','active','{}','{}')"
    )
    for src, text in (("s1", "任期到2025年结束"), ("s2", "任期到2026年结束")):
        con.execute(
            """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane)
               VALUES(?,?,?,?,?,'已萃取','fast')""",
            (src, "user", now, f"h-{src}", text),
        )
        engine._add_statement(
            con, "c1", "概述", None, text, text, src, "user", "written", now
        )
    con.commit()
    n = con.execute(
        "SELECT COUNT(*) AS n FROM beliefs WHERE crystal_id='c1' AND invalid_at IS NULL "
        "AND IFNULL(merged_into,'')=''"
    ).fetchone()["n"]
    assert n == 2


def test_reinforce_restated_quotes_without_merge_json(tmp_path, monkeypatch):
    """Material that again contains a live quote attaches proof — unified landing."""
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    now = "2026-06-01T00:00:00+00:00"
    quote = "证人确认雾季课题当晚断电"
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) "
        "VALUES('c1','概念','雾季课题','active','{}','{}')"
    )
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane)
           VALUES('s1','user',?,?,?,'已萃取','fast')""",
        (now, "h1", quote),
    )
    sid = engine._add_statement(
        con,
        "c1",
        "概述",
        None,
        quote,
        quote,
        "s1",
        "user",
        "written",
        now,
    )
    body2 = f"材料复述：{quote}。另有闲话。"
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane)
           VALUES('s2','user',?,'h2',?,'已萃取','fast')""",
        (now, body2),
    )
    n = reinforce_restated_statements(
        con,
        crystal_id="c1",
        source_id="s2",
        body=body2,
    )
    con.commit()
    assert n == 1
    rows = con.execute(
        "SELECT source_id FROM statement_sources WHERE statement_id=?", (sid,)
    ).fetchall()
    assert {row["source_id"] for row in rows} == {"s1", "s2"}
    assert engine._statement_proof_count(con, sid) >= 2


def test_paraphrase_keeps_new_statement_wording(tmp_path, monkeypatch):
    """Belief near-dup must not collapse statement identity or freeze old wording."""
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    now = "2026-06-01T00:00:00+00:00"
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) "
        "VALUES('c1','人物','证人','active','{}','{}')"
    )
    first = "证人确认雾季课题当晚断电"
    second = "证人确认了雾季课题当晚断电"
    for src, wording in (("s1", first), ("s2", second)):
        con.execute(
            """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane)
               VALUES(?,?,?,?,?,'已萃取','fast')""",
            (src, "user", now, f"h-{src}", wording),
        )
        engine._add_statement(
            con, "c1", "概述", None, wording, wording, src, "user", "written", now
        )
    con.commit()
    live = con.execute(
        """SELECT id, text FROM statements
           WHERE crystal_id='c1' AND invalid_at IS NULL AND trust='written'"""
    ).fetchall()
    assert len(live) == 1
    assert live[0]["text"] == second
    beliefs = con.execute(
        "SELECT id FROM beliefs WHERE crystal_id='c1' AND invalid_at IS NULL "
        "AND IFNULL(merged_into,'')=''"
    ).fetchall()
    assert len(beliefs) == 1
    assert belief_proof_count(con, beliefs[0]["id"]) == 2


def test_short_quote_does_not_reinforce(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    now = "2026-06-01T00:00:00+00:00"
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) "
        "VALUES('c1','概念','端口','active','{}','{}')"
    )
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane)
           VALUES('s1','user',?,'h1','端口 18080','已萃取','fast')""",
        (now,),
    )
    # Direct row: short quote that would false-hit many bodies if reinforced.
    con.execute(
        """INSERT INTO statements(
             id, crystal_id, field, text, quote, source_id, trust, rank, created_at
           ) VALUES('st1','c1','概述','生产端口','18080','s1','written','user',?)""",
        (now,),
    )
    n = reinforce_restated_statements(
        con,
        crystal_id="c1",
        source_id="s2",
        body="别处也写了 18080 与无关闲话。",
    )
    assert n == 0


def test_belief_schema_exists_after_connect(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    tables = {
        row[0]
        for row in engine.db().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"beliefs", "belief_sources", "belief_statements"} <= tables


def test_attach_refuses_empty_quote(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    now = "2026-06-01T00:00:00+00:00"
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) "
        "VALUES('c1','人物','证人','active','{}','{}')"
    )
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane)
           VALUES('s1','user',?,'h','x','已萃取','fast')""",
        (now,),
    )
    con.execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,event_at,created_at)
           VALUES('st1','c1','概述','说法','','s1','written','user',?,?)""",
        (now, now),
    )
    assert (
        attach_statement_belief(
            con,
            statement_id="st1",
            crystal_id="c1",
            field="概述",
            chapter=None,
            text="说法",
            quote="",
            source_id="s1",
            trust="written",
        )
        is None
    )
