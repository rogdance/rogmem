"""The chunk vectors live in one numpy matrix now, maintained incrementally.

The corpus used to be a Python list of (id, vec) that was reparsed whole — inside the
engine lock — from per-unit paths. These tests pin the matrix contract and the two
guardrails that keep the reparse off the lock: forget must drop rows incrementally, and
the search path must not fall back to the old per-row Python dot product.
"""

from __future__ import annotations

import ast
import inspect
import json
import time

import pytest

from rogmem import engine


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.delenv("ROGMEM_LLM_BASE", raising=False)
    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _wait_size(store, want, timeout=5.0):
    deadline = time.time() + timeout
    while store.size() < want and time.time() < deadline:
        time.sleep(0.01)


def _cids(hits):
    return [cid for cid, _score in hits]


def _scores(hits):
    return [score for _cid, score in hits]


def _with_lock_managers(func):
    """Names of every `with <name>:` in a function, so a lock can be asserted absent."""
    tree = ast.parse(inspect.getsource(func))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for item in node.items:
                expr = item.context_expr
                if isinstance(expr, ast.Name):
                    names.append(expr.id)
                elif isinstance(expr, ast.Attribute):
                    names.append(expr.attr)
    return names


def test_add_then_search_scores_the_dot_product():
    store = engine._VectorStore()
    store.add(7, [1.0, 2.0, 3.0])
    store.add(9, [4.0, 5.0, 6.0])
    hits = store.search([1.0, 1.0, 1.0], 10)
    assert _cids(hits) == [9, 7]
    assert _scores(hits) == pytest.approx([15.0, 6.0], abs=1e-5)


def test_add_same_id_replaces_instead_of_duplicating():
    store = engine._VectorStore()
    store.add(1, [1.0, 0.0])
    store.add(1, [0.0, 1.0])
    assert store.size() == 1
    hits = store.search([0.0, 1.0], 5)
    assert _cids(hits) == [1]
    assert _scores(hits) == pytest.approx([1.0], abs=1e-5)


def test_drop_removes_it_from_search_and_size():
    store = engine._VectorStore()
    store.add(1, [1.0, 0.0])
    store.add(2, [0.0, 1.0])
    assert store.size() == 2
    store.drop([1])
    assert store.size() == 1
    hits = store.search([1.0, 1.0], 5)
    assert _cids(hits) == [2]


def test_drop_of_an_unknown_id_is_a_noop():
    store = engine._VectorStore()
    store.add(1, [1.0, 0.0])
    store.drop([999])
    assert store.size() == 1


def test_a_mismatched_width_is_parked_and_never_scored_in_the_matrix():
    store = engine._VectorStore()
    store.add(1, [1.0, 0.0])  # main width 2
    store.add(2, [1.0, 0.0, 0.0, 0.0])  # parked, width 4
    assert store.size() == 2
    # A query at the matrix width sees only the matrix row, never the parked one.
    assert _cids(store.search([1.0, 1.0], 5)) == [1]
    # A query at the parked width is compared the old way, on its own.
    assert _cids(store.search([1.0, 0.0, 0.0, 0.0], 5)) == [2]


def test_search_orders_by_score_and_caps_at_limit():
    store = engine._VectorStore()
    for cid, vec in {10: [5.0, 0.0], 11: [4.0, 0.0], 12: [3.0, 0.0], 13: [2.0, 0.0], 14: [1.0, 0.0]}.items():
        store.add(cid, vec)
    hits = store.search([1.0, 0.0], 3)
    assert _cids(hits) == [10, 11, 12]
    assert _scores(hits) == pytest.approx([5.0, 4.0, 3.0], abs=1e-5)


def test_search_breaks_a_tie_by_the_higher_id():
    store = engine._VectorStore()
    store.add(3, [1.0, 1.0])
    store.add(8, [1.0, 1.0])
    store.add(5, [1.0, 1.0])
    assert _cids(store.search([1.0, 0.0], 3)) == [8, 5, 3]


def test_search_before_the_fill_returns_empty_not_raise():
    store = engine._VectorStore()
    assert store.ready is False
    assert store.search([1.0, 0.0], 5) == []
    assert store.search([], 5) == []


def test_numpy_matrix_and_the_old_python_scoring_pick_the_same_rows():
    pairs = [
        (1, [0.1, 0.2, 0.3]),
        (2, [0.4, 0.5, 0.6]),
        (3, [0.6, 0.6, 0.6]),
        (4, [0.2, 0.9, 0.1]),
        (5, [0.3, 0.3, 0.3]),
        (6, [0.5, 0.5, 0.5]),
        (7, [0.5, 0.5, 0.5]),
    ]
    qv = [0.5, 0.5, 0.5]
    limit = 4
    store = engine._VectorStore()
    store.rebuild(pairs)
    new = store.search(qv, limit)
    # The old selection, verbatim from the loop this replaced.
    scored = []
    for cid, vec in pairs:
        if len(vec) != len(qv):
            continue
        scored.append((sum(a * b for a, b in zip(qv, vec)), cid))
    scored.sort(reverse=True)
    old = scored[:limit]
    assert _cids(new) == [cid for _score, cid in old]
    assert _scores(new) == pytest.approx([score for score, _cid in old], abs=1e-5)


def test_forget_drops_vectors_incrementally_without_a_sync_reload(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    token = "sk-abc_def9999zzzz"
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES('forget','user',?,'f-h',?,'已萃取','fast',1)""",
        (engine.utcnow(), f"请忘掉 {token}"),
    )
    cur = con.execute(
        "INSERT INTO chunks(source_id,seq,loc,text,cited,vec_json) VALUES('forget',0,'1',?,0,?)",
        (token, json.dumps([1.0, 0.0])),
    )
    cid = cur.lastrowid
    con.execute("INSERT INTO fts_chunk(chunk_id,text) VALUES(?,?)", (str(cid), engine._tokens(token)))
    con.commit()
    engine._load_vecs()
    _wait_size(engine._VECS, 1)
    assert engine._VECS.size() == 1
    calls = []
    monkeypatch.setattr(engine, "_load_vecs", lambda *args, **kwargs: calls.append(args))
    row = con.execute("SELECT * FROM sources WHERE id='forget'").fetchone()
    engine._maybe_forget(row)
    # The lock-holding path must not fall back to the synchronous whole-corpus reparse.
    assert calls == []
    assert engine._VECS.size() == 0
    assert engine._VECS.search([1.0, 0.0], 5) == []
    assert con.execute("SELECT vec_json FROM chunks WHERE id=?", (cid,)).fetchone()["vec_json"] is None


def test_the_reparse_never_runs_inside_the_engine_lock():
    for func in (engine._load_vecs, engine._reload_vecs_worker, engine._read_all_chunk_vectors):
        assert "LOCK" not in _with_lock_managers(func), func.__name__
    # `_load_vecs` only schedules; the parse itself is not in it.
    src = inspect.getsource(engine._load_vecs)
    assert "json.loads(" not in src
    assert "Thread" in src


def test_the_semantic_search_does_not_loop_vectors_in_python():
    src = inspect.getsource(engine.search)
    assert "for cid, vec in _VECS" not in src
    assert "_VECS.search(" in src


def test_search_carries_vectors_loaded_from_the_database(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane)
           VALUES('vs','file',?,'vs-h','夹具','已解析','slow')""",
        (engine.utcnow(),),
    )
    for seq, (text, vec) in enumerate({"近": [1.0, 0.0], "远": [0.0, 1.0]}.items()):
        cur = con.execute(
            "INSERT INTO chunks(source_id,seq,loc,text,cited,vec_json) VALUES('vs',?,'l',?,0,?)",
            (seq, text, json.dumps(vec)),
        )
        con.execute("INSERT INTO fts_chunk(chunk_id,text) VALUES(?,?)", (str(cur.lastrowid), engine._tokens(text)))
    con.commit()
    engine._load_vecs()
    _wait_size(engine._VECS, 2)
    monkeypatch.setattr(engine, "_embed_one", lambda _text: [1.0, 0.0])
    found = engine.search("完全无关的问句", limit=5)
    texts = [item.get("text", "") for item in found.get("raw", [])]
    assert texts
    assert texts[0] == "近"
