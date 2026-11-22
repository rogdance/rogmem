"""Match=0 冷启 Fail 闸。

产品钉死正路径：零候选 → identity=new → #79 `_land_integrate_new` 薄建。
reject / deferred 只是负路径硬卡，不是默认。
禁 extract 假消化、禁静默 swallow、写开关关零落库。
prompts 零改。不宣称冷启闭环。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rogmem.crystal_store import DISTILLER_STORE_WRITE_ENABLED, CrystalStore, hydrate
from rogmem.distill_gate import PAYLOAD_KIND_EXTRACT, PAYLOAD_KIND_INTEGRATE

pytestmark = pytest.mark.match0_fail_gate

MISS_TEXT = "今天只谈天气，不提任何人名。"
NEW_DRAFT = {
    "name": "北窗讲师丙",
    "type": "人物",
    "aliases": ["讲师丙"],
    "summary": "在北窗实验室做讲师。",
    "owner_relation": "同事",
    "body_md": "## 事业经历\n\n云岚测试员甲于 2019 年在北窗实验室任职。",
    "facts": [
        {
            "claim": "2019 年在北窗实验室任职",
            "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
        }
    ],
    "org_title": "讲师",
}
NAMELESS_DRAFT = {
    "type": "人物",
    "aliases": [],
    "facts": [
        {
            "claim": "2019 年在北窗实验室任职",
            "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
        }
    ],
}
ILLEGAL_NEW = {"identity": "new", "ops": [{"op": "add", "path": "facts"}], "conflicts": []}


def _engine(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _queue_user(engine, source_id: str, body: str = MISS_TEXT) -> None:
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
            "SELECT id, type, name, status, summary_json, chapters_json, envelope_json, "
            "body_md, facts_json, revision FROM crystallizations WHERE status='active' "
            "ORDER BY id"
        )
    )


def _ids(rows):
    return {row["id"] for row in rows}


def _created_named(engine, before, name: str):
    return [row for row in _active_rows(engine) if row["id"] not in _ids(before) and row["name"] == name]


def _boom_extract(*_a, **_k):
    raise AssertionError("Match=0 must not fall back to extract")


def _wire_match0_draft(engine, monkeypatch, draft=NEW_DRAFT):
    monkeypatch.setattr(engine, "_match_integrate_parts", lambda _text: {})
    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: {"crystals": [draft]})
    monkeypatch.setattr(engine, "_llm_extract", _boom_extract)


# --- M0-CORE / M0-NEW 正路径 ---


def test_match0_to_new_lands_thin(tmp_path, monkeypatch):
    """M0-NEW-01: Match=0 + 非空 mid → identity=new 薄建成功。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    assert engine._match_integrate_parts(MISS_TEXT) == {}
    integrate_calls: list = []
    land_calls: list = []
    orig_land = engine._land_integrate_new

    def spy_integrate(*_a, **_k):
        integrate_calls.append(1)
        raise AssertionError("Match=0 must not call Integrate LLM")

    def spy_land(*args, **kwargs):
        land_calls.append({"args": args, "kwargs": kwargs})
        return orig_land(*args, **kwargs)

    _wire_match0_draft(engine, monkeypatch)
    monkeypatch.setattr(engine, "_llm_integrate", spy_integrate)
    monkeypatch.setattr(engine, "_land_integrate_new", spy_land)
    before = _active_rows(engine)
    _queue_user(engine, "src-m0-new")
    assert engine.pump_one() == "src-m0-new"
    assert _queue_row(engine, "src-m0-new")["state"] == "done"
    assert integrate_calls == []
    assert land_calls
    created = _created_named(engine, before, "北窗讲师丙")
    assert len(created) == 1
    row = created[0]
    assert row["status"] == "active"
    assert json.loads(row["summary_json"] or "{}") == {}
    assert json.loads(row["chapters_json"] or "{}") == {}
    thin = hydrate(row)
    assert thin.storage == "thin"
    assert thin.summary == "在北窗实验室做讲师。"
    assert "讲师丙" in thin.aliases
    events = CrystalStore(engine.db()).events(row["id"])
    assert any(item["kind"] == "promote_new" for item in events)


def test_match0_must_not_fallback_extract(tmp_path, monkeypatch):
    """M0-CORE-01: Match=0 不得产出可泵消化的 extract。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    extract_calls: list = []
    routed: list[str] = []

    def spy_extract(*_a, **_k):
        extract_calls.append(1)
        return engine._normalize_extract({"items": [], "critic": {}}, MISS_TEXT)

    def spy_items(*_a, **_k):
        routed.append("extract")
        return 0

    _wire_match0_draft(engine, monkeypatch)
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("integrate llm")))
    monkeypatch.setattr(engine, "_llm_extract", spy_extract)
    monkeypatch.setattr(engine, "_apply_items", spy_items)
    data = engine._produce_distill_payload(MISS_TEXT, "user")
    assert data.get("payload_kind") == PAYLOAD_KIND_INTEGRATE
    assert data.get("payload_kind") != PAYLOAD_KIND_EXTRACT
    assert data.get("identity") == "new"
    _queue_user(engine, "src-m0-no-extract")
    assert engine.pump_one() == "src-m0-no-extract"
    assert extract_calls == []
    assert routed == []
    queued = _queue_row(engine, "src-m0-no-extract")
    assert queued["state"] == "done"


def test_match0_no_silent_swallow(tmp_path, monkeypatch):
    """M0-CORE-02: Match=0 必须有可观测决策，禁止空 {} 蒸发。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _wire_match0_draft(engine, monkeypatch)
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("integrate llm")))
    data = engine._produce_distill_payload(MISS_TEXT, "user")
    assert data
    assert data.get("payload_kind") == PAYLOAD_KIND_INTEGRATE
    assert data.get("identity") == "new"
    assert data.get("_draft_crystal") == NEW_DRAFT
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?, 'user', 'qa', 'qa', ?, ?, ?, ?, 'fast', 1)""",
        ("src-m0-observe", engine.utcnow(), "src-m0-observe-h", MISS_TEXT, engine.ST_VEC_READY),
    )
    con.commit()
    source = con.execute("SELECT * FROM sources WHERE id='src-m0-observe'").fetchone()
    result = engine._route_distill_payload(source, data, MISS_TEXT)
    assert result is not None
    assert getattr(result, "decision", None) == "accept"
    assert getattr(result, "identity", None) == "new"

    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: None)
    empty = engine._produce_distill_payload(MISS_TEXT, "user")
    assert empty
    assert empty.get("payload_kind") == PAYLOAD_KIND_INTEGRATE
    assert empty.get("payload_kind") != PAYLOAD_KIND_EXTRACT
    _queue_user(engine, "src-m0-empty-mid")
    assert engine.pump_one() == "src-m0-empty-mid"
    queued = _queue_row(engine, "src-m0-empty-mid")
    assert queued["state"] != "done"
    assert queued["last_error"]


def test_match0_new_reuses_pr79_land_gates(tmp_path, monkeypatch):
    """M0-NEW-02: 走既有 _land_integrate_new；缺稿 / 无名不得假 accept。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    # 同一段合成材料在一个库里换桩重跑；分诊会把后几次当重复材料跳过。
    engine.set_setting("distill_triage", "0")
    engine.db().commit()
    land_calls: list = []
    orig_land = engine._land_integrate_new

    def spy_land(*args, **kwargs):
        land_calls.append(1)
        return orig_land(*args, **kwargs)

    _wire_match0_draft(engine, monkeypatch)
    monkeypatch.setattr(engine, "_land_integrate_new", spy_land)
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("integrate llm")))
    before = _active_rows(engine)
    _queue_user(engine, "src-m0-reuse-ok")
    assert engine.pump_one() == "src-m0-reuse-ok"
    assert land_calls
    assert _created_named(engine, before, "北窗讲师丙")

    land_calls.clear()
    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: {"crystals": [NAMELESS_DRAFT]})
    nameless_before = _active_rows(engine)
    _queue_user(engine, "src-m0-reuse-nameless")
    assert engine.pump_one() == "src-m0-reuse-nameless"
    queued = _queue_row(engine, "src-m0-reuse-nameless")
    assert queued["state"] != "done"
    assert queued["last_error"]
    assert _ids(_active_rows(engine)) == _ids(nameless_before)
    assert len(_created_named(engine, nameless_before, "北窗讲师丙")) == 0


def test_match0_single_mid_nloop_same_as_new(tmp_path, monkeypatch):
    """M0-NEW-03: 单枚 mid + Match=0 与 #79 new 同构，不得改回 extract。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    apply_calls: list = []
    orig_apply = engine.apply_integrate

    def spy_apply(*args, **kwargs):
        apply_calls.append({"args": args, "kwargs": kwargs})
        return orig_apply(*args, **kwargs)

    _wire_match0_draft(engine, monkeypatch)
    monkeypatch.setattr(engine, "apply_integrate", spy_apply)
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("integrate llm")))
    data = engine._produce_distill_payload(MISS_TEXT, "user")
    assert data["payload_kind"] == PAYLOAD_KIND_INTEGRATE
    assert "_integrate_batch" not in data
    assert data.get("identity") == "new"
    assert data.get("_draft_crystal") == NEW_DRAFT
    before = _active_rows(engine)
    _queue_user(engine, "src-m0-single")
    assert engine.pump_one() == "src-m0-single"
    assert len(apply_calls) == 1
    assert apply_calls[0]["kwargs"].get("draft_crystal") == NEW_DRAFT
    created = _created_named(engine, before, "北窗讲师丙")
    assert len(created) == 1
    assert hydrate(created[0]).storage == "thin"


# --- M0-REJ 负路径（仅硬卡，不是默认） ---


def test_match0_explicit_reject_or_deferred(tmp_path, monkeypatch):
    """M0-REJ-01: 显式 reject 零落库，不 extract。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    extract_calls: list = []
    monkeypatch.setattr(
        engine,
        "_llm_extract",
        lambda *_a, **_k: extract_calls.append(1) or engine._normalize_extract({"items": [], "critic": {}}, MISS_TEXT),
    )
    result = engine.apply_integrate(
        ILLEGAL_NEW,
        candidates=[],
        corpus=MISS_TEXT,
        store=CrystalStore(engine.db()),
        draft_crystal=NEW_DRAFT,
        holder="match0-explicit-reject",
    )
    assert result.decision == "rejected"
    assert any(item.code == "ops_conflicts_must_be_empty" for item in result.issues)
    assert _ids(_active_rows(engine)) == _ids(before)
    assert extract_calls == []
    assert all(row["name"] != "北窗讲师丙" for row in _active_rows(engine))


def test_match0_reject_pump_not_fake_done(tmp_path, monkeypatch):
    """M0-REJ-02: Match=0 负路径泵不得假 done。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _wire_match0_draft(engine, monkeypatch, NAMELESS_DRAFT)
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("integrate llm")))
    before = _active_rows(engine)
    _queue_user(engine, "src-m0-rej-pump")
    assert engine.pump_one() == "src-m0-rej-pump"
    queued = _queue_row(engine, "src-m0-rej-pump")
    assert queued["state"] != "done"
    assert queued["last_error"]
    assert _ids(_active_rows(engine)) == _ids(before)


def test_match0_land_fail_fail_closed_not_extract(tmp_path, monkeypatch):
    """M0-REUSE-02: 缺 draft / land 失败 → rejected，非 accept，非 extract。"""
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
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("integrate llm")))
    before = _active_rows(engine)
    data = engine._produce_distill_payload(MISS_TEXT, "user")
    assert data.get("payload_kind") == PAYLOAD_KIND_INTEGRATE
    assert data.get("payload_kind") != PAYLOAD_KIND_EXTRACT
    _queue_user(engine, "src-m0-land-fail")
    assert engine.pump_one() == "src-m0-land-fail"
    queued = _queue_row(engine, "src-m0-land-fail")
    assert queued["state"] != "done"
    assert queued["last_error"]
    assert extract_calls == []
    assert _ids(_active_rows(engine)) == _ids(before)


def test_match0_write_switch_off_zero_land(tmp_path, monkeypatch, write_switch_off):
    """M0-WS-01: 写开关关 → deferred / 零落库 / 非 accept / 泵不假 done。"""
    engine = _engine(tmp_path, monkeypatch)
    _wire_match0_draft(engine, monkeypatch)
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("integrate llm")))
    before = _active_rows(engine)
    data = engine._produce_distill_payload(MISS_TEXT, "user")
    assert data.get("identity") == "new"
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?, 'user', 'qa', 'qa', ?, ?, ?, ?, 'fast', 1)""",
        ("src-m0-ws-apply", engine.utcnow(), "src-m0-ws-apply-h", MISS_TEXT, engine.ST_VEC_READY),
    )
    con.commit()
    source = con.execute("SELECT * FROM sources WHERE id='src-m0-ws-apply'").fetchone()
    result = engine._route_distill_payload(source, data, MISS_TEXT)
    assert result.decision == "deferred"
    assert result.decision != "accept"
    assert any(item.code == "new_land_write_switch_off" for item in result.issues)
    assert _ids(_active_rows(engine)) == _ids(before)
    _queue_user(engine, "src-m0-ws-pump")
    assert engine.pump_one() == "src-m0-ws-pump"
    queued = _queue_row(engine, "src-m0-ws-pump")
    assert queued["state"] != "done"
    assert queued["last_error"]
    assert _ids(_active_rows(engine)) == _ids(before)
    assert all(row["name"] != "北窗讲师丙" for row in _active_rows(engine))


def test_match0_docs_prompts_git_diff_empty():
    """M0-PROMPT-01 / M0-REUSE-01: docs/prompts 真 git diff 为空。"""
    from tests.docs_prompts_zero_diff import assert_docs_prompts_zero_diff

    assert DISTILLER_STORE_WRITE_ENABLED is True
    assert_docs_prompts_zero_diff()
