"""FTS5 检索必须按 bm25/rank 取 top-k。QA-BENCH-001 / BUG-001。"""

from __future__ import annotations

import inspect
import time

from rogmem import engine

QUERY = "暹罗猫每天吃鱼"
WEAK = "每天吃一点青菜"
STRONG = "暹罗猫每天吃鱼 暹罗猫每天吃鱼 暹罗猫每天吃鱼"
WEAK_N = 24


def _open_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.delenv("ROGMEM_LLM_BASE", raising=False)
    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _seed_ranked_statements(con) -> None:
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES('rank-c','人物','夹具人','active','{}','{}')"
    )
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane)
           VALUES('rank-src','user','2026-10-25T00:00:00+00:00','rank-h','夹具','已蒸馏','fast')"""
    )
    for i in range(WEAK_N):
        sid = f"weak-{i:02d}"
        text = f"{WEAK} {i}"
        con.execute(
            """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,created_at)
               VALUES(?,?,?,?,?,?, 'written','user',?)""",
            (sid, "rank-c", "概述", text, text, "rank-src", "2026-10-25T00:00:00+00:00"),
        )
        con.execute("INSERT INTO fts_stmt(stmt_id,text) VALUES(?,?)", (sid, engine._tokens(text)))
    con.execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,created_at)
           VALUES('strong-late','rank-c','概述',?,?, 'rank-src','written','user',?)""",
        (STRONG, STRONG, "2026-10-25T00:00:01+00:00"),
    )
    con.execute("INSERT INTO fts_stmt(stmt_id,text) VALUES(?,?)", ("strong-late", engine._tokens(STRONG)))
    con.commit()


def _seed_ranked_chunks(con) -> None:
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane)
           VALUES('rank-raw','file','2026-10-25T00:00:00+00:00','rank-raw-h','夹具原文','已解析','slow')"""
    )
    for i in range(WEAK_N):
        text = f"{WEAK} {i}"
        cur = con.execute(
            "INSERT INTO chunks(source_id,seq,loc,text,cited) VALUES(?,?,?,?,0)",
            ("rank-raw", i, str(i), text),
        )
        con.execute("INSERT INTO fts_chunk(chunk_id,text) VALUES(?,?)", (str(cur.lastrowid), engine._tokens(text)))
    cur = con.execute(
        "INSERT INTO chunks(source_id,seq,loc,text,cited) VALUES(?,?,?,?,0)",
        ("rank-raw", WEAK_N, "strong", STRONG),
    )
    con.execute("INSERT INTO fts_chunk(chunk_id,text) VALUES(?,?)", (str(cur.lastrowid), engine._tokens(STRONG)))
    con.commit()


def test_unranked_match_limit_is_flooded_by_early_rowids(tmp_path, monkeypatch):
    """夹具自检：同一批数据上，不写 ORDER BY rank 时强命中进不了 top-k。"""
    _open_engine(tmp_path, monkeypatch)
    _seed_ranked_statements(engine.db())
    mq = engine._match_query(QUERY)
    ids = [
        row["stmt_id"]
        for row in engine.db().execute(
            "SELECT stmt_id FROM fts_stmt WHERE fts_stmt MATCH ? LIMIT ?",
            (mq, 5),
        )
    ]
    assert ids
    assert all(item.startswith("weak-") for item in ids)
    assert "strong-late" not in ids


def test_search_crystals_put_late_strong_match_first(tmp_path, monkeypatch):
    _open_engine(tmp_path, monkeypatch)
    _seed_ranked_statements(engine.db())
    found = engine.search(QUERY, limit=5)
    texts = [item.get("text", "") for item in found.get("crystals", [])]
    assert texts, found
    assert STRONG in texts
    assert texts[0] == STRONG
    assert not any(item.startswith(WEAK) for item in texts[:1])


def test_search_raw_puts_late_strong_chunk_first(tmp_path, monkeypatch):
    _open_engine(tmp_path, monkeypatch)
    _seed_ranked_chunks(engine.db())
    found = engine.search(QUERY, limit=5)
    texts = [item.get("text", "") for item in found.get("raw", [])]
    assert texts, found
    assert STRONG in texts
    assert texts[0] == STRONG


def _wait_vec_ready(engine, timeout=5.0):
    """The corpus loads on a background thread now; wait for that first fill to settle."""
    deadline = time.time() + timeout
    while not engine._VECS.ready and time.time() < deadline:
        time.sleep(0.01)


def test_vector_hit_is_not_dropped_when_keywords_fill_the_page(tmp_path, monkeypatch):
    engine = _open_engine(tmp_path, monkeypatch)
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane)
           VALUES('vec-raw','file','2026-10-25T00:00:00+00:00','vec-h','夹具','已解析','slow')"""
    )
    for i in range(8):
        text = f"{WEAK} {i}"
        cur = con.execute(
            "INSERT INTO chunks(source_id,seq,loc,text,cited) VALUES(?,?,?,?,0)",
            ("vec-raw", i, str(i), text),
        )
        con.execute("INSERT INTO fts_chunk(chunk_id,text) VALUES(?,?)", (str(cur.lastrowid), engine._tokens(text)))
    semantic = "蓝色短毛独自睡觉"
    cur = con.execute(
        "INSERT INTO chunks(source_id,seq,loc,text,cited) VALUES(?,?,?,?,0)",
        ("vec-raw", 8, "sem", semantic),
    )
    semantic_id = cur.lastrowid
    con.execute("INSERT INTO fts_chunk(chunk_id,text) VALUES(?,?)", (str(semantic_id), engine._tokens(semantic)))
    con.commit()
    _wait_vec_ready(engine)
    engine._VECS.rebuild([(semantic_id, [1.0, 0.0])])
    monkeypatch.setattr(engine, "_embed_one", lambda _text: [1.0, 0.0])
    found = engine.search(QUERY, limit=5)
    texts = [item.get("text", "") for item in found.get("raw", [])]
    assert semantic in texts, texts


def test_secret_question_returns_the_cited_original(tmp_path, monkeypatch):
    engine = _open_engine(tmp_path, monkeypatch)
    con = engine.db()
    secret = "密钥 sk-abc_def9999zzzz 留在原文"
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane)
           VALUES('sec-src','user','2026-10-25T00:00:00+00:00','sec-h',?,'已蒸馏','fast')""",
        (secret,),
    )
    cur = con.execute(
        "INSERT INTO chunks(source_id,seq,loc,text,cited) VALUES('sec-src',0,'1',?,1)",
        (secret,),
    )
    con.execute("INSERT INTO fts_chunk(chunk_id,text) VALUES(?,?)", (str(cur.lastrowid), engine._tokens(secret)))
    con.execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,created_at)
           VALUES('sec-st','owner','真名','有一则令牌',?,?, 'written','user','2026-10-25T00:00:00+00:00')""",
        ("«令牌»", "sec-src"),
    )
    con.commit()
    found = engine.search("我的密钥在哪", limit=5)
    assert found["prefer_raw"] is True
    assert found["crystals"] == []
    assert any(secret in item.get("text", "") for item in found["raw"])
    assert engine._prefer_raw_query("这条字段值是多少") is True
    assert engine._prefer_raw_query("今天吃什么") is False


def test_forget_does_not_treat_underscore_as_a_wildcard(tmp_path, monkeypatch):
    engine = _open_engine(tmp_path, monkeypatch)
    con = engine.db()
    token = "sk-abc_def9999zzzz"
    decoy = "sk-abcXdef9999zzzz"
    now = engine.utcnow()
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES('keep','user',?,'keep-h',?,'已蒸馏','fast',1)""",
        (now, f"保留 {decoy} 不动"),
    )
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES('drop','user',?,'drop-h',?,'已蒸馏','fast',1)""",
        (now, f"请忘掉 {token}"),
    )
    con.commit()
    row = con.execute("SELECT * FROM sources WHERE id='drop'").fetchone()
    engine._maybe_forget(row)
    kept = con.execute("SELECT body FROM sources WHERE id='keep'").fetchone()["body"]
    dropped = con.execute("SELECT body FROM sources WHERE id='drop'").fetchone()["body"]
    assert decoy in kept
    assert token not in dropped


def test_fts_sql_orders_by_rank():
    src = inspect.getsource(engine._fts_top) + inspect.getsource(engine.search)
    assert "ORDER BY rank" in src
    assert src.count("ORDER BY rank") >= 1
    assert "MATCH ? LIMIT" not in inspect.getsource(engine._fts_top)
    assert "MATCH ? LIMIT" not in inspect.getsource(engine.search)
