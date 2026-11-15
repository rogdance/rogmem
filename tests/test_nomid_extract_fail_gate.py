"""无 mid 假消化 Fail 闸（旁路刀）。

契约：Match≥1 且无非空 mid → 禁落 _llm_extract 假消化；
fail-closed（rejected/deferred 或可观测 unwritten），泵非 done。
Match=0 无 mid 仍 identity=new（#86）。
有 mid 而 Integrate=None → _unwritten，禁 extract（#80）。
与 #80 None→unwritten 同构。

行为断言，禁止 inspect.getsource 纸老虎。docs/prompts 零改。
"""

from __future__ import annotations

import pytest

from rogmem.crystal_store import DISTILLER_STORE_WRITE_ENABLED
from rogmem.distill_gate import PAYLOAD_KIND_EXTRACT, PAYLOAD_KIND_INTEGRATE

pytestmark = pytest.mark.nomid_extract_fail_gate

CORPUS = "云岚测试员甲于 2019 年在北窗实验室任职。后来到南港书院做讲师。"
MISS_TEXT = "今天只谈天气，不提任何人名。"
HITS = [{"id": "c-yunlan", "name": "云岚甲", "type": "人物", "aliases": ["测试员甲"]}]
QING_DRAFT = {
    "name": "云岚甲",
    "type": "人物",
    "aliases": ["测试员甲"],
    "summary": "在北窗实验室任职。",
    "owner_relation": "同事",
    "body_md": "## 事业经历\n\n云岚测试员甲于 2019 年在北窗实验室任职。",
    "facts": [
        {
            "claim": "2019 年在北窗实验室任职",
            "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
        }
    ],
}
EMPTY_FACTS = {"name": "云岚甲", "type": "人物", "aliases": ["测试员甲"], "facts": []}


def _engine(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _queue_user(engine, source_id: str, body: str = CORPUS) -> None:
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?, 'user', 'qa', 'qa', ?, ?, ?, ?, 'fast', 1)""",
        (source_id, engine.utcnow(), source_id + "-h", body, engine.ST_VEC_READY),
    )
    con.execute("INSERT INTO queue(source_id,lane,state) VALUES(?, 'fast', 'queued')", (source_id,))
    con.commit()


def _queue_row(engine, source_id: str):
    return engine.db().execute(
        "SELECT state, last_error FROM queue WHERE source_id=?", (source_id,)
    ).fetchone()


def _active_rows(engine):
    return list(
        engine.db().execute(
            "SELECT id, type, name, status, body_md, revision FROM crystallizations "
            "WHERE status='active' ORDER BY id"
        )
    )


def _ids(rows):
    return {row["id"] for row in rows}


def _boom_extract(*_a, **_k):
    raise AssertionError("Match≥1 empty mid must not fall back to extract")


def _wire_match_ge1_empty_mid(engine, monkeypatch, *, draft=None):
    monkeypatch.setattr(engine, "_match_integrate_parts", lambda _text: {"library_candidates": HITS})
    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: draft)
    monkeypatch.setattr(engine, "_llm_extract", _boom_extract)
    monkeypatch.setattr(
        engine,
        "_llm_integrate",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("empty mid must not call Integrate LLM")),
    )


def _assert_unwritten_not_extract(data):
    assert data
    assert data.get("payload_kind") == PAYLOAD_KIND_INTEGRATE
    assert data.get("payload_kind") != PAYLOAD_KIND_EXTRACT
    assert data.get("_integrate_call_failed") is True
    assert data.get("identity") != "new"
    assert "items" not in data or data.get("items") in (None, [])


# --- NM-CORE：Match≥1 + 空 draft 不得假消化 ---


def test_match_ge1_empty_draft_payload_not_extract(tmp_path, monkeypatch):
    """NM-CORE-01: Match≥1 + 空 draft ⇒ payload 非 extract，可观测 unwritten。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    extract_calls: list = []
    _wire_match_ge1_empty_mid(engine, monkeypatch)
    monkeypatch.setattr(
        engine,
        "_llm_extract",
        lambda *_a, **_k: extract_calls.append(1) or {"payload_kind": "extract", "items": []},
    )
    data = engine._produce_distill_payload(CORPUS, "user", library_candidates=HITS)
    _assert_unwritten_not_extract(data)
    assert extract_calls == []


def test_match_ge1_empty_facts_stub_not_extract(tmp_path, monkeypatch):
    """NM-CORE-02: Match≥1 + 空 facts stub（无非空 mid）同样不得 extract。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    extract_calls: list = []
    _wire_match_ge1_empty_mid(engine, monkeypatch, draft={"crystals": [EMPTY_FACTS]})
    monkeypatch.setattr(
        engine,
        "_llm_extract",
        lambda *_a, **_k: extract_calls.append(1) or {"payload_kind": "extract", "items": []},
    )
    data = engine._produce_distill_payload(
        CORPUS,
        "user",
        draft_crystal=EMPTY_FACTS,
        library_candidates=HITS,
    )
    _assert_unwritten_not_extract(data)
    assert extract_calls == []


def test_match_ge1_empty_draft_pump_not_done(tmp_path, monkeypatch):
    """整份材料没有新稿：不得一次结案，留待重试。不改走 extract。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    extract_calls: list = []
    apply_items: list = []
    monkeypatch.setattr(engine, "_match_integrate_parts", lambda _text: {"library_candidates": HITS})
    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: None)
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: None)
    monkeypatch.setattr(
        engine,
        "_llm_extract",
        lambda *_a, **_k: extract_calls.append(1) or engine._normalize_extract({"items": [], "critic": {}}, CORPUS),
    )
    monkeypatch.setattr(engine, "_apply_items", lambda *_a, **_k: apply_items.append(1) or 0)
    before = _active_rows(engine)
    _queue_user(engine, "src-nomid-pump")
    assert engine.pump_one() == "src-nomid-pump"
    queued = _queue_row(engine, "src-nomid-pump")
    assert queued["state"] != "done"
    assert queued["last_error"]
    row = engine.db().execute("SELECT status FROM sources WHERE id='src-nomid-pump'").fetchone()
    assert row["status"] != engine.ST_NO_CRYSTAL
    assert extract_calls == []
    assert apply_items == []
    assert _ids(_active_rows(engine)) == _ids(before)


def test_match_ge1_empty_draft_route_rejected_not_accept(tmp_path, monkeypatch):
    """NM-CORE-03: unwritten 路由 rejected，不得假 accept。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _wire_match_ge1_empty_mid(engine, monkeypatch)
    data = engine._produce_distill_payload(CORPUS, "user", library_candidates=HITS)
    _assert_unwritten_not_extract(data)
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?, 'user', 'qa', 'qa', ?, ?, ?, ?, 'fast', 1)""",
        ("src-nomid-route", engine.utcnow(), "src-nomid-route-h", CORPUS, engine.ST_VEC_READY),
    )
    con.commit()
    source = con.execute("SELECT * FROM sources WHERE id='src-nomid-route'").fetchone()
    result = engine._route_distill_payload(source, data, CORPUS)
    assert result is not None
    assert getattr(result, "decision", None) in {"rejected", "deferred"}
    assert getattr(result, "decision", None) != "accept"
    assert any(item.code == "integrate_call_failed" for item in (result.issues or []))


# --- NM-ISO：与 #80 None→unwritten 同构 ---


def test_match_ge1_empty_draft_isomorphic_to_hash80_unwritten(tmp_path, monkeypatch):
    """NM-ISO-01: Match≥1+空 draft 与有 mid 而 Integrate=None 同构（unwritten，非 extract）。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    extract_calls: list = []
    monkeypatch.setattr(
        engine,
        "_llm_extract",
        lambda *_a, **_k: extract_calls.append(1) or {"payload_kind": "extract", "items": []},
    )

    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: None)
    empty = engine._produce_distill_payload(CORPUS, "user", library_candidates=HITS)

    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: {"crystals": [QING_DRAFT]})
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: None)
    mid_none = engine._produce_distill_payload(
        CORPUS,
        "user",
        draft_crystal=QING_DRAFT,
        library_candidates=HITS,
    )

    _assert_unwritten_not_extract(empty)
    _assert_unwritten_not_extract(mid_none)
    assert empty.get("payload_kind") == mid_none.get("payload_kind") == PAYLOAD_KIND_INTEGRATE
    assert empty.get("_integrate_call_failed") is True
    assert mid_none.get("_integrate_call_failed") is True
    assert extract_calls == []

    con = engine.db()
    for sid, payload in (("src-iso-empty", empty), ("src-iso-mid", mid_none)):
        con.execute(
            """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
               VALUES(?, 'user', 'qa', 'qa', ?, ?, ?, ?, 'fast', 1)""",
            (sid, engine.utcnow(), sid + "-h", CORPUS, engine.ST_VEC_READY),
        )
    con.commit()
    empty_result = engine._route_distill_payload(
        con.execute("SELECT * FROM sources WHERE id='src-iso-empty'").fetchone(), empty, CORPUS
    )
    mid_result = engine._route_distill_payload(
        con.execute("SELECT * FROM sources WHERE id='src-iso-mid'").fetchone(), mid_none, CORPUS
    )
    assert empty_result.decision == mid_result.decision
    assert empty_result.decision in {"rejected", "deferred"}
    empty_codes = {item.code for item in (empty_result.issues or [])}
    mid_codes = {item.code for item in (mid_result.issues or [])}
    assert "integrate_call_failed" in empty_codes
    assert "integrate_call_failed" in mid_codes


# --- NM-REG：#86 / #80 不得被本刀改坏 ---


def test_match0_empty_mid_still_identity_new(tmp_path, monkeypatch):
    """NM-REG-86: Match=0 无 mid 仍 identity=new，不得改成 extract 或本刀 unwritten。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    extract_calls: list = []
    monkeypatch.setattr(engine, "_match_integrate_parts", lambda _text: {})
    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: None)
    monkeypatch.setattr(
        engine,
        "_llm_extract",
        lambda *_a, **_k: extract_calls.append(1) or {"payload_kind": "extract", "items": []},
    )
    monkeypatch.setattr(
        engine,
        "_llm_integrate",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Match=0 must not call Integrate LLM")),
    )
    data = engine._produce_distill_payload(MISS_TEXT, "user")
    assert data.get("payload_kind") == PAYLOAD_KIND_INTEGRATE
    assert data.get("payload_kind") != PAYLOAD_KIND_EXTRACT
    assert data.get("identity") == "new"
    assert not data.get("_integrate_call_failed")
    assert extract_calls == []


def test_has_mid_integrate_none_still_unwritten(tmp_path, monkeypatch):
    """NM-REG-80: 有 mid 而 Integrate=None → _unwritten，禁 extract，泵非 done。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    extract_calls: list = []
    monkeypatch.setattr(engine, "_match_integrate_parts", lambda _text: {"library_candidates": HITS})
    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: {"crystals": [QING_DRAFT]})
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: None)
    monkeypatch.setattr(
        engine,
        "_llm_extract",
        lambda *_a, **_k: extract_calls.append(1) or {"payload_kind": "extract", "items": []},
    )
    data = engine._produce_distill_payload(CORPUS, "user", library_candidates=HITS)
    _assert_unwritten_not_extract(data)
    assert data.get("_draft_crystal") == QING_DRAFT
    before = _active_rows(engine)
    _queue_user(engine, "src-reg80-none")
    assert engine.pump_one() == "src-reg80-none"
    queued = _queue_row(engine, "src-reg80-none")
    assert queued["state"] != "done"
    assert queued["last_error"]
    assert extract_calls == []
    assert _ids(_active_rows(engine)) == _ids(before)


def test_nomid_docs_prompts_git_diff_empty():
    """NM-PROMPT-01: docs/prompts 真 git diff 为空。"""
    from tests.docs_prompts_zero_diff import assert_docs_prompts_zero_diff

    assert DISTILLER_STORE_WRITE_ENABLED is True
    assert_docs_prompts_zero_diff()


def test_nomid_fail_gate_inventory():
    """NM-FAIL-01: 硬卡测网必须齐（空 draft / 泵 / 同构 / #86 / #80）。"""
    names = {name for name in globals() if name.startswith("test_")}
    required = {
        "test_match_ge1_empty_draft_payload_not_extract",
        "test_match_ge1_empty_facts_stub_not_extract",
        "test_match_ge1_empty_draft_pump_not_done",
        "test_match_ge1_empty_draft_route_rejected_not_accept",
        "test_match_ge1_empty_draft_isomorphic_to_hash80_unwritten",
        "test_match0_empty_mid_still_identity_new",
        "test_has_mid_integrate_none_still_unwritten",
        "test_nomid_docs_prompts_git_diff_empty",
    }
    missing = sorted(required - names)
    assert not missing, f"无 mid 假消化 Fail 闸缺测: {missing}"
