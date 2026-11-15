"""A/#79 残留 Fail 闸：facts evidence 保留 + identity=new id 回填。

对照 QA `ab-fail-gate-cases.md` §A（AB-A-EV / ID / NEG / WS / PATH / PROMPT）。
断言库读回、statements quote、GateResult、泵/queue；禁止源码 sniff 冒充绿。
本刀不做 B（厚读 / project_thick）。docs/prompts 真 git diff 必须空。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rogmem.crystal_store import DISTILLER_STORE_WRITE_ENABLED, CrystalStore, hydrate
from rogmem.distill_gate import GateResult

pytestmark = pytest.mark.ab79_fail_gate

CORPUS = "云岚测试员甲于 2019 年在北窗实验室任职。后来到南港书院做讲师。"
EVIDENCE = "云岚测试员甲于 2019 年在北窗实验室任职。"
CLAIM = "2019 年在北窗实验室任职"
NEW_DRAFT = {
    "name": "北窗讲师丙",
    "type": "人物",
    "aliases": ["讲师丙"],
    "summary": "在北窗实验室做讲师。",
    "owner_relation": "同事",
    "body_md": "## 事业经历\n\n云岚测试员甲于 2019 年在北窗实验室任职。",
    "facts": [{"claim": CLAIM, "evidence": EVIDENCE}],
    "org_title": "讲师",
}
EMPTY_EVIDENCE_DRAFT = {
    "name": "空证据戊",
    "type": "人物",
    "aliases": [],
    "facts": [{"claim": "只有主张没有出处", "evidence": ""}],
}
yunlan_DRAFT = {
    "name": "云岚甲",
    "type": "人物",
    "aliases": ["测试员甲"],
    "summary": "在北窗实验室任职。",
    "owner_relation": "同事",
    "body_md": "## 事业经历\n\n云岚测试员甲于 2019 年在北窗实验室任职。",
    "facts": [{"claim": CLAIM, "evidence": EVIDENCE}],
}
SCHOOL_DRAFT = {
    "name": "南港书院",
    "type": "组织",
    "aliases": [],
    "summary": "云岚甲后来任教的书院。",
    "owner_relation": "任职机构",
    "body_md": "## 发展沿革\n\n后来到南港书院做讲师。",
    "facts": [{"claim": "后来到南港书院做讲师", "evidence": "后来到南港书院做讲师。"}],
}
NEW_ACCEPT = {"identity": "new", "ops": [], "conflicts": []}
MAYBE_ACCEPT = {"identity": "maybe", "ops": [], "conflicts": []}
SAME_BODY = {
    "identity": "same",
    "matched_id": "owner",
    "ops": [
        {
            "op": "update",
            "path": "body_md",
            "value": CORPUS,
            "fact": {"claim": "2019 入职", "evidence": EVIDENCE},
        }
    ],
    "conflicts": [],
}
SAME_FACT_ADD = {
    "identity": "same",
    "matched_id": "owner",
    "ops": [
        {
            "op": "add",
            "path": "facts",
            "value": {"claim": CLAIM, "evidence": EVIDENCE},
        }
    ],
    "conflicts": [],
}
SAME_yunlan = {
    "identity": "same",
    "matched_id": "c-yunlan",
    "ops": [
        {
            "op": "update",
            "path": "body_md",
            "value": CORPUS,
            "fact": {"claim": "2019 入职", "evidence": EVIDENCE},
        }
    ],
    "conflicts": [],
}

REQUIRED_NEG_TESTS = {
    "test_ab_a_write_switch_off_new_deferred_no_write",
    "test_ab_a_missing_draft_rejected",
    "test_ab_a_empty_matched_id_after_successful_new_is_fail",
    "test_ab_a_land_fail_not_accept_empty_id_ok",
    "test_ab_a_value_key_must_not_drop_statement_evidence",
}


def _engine(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


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


def _created(engine, before):
    return [row for row in _active_rows(engine) if row["id"] not in _ids(before)]


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


def _seed_named(engine, cid: str, name: str, ctype: str = "人物") -> None:
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json)
           VALUES(?,?,?,'active',?,?)""",
        (cid, ctype, name, json.dumps({"名称": name}, ensure_ascii=False), "{}"),
    )
    engine.db().commit()


def _draft_bundle(*crystals):
    return {"crystals": list(crystals)}


def _fact_statements(engine, crystal_id: str, claim: str):
    return list(
        engine.db().execute(
            "SELECT text, quote FROM statements WHERE crystal_id=? AND text=?",
            (crystal_id, claim),
        )
    )


def _assert_facts_keep_evidence(row, claim: str = CLAIM, evidence: str = EVIDENCE) -> None:
    thin = hydrate(row)
    matched = [fact for fact in thin.facts if fact.claim == claim]
    assert matched, f"crystal {row['id']} missing claim {claim!r}"
    assert all(fact.evidence == evidence for fact in matched)
    facts_json = json.loads(row["facts_json"] or "[]")
    env = json.loads(row["envelope_json"] or "{}")
    for blob in (facts_json, env.get("facts") or []):
        hits = [item for item in blob if isinstance(item, dict) and item.get("claim") == claim]
        assert hits
        assert all(item.get("evidence") == evidence for item in hits)


def _assert_statement_quote_keeps_evidence(engine, crystal_id: str, claim: str = CLAIM, evidence: str = EVIDENCE) -> None:
    rows = _fact_statements(engine, crystal_id, claim)
    assert rows, f"no statement for claim {claim!r} on {crystal_id}"
    assert all((row["quote"] or "") == evidence for row in rows), (
        f"statements quote dropped evidence for {crystal_id}: "
        + repr([(row["text"], row["quote"]) for row in rows])
    )


def _land_new(engine, draft=None, payload=None, **kwargs):
    return engine.apply_integrate(
        payload if payload is not None else dict(NEW_ACCEPT),
        candidates=["owner"],
        corpus=CORPUS,
        store=CrystalStore(engine.db()),
        draft_crystal=NEW_DRAFT if draft is None else draft,
        holder=kwargs.pop("holder", "ab-a-new"),
        **kwargs,
    )


# --- AB-A-EV ---


def test_ab_a_new_land_crystal_facts_keep_evidence(tmp_path, monkeypatch):
    """AB-A-EV-01: new 落库后 Crystal.facts 保留 evidence。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    result = _land_new(engine)
    assert result.decision == "accept"
    created = _created(engine, before)
    assert len(created) == 1
    _assert_facts_keep_evidence(created[0])
    loaded = CrystalStore(engine.db()).get(created[0]["id"])
    assert loaded is not None
    assert any(fact.claim == CLAIM and fact.evidence == EVIDENCE for fact in loaded.facts)


def test_ab_a_new_land_statements_quote_keeps_evidence(tmp_path, monkeypatch):
    """AB-A-EV-02: sidecar statements quote 必须写入 fact.evidence（主挡合）。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    result = _land_new(engine)
    assert result.decision == "accept"
    created = _created(engine, before)
    assert len(created) == 1
    _assert_statement_quote_keeps_evidence(engine, created[0]["id"])


def test_ab_a_value_key_must_not_drop_statement_evidence(tmp_path, monkeypatch):
    """AB-A-EV-03: 生产 _draft_thin_ops 仍走 value= 时，sidecar 不得丢掉 evidence。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    ops = engine._draft_thin_ops(NEW_DRAFT)
    fact_ops = [op for op in ops if str(op.get("path") or "").startswith("facts")]
    assert fact_ops, "production thin-ops must emit a facts add"
    assert all(isinstance(op.get("value"), dict) for op in fact_ops)
    assert all((op.get("value") or {}).get("evidence") == EVIDENCE for op in fact_ops)
    before = _active_rows(engine)
    result = _land_new(engine)
    assert result.decision == "accept"
    created = _created(engine, before)
    assert len(created) == 1
    _assert_facts_keep_evidence(created[0])
    _assert_statement_quote_keeps_evidence(engine, created[0]["id"])


def test_ab_a_same_and_nloop_facts_evidence_to_statements(tmp_path, monkeypatch):
    """AB-A-EV-04: same 的 facts add，以及 N-loop 中的 new，quote 契约与 EV-02 一致。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    same = engine.apply_integrate(
        SAME_FACT_ADD,
        candidates=["owner"],
        corpus=CORPUS,
        store=CrystalStore(engine.db()),
        crystal_id="owner",
        holder="ab-a-same-facts",
    )
    assert same.decision == "accept"
    owner = engine.db().execute("SELECT * FROM crystallizations WHERE id='owner'").fetchone()
    _assert_facts_keep_evidence(owner)
    _assert_statement_quote_keeps_evidence(engine, "owner")

    _seed_named(engine, "c-yunlan", "云岚甲")
    before = _active_rows(engine)
    seen = {}

    def spy_integrate(_text, _kind="", **kw):
        name = (kw.get("draft_crystal") or {}).get("name")
        if name == "云岚甲":
            return dict(SAME_yunlan)
        return dict(NEW_ACCEPT)

    orig_apply = engine.apply_integrate

    def spy_apply(*args, **kwargs):
        result = orig_apply(*args, **kwargs)
        seen.setdefault("results", []).append(result)
        return result

    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: _draft_bundle(yunlan_DRAFT, NEW_DRAFT))
    monkeypatch.setattr(engine, "_llm_integrate", spy_integrate)
    monkeypatch.setattr(engine, "apply_integrate", spy_apply)
    monkeypatch.setattr(engine, "_llm_extract", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("extract")))
    _queue_user(engine, "src-ab-a-ev04")
    assert engine.pump_one() == "src-ab-a-ev04"
    assert _queue_row(engine, "src-ab-a-ev04")["state"] == "done"
    created = [row for row in _created(engine, before) if row["name"] == "北窗讲师丙"]
    assert len(created) == 1
    _assert_facts_keep_evidence(created[0])
    _assert_statement_quote_keeps_evidence(engine, created[0]["id"])
    new_results = [item for item in seen.get("results", []) if getattr(item, "identity", None) == "new"]
    assert new_results
    assert all(item.decision == "accept" and item.matched_id == created[0]["id"] for item in new_results)


# --- AB-A-ID ---


def test_ab_a_new_land_fills_gate_result_matched_id(tmp_path, monkeypatch):
    """AB-A-ID-01: land 成功后 GateResult.matched_id = 新 crystal_id。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    result = _land_new(engine)
    created = _created(engine, before)
    assert result.decision == "accept"
    assert result.identity == "new"
    assert result.matched_id
    assert len(created) == 1
    assert result.matched_id == created[0]["id"]
    if isinstance(result.payload, dict) and result.payload.get("matched_id"):
        assert result.payload["matched_id"] == created[0]["id"]


def test_ab_a_pump_or_route_observes_new_crystal_id(tmp_path, monkeypatch):
    """AB-A-ID-02: 泵/路由可观测到新 id，禁空 matched_id 静默。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    seen = {}
    orig_route = engine._route_distill_payload

    def spy_route(*args, **kwargs):
        out = orig_route(*args, **kwargs)
        seen["result"] = out
        return out

    monkeypatch.setattr(
        engine,
        "_with_approved_draft",
        lambda *_a, **_k: {
            "draft_crystal": NEW_DRAFT,
            "library_candidates": [{"id": "owner", "name": "记忆主人"}],
        },
    )
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: dict(NEW_ACCEPT))
    monkeypatch.setattr(engine, "_route_distill_payload", spy_route)
    _queue_user(engine, "src-ab-a-id02")
    assert engine.pump_one() == "src-ab-a-id02"
    assert _queue_row(engine, "src-ab-a-id02")["state"] == "done"
    created = _created(engine, before)
    assert len(created) == 1
    routed = seen["result"]
    assert not isinstance(routed, (list, tuple))
    assert routed.decision == "accept"
    assert routed.matched_id == created[0]["id"]
    events = CrystalStore(engine.db()).events(routed.matched_id)
    assert any(item["kind"] == "promote_new" for item in events)


def test_ab_a_empty_matched_id_after_successful_new_is_fail(tmp_path, monkeypatch):
    """AB-A-ID-03: 成功 new 却空 matched_id 不得静默；泵不得假 done。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    result = _land_new(engine)
    created = _created(engine, before)
    assert result.decision == "accept"
    assert result.matched_id == created[0]["id"]

    hollow = GateResult(decision="accept", identity="new", matched_id=None, payload=dict(NEW_ACCEPT))
    with pytest.raises(RuntimeError, match="matched_id"):
        engine._raise_if_new_land_unwritten(hollow)

    orig_apply = engine.apply_integrate

    def strip_id(*args, **kwargs):
        landed = orig_apply(*args, **kwargs)
        landed.matched_id = None
        if isinstance(landed.payload, dict):
            landed.payload = dict(landed.payload)
            landed.payload["matched_id"] = None
        return landed

    monkeypatch.setattr(engine, "apply_integrate", strip_id)
    monkeypatch.setattr(
        engine,
        "_with_approved_draft",
        lambda *_a, **_k: {
            "draft_crystal": NEW_DRAFT,
            "library_candidates": [{"id": "owner", "name": "记忆主人"}],
        },
    )
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: dict(NEW_ACCEPT))
    _queue_user(engine, "src-ab-a-id03")
    assert engine.pump_one() == "src-ab-a-id03"
    queued = _queue_row(engine, "src-ab-a-id03")
    assert queued["state"] != "done"
    assert queued["last_error"]
    assert "matched_id" in (queued["last_error"] or "")


def test_ab_a_land_fail_not_accept_empty_id_ok(tmp_path, monkeypatch):
    """AB-A-ID-04: land 失败不得 accept；此时 matched_id 空可接受。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    missing = _land_new(engine, draft={}, holder="ab-a-id04-empty")
    assert missing.decision == "rejected"
    assert missing.decision != "accept"
    assert not missing.matched_id
    assert any(item.code == "new_land_missing_draft" for item in missing.issues)
    nameless = _land_new(
        engine,
        draft={"type": "人物", "facts": NEW_DRAFT["facts"]},
        holder="ab-a-id04-nameless",
    )
    assert nameless.decision == "rejected"
    assert not nameless.matched_id
    assert _ids(_active_rows(engine)) == _ids(before)


# --- AB-A-WS / NEG / PATH / PROMPT ---


def test_ab_a_write_switch_off_new_deferred_no_write(tmp_path, monkeypatch, write_switch_off):
    """AB-A-WS-01: 写开关关 → deferred，零新行，禁假 accept。"""
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    result = _land_new(engine, holder="ab-a-ws")
    assert result.decision == "deferred"
    assert result.decision != "accept"
    assert any(item.code == "new_land_write_switch_off" for item in result.issues)
    assert _ids(_active_rows(engine)) == _ids(before)
    assert all(row["name"] != "北窗讲师丙" for row in _active_rows(engine))


def test_ab_a_missing_draft_rejected(tmp_path, monkeypatch):
    """AB-A-NEG-01: 缺 sidecar / 无名 draft → rejected，零落库。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    missing = engine.apply_integrate(
        dict(NEW_ACCEPT),
        candidates=["owner"],
        corpus=CORPUS,
        store=CrystalStore(engine.db()),
        holder="ab-a-neg-noside",
    )
    assert missing.decision == "rejected"
    assert any(item.code == "new_land_missing_draft" for item in missing.issues)
    nameless = _land_new(
        engine,
        draft={"type": "人物", "facts": NEW_DRAFT["facts"]},
        holder="ab-a-neg-noname",
    )
    assert nameless.decision == "rejected"
    assert any(item.code == "new_land_missing_draft" for item in nameless.issues)
    assert _ids(_active_rows(engine)) == _ids(before)


def test_ab_a_empty_evidence_claim_still_lands(tmp_path, monkeypatch):
    """AB-A-NEG-02: claim 非空、evidence 空允许落库；不得拿来粉饰 EV-02。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    result = _land_new(engine, draft=EMPTY_EVIDENCE_DRAFT, holder="ab-a-neg-empty-ev")
    assert result.decision == "accept"
    created = _created(engine, before)
    assert len(created) == 1
    thin = hydrate(created[0])
    assert any(fact.claim == "只有主张没有出处" for fact in thin.facts)
    rows = _fact_statements(engine, created[0]["id"], "只有主张没有出处")
    assert rows
    assert all((row["text"] or "") == "只有主张没有出处" for row in rows)


def test_ab_a_negative_path_inventory_present():
    """AB-A-NEG-03: 负路径测网必须存在，禁止只有 happy-path。"""
    names = {name for name in globals() if name.startswith("test_ab_a_")}
    missing = REQUIRED_NEG_TESTS - names
    assert not missing, f"negative-path tests missing: {sorted(missing)}"


def test_ab_a_single_draft_equals_pr79_plus_residue_gates(tmp_path, monkeypatch):
    """AB-A-PATH-01: 单稿 ≡ #79，并带本刀 evidence/id 契约。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    new = _land_new(engine, holder="ab-a-path-new")
    created = _created(engine, before)
    assert new.decision == "accept"
    assert new.identity == "new"
    assert new.matched_id == created[0]["id"]
    _assert_facts_keep_evidence(created[0])
    _assert_statement_quote_keeps_evidence(engine, created[0]["id"])
    assert json.loads(created[0]["summary_json"] or "{}") == {}
    assert json.loads(created[0]["chapters_json"] or "{}") == {}

    owner_before = engine.db().execute(
        "SELECT revision, body_md FROM crystallizations WHERE id='owner'"
    ).fetchone()
    maybe = engine.apply_integrate(
        MAYBE_ACCEPT,
        candidates=["owner"],
        corpus=CORPUS,
        store=CrystalStore(engine.db()),
        crystal_id="owner",
        draft_crystal=NEW_DRAFT,
        holder="ab-a-path-maybe",
    )
    assert maybe.decision == "deferred"
    assert maybe.identity == "maybe"
    owner_mid = engine.db().execute(
        "SELECT revision, body_md FROM crystallizations WHERE id='owner'"
    ).fetchone()
    assert owner_mid["revision"] == owner_before["revision"]
    assert owner_mid["body_md"] == owner_before["body_md"]
    assert len(_created(engine, before)) == 1

    same = engine.apply_integrate(
        SAME_BODY,
        candidates=["owner"],
        corpus=CORPUS,
        store=CrystalStore(engine.db()),
        crystal_id="owner",
        draft_crystal=NEW_DRAFT,
        holder="ab-a-path-same",
    )
    assert same.decision == "accept"
    assert same.identity == "same"
    assert same.matched_id == "owner"
    owner_after = engine.db().execute(
        "SELECT revision, body_md FROM crystallizations WHERE id='owner'"
    ).fetchone()
    assert int(owner_after["revision"] or 0) > int(owner_before["revision"] or 0)
    assert CORPUS in (owner_after["body_md"] or "")
    assert all(row["name"] != "北窗讲师丙" or row["id"] == created[0]["id"] for row in _active_rows(engine))

    failed = _land_new(engine, draft={}, holder="ab-a-path-fail")
    assert failed.decision != "accept"


def test_ab_a_nloop_new_keeps_evidence_and_fills_id(tmp_path, monkeypatch):
    """AB-A-PATH-02: N-loop 每枚 new 的 evidence/id 成立；失败枚不 continue 蒸发。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    before = _active_rows(engine)
    seen = {}

    def spy_integrate(_text, _kind="", **kw):
        name = (kw.get("draft_crystal") or {}).get("name")
        if name == "云岚甲":
            return dict(SAME_yunlan)
        return dict(NEW_ACCEPT)

    orig_apply = engine.apply_integrate

    def spy_apply(*args, **kwargs):
        result = orig_apply(*args, **kwargs)
        seen.setdefault("results", []).append(result)
        return result

    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: _draft_bundle(yunlan_DRAFT, NEW_DRAFT))
    monkeypatch.setattr(engine, "_llm_integrate", spy_integrate)
    monkeypatch.setattr(engine, "apply_integrate", spy_apply)
    monkeypatch.setattr(engine, "_llm_extract", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("extract")))
    _queue_user(engine, "src-ab-a-path02")
    assert engine.pump_one() == "src-ab-a-path02"
    assert _queue_row(engine, "src-ab-a-path02")["state"] == "done"
    created = [row for row in _created(engine, before) if row["name"] == "北窗讲师丙"]
    assert len(created) == 1
    _assert_facts_keep_evidence(created[0])
    _assert_statement_quote_keeps_evidence(engine, created[0]["id"])
    new_results = [item for item in seen.get("results", []) if getattr(item, "identity", None) == "new"]
    assert len(new_results) == 1
    assert new_results[0].matched_id == created[0]["id"]

    extract_calls = []
    apply_names = []

    def one_none(_text, _kind="", **kw):
        name = (kw.get("draft_crystal") or {}).get("name")
        if name == "云岚甲":
            return dict(SAME_yunlan)
        return None

    def track_apply(*args, **kwargs):
        apply_names.append((kwargs.get("draft_crystal") or {}).get("name"))
        return orig_apply(*args, **kwargs)

    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: _draft_bundle(yunlan_DRAFT, SCHOOL_DRAFT))
    monkeypatch.setattr(engine, "_llm_integrate", one_none)
    monkeypatch.setattr(engine, "apply_integrate", track_apply)
    monkeypatch.setattr(
        engine,
        "_llm_extract",
        lambda *_a, **_k: extract_calls.append(1) or {"payload_kind": "extract", "items": []},
    )
    _queue_user(engine, "src-ab-a-path02-none")
    assert engine.pump_one() == "src-ab-a-path02-none"
    queued = _queue_row(engine, "src-ab-a-path02-none")
    assert queued["state"] != "done"
    assert queued["last_error"]
    assert apply_names == ["云岚甲"]
    assert extract_calls == []


def test_ab_a_docs_prompts_git_diff_empty():
    """AB-A-PROMPT-01: 相对 origin/release/2.0 的真 git diff docs/prompts 必须空。"""
    repo = Path(__file__).resolve().parents[1]
    env_base = "ROGMEM_PROMPTS_BASE"
    # 禁旁路冒充：不用环境变量改基线。
    preferred = ("origin/release/2.0", "release/2.0")
    base = None
    for ref in preferred:
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", ref],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            base = ref
            break
    assert base, f"cannot resolve prompts base from {preferred}"
    merge = subprocess.run(
        ["git", "merge-base", base, "HEAD"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 0, merge.stderr
    ancestor = merge.stdout.strip()
    assert ancestor
    diff = subprocess.run(
        ["git", "diff", f"{ancestor}...HEAD", "--", "docs/prompts"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    assert diff.returncode == 0, diff.stderr
    assert diff.stdout == "", f"docs/prompts git diff not empty vs {base}:\n{diff.stdout}"
    worktree = subprocess.run(
        ["git", "diff", ancestor, "--", "docs/prompts"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    assert worktree.returncode == 0, worktree.stderr
    assert worktree.stdout == "", f"docs/prompts worktree diff not empty:\n{worktree.stdout}"
    assert env_base not in (diff.args or [])
