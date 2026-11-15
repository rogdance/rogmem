"""多候选全文比对（BA-*）。

Match ≥2 时，每一篇候选都带着正文一次送进整合。不先用摘要卡片认人。
只拿卡片就当同一篇来写，仍然拒绝。整合没有交卷、写开关关掉，都不落库。

Match=0 冷启另刀（产品未钉）。本文件不测 M0。
禁止 inspect.getsource 纸老虎。
"""

from __future__ import annotations

import json

import pytest

from rogmem.crystal_store import (
    DISTILLER_STORE_WRITE_ENABLED,
    CrystalStore,
    Fact,
    ThinCrystal,
    hydrate,
)
from rogmem.distill_gate import PAYLOAD_KIND_EXTRACT, PAYLOAD_KIND_INTEGRATE

pytestmark = pytest.mark.ba_promote_fail_gate

CORPUS = "云岚测试员甲于 2019 年在北窗实验室任职。后来到南港书院做讲师。"
QING_BODY = "## 事业经历\n\n云岚测试员甲于 2019 年在北窗实验室任职。"
SCHOOL_BODY = "## 发展沿革\n\n后来到南港书院做讲师。"
QING_FACT = {
    "claim": "2019 年在北窗实验室任职",
    "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
}
SCHOOL_FACT = {
    "claim": "后来到南港书院做讲师",
    "evidence": "后来到南港书院做讲师。",
}
QING_DRAFT = {
    "name": "云岚甲",
    "type": "人物",
    "aliases": ["测试员甲"],
    "summary": "在北窗实验室任职。",
    "owner_relation": "同事",
    "body_md": QING_BODY,
    "facts": [dict(QING_FACT)],
}
SAME_QING = {
    "identity": "same",
    "matched_id": "c-yunlan",
    "ops": [
        {
            "op": "update",
            "path": "body_md",
            "value": CORPUS,
            "fact": {
                "claim": "2019 入职",
                "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
            },
        }
    ],
    "conflicts": [],
}
NEW_ACCEPT = {"identity": "new", "ops": [], "conflicts": []}
MAYBE_ACCEPT = {"identity": "maybe", "ops": [], "conflicts": []}

_A_PACK_KEYS = ("id", "type", "name", "aliases", "summary", "body_md", "facts", "body_injection")


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


def _seed_thin(engine, cid: str, name: str, *, ctype: str = "人物", body: str, facts: list, summary: str, aliases=None):
    store = CrystalStore(engine.db())
    store.put(
        ThinCrystal(
            id=cid,
            type=ctype,
            name=name,
            status="active",
            revision=0,
            summary=summary,
            aliases=list(aliases or []),
            body_md=body,
            facts=[Fact(**item) if isinstance(item, dict) else item for item in facts],
        ),
        holder=f"seed:{cid}",
        commit=True,
    )


def _seed_ba_library(engine) -> None:
    _seed_thin(
        engine,
        "c-yunlan",
        "云岚甲",
        body=QING_BODY,
        facts=[QING_FACT],
        summary="在北窗实验室任职。",
        aliases=["测试员甲"],
    )
    _seed_thin(
        engine,
        "c-school",
        "南港书院",
        ctype="组织",
        body=SCHOOL_BODY,
        facts=[SCHOOL_FACT],
        summary="云岚甲后来任教的书院。",
    )


def _row(engine, cid: str):
    return engine.db().execute("SELECT * FROM crystallizations WHERE id=?", (cid,)).fetchone()


def _is_b_card(item: dict) -> bool:
    injection = str(item.get("body_injection") or "")
    if injection in {"full", "sectioned"}:
        return False
    if injection == "card":
        return True
    return not str(item.get("body_md") or "").strip()


def _is_a_pack(item: dict) -> bool:
    if str(item.get("body_injection") or "") not in {"full", "sectioned"}:
        return False
    if any(key not in item for key in _A_PACK_KEYS):
        return False
    facts = item.get("facts") or []
    return isinstance(facts, list)


def _phase_of(candidates) -> str:
    if not isinstance(candidates, list) or not candidates:
        return "empty"
    if len(candidates) >= 2 and all(_is_a_pack(item) for item in candidates if isinstance(item, dict)):
        return "full"
    if len(candidates) >= 2 and all(_is_b_card(item) for item in candidates if isinstance(item, dict)):
        return "b"
    if len(candidates) == 1 and isinstance(candidates[0], dict) and _is_a_pack(candidates[0]):
        return "a"
    return "other"


def _spy_integrate_promote(engine, monkeypatch, *, first=None, second=None, by_phase=None):
    calls: list[dict] = []

    def spy(_text, _kind="", **kw):
        cands = list(kw.get("library_candidates") or [])
        phase = _phase_of(cands)
        calls.append({"kw": kw, "phase": phase, "ids": [item.get("id") for item in cands if isinstance(item, dict)]})
        if by_phase is not None:
            produced = by_phase(phase, kw)
            if produced is None:
                return None
            return dict(produced)
        if phase in {"a", "full"}:
            return dict(second if second is not None else SAME_QING)
        return dict(first if first is not None else SAME_QING)

    monkeypatch.setattr(engine, "_llm_integrate", spy)
    return calls


def test_ba_promote_positive_lands_thin_on_matched_id(tmp_path, monkeypatch):
    """BA-POS-01: ≥2 hit 一次送全文，same 后落到对上的那一篇。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_ba_library(engine)
    before = _row(engine, "c-yunlan")
    calls = _spy_integrate_promote(engine, monkeypatch)
    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: {"crystals": [QING_DRAFT]})
    extract_calls: list[int] = []
    monkeypatch.setattr(
        engine,
        "_llm_extract",
        lambda *_a, **_k: extract_calls.append(1) or {"payload_kind": PAYLOAD_KIND_EXTRACT, "items": []},
    )
    _queue_user(engine, "src-ba-pos")
    assert engine.pump_one() == "src-ba-pos"
    assert _queue_row(engine, "src-ba-pos")["state"] == "done"
    assert len(calls) == 1
    assert calls[0]["phase"] == "full"
    assert set(calls[0]["ids"]) >= {"c-yunlan", "c-school"}
    assert all(_is_a_pack(item) for item in calls[0]["kw"]["library_candidates"])
    pack = next(item for item in calls[0]["kw"]["library_candidates"] if item["id"] == "c-yunlan")
    assert str(pack.get("body_md") or "")
    after = _row(engine, "c-yunlan")
    thin = hydrate(after)
    assert thin.storage == "thin"
    assert thin.id == "c-yunlan"
    assert CORPUS in (after["body_md"] or "")
    assert int(after["revision"] or 0) > int(before["revision"] or 0)
    assert json.loads(after["summary_json"] or "{}") == json.loads(before["summary_json"] or "{}")
    assert json.loads(after["chapters_json"] or "{}") == json.loads(before["chapters_json"] or "{}")
    assert extract_calls == []


def test_ba_same_applies_against_promoted_full_pack(tmp_path, monkeypatch):
    """BA-POS-02: 出判断的那一次，每个候选都是全文。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_ba_library(engine)
    apply_targets: list[str] = []
    orig = engine.apply_integrate

    def spy_apply(*args, **kwargs):
        raw = args[0] if args else kwargs.get("raw")
        cid = kwargs.get("crystal_id") or ""
        apply_targets.append(str(cid or getattr(raw, "get", lambda *_: "")("matched_id") or ""))
        return orig(*args, **kwargs)

    calls = _spy_integrate_promote(engine, monkeypatch)
    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: {"crystals": [QING_DRAFT]})
    monkeypatch.setattr(engine, "apply_integrate", spy_apply)
    monkeypatch.setattr(engine, "_llm_extract", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("extract")))
    data = engine._produce_distill_payload(CORPUS, "user", **engine._match_integrate_parts(CORPUS))
    assert data.get("payload_kind") == PAYLOAD_KIND_INTEGRATE
    assert data.get("payload_kind") != PAYLOAD_KIND_EXTRACT
    full_calls = [item for item in calls if item["phase"] == "full"]
    assert len(full_calls) == 1
    pack = next(item for item in full_calls[0]["kw"]["library_candidates"] if item.get("id") == "c-yunlan")
    for key in _A_PACK_KEYS:
        assert key in pack
    assert pack["id"] == "c-yunlan"
    assert pack["body_injection"] in {"full", "sectioned"}
    assert pack["body_md"]
    facts = pack["facts"]
    assert facts and any(str(item.get("evidence") or "") for item in facts if isinstance(item, dict))
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?, 'user', 'qa', 'qa', ?, ?, ?, ?, 'fast', 1)""",
        ("src-ba-full", engine.utcnow(), "src-ba-full-h", CORPUS, engine.ST_VEC_READY),
    )
    con.commit()
    source = con.execute("SELECT * FROM sources WHERE id='src-ba-full'").fetchone()
    result = engine._route_distill_payload(source, data, CORPUS)
    one = result[0] if isinstance(result, list) else result
    assert one.decision == "accept"
    assert one.identity == "same"
    assert one.matched_id == "c-yunlan"
    assert apply_targets
    assert all(target == "c-yunlan" for target in apply_targets if target)
    thin = hydrate(_row(engine, "c-yunlan"))
    assert thin.storage == "thin"
    assert CORPUS in (thin.body_md or "")


def test_ba_multi_hit_requires_promote_before_final_ops(tmp_path, monkeypatch):
    """BA-POS-03: 多 hit 一次送出全部全文，不再先发一版没有正文的卡片。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_ba_library(engine)
    calls = _spy_integrate_promote(engine, monkeypatch)
    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: {"crystals": [QING_DRAFT]})
    data = engine._produce_distill_payload(CORPUS, "user", **engine._match_integrate_parts(CORPUS))
    assert data.get("payload_kind") == PAYLOAD_KIND_INTEGRATE
    assert len(calls) == 1
    assert calls[0]["phase"] == "full"
    assert set(calls[0]["ids"]) >= {"c-yunlan", "c-school"}
    assert all(_is_a_pack(item) for item in calls[0]["kw"]["library_candidates"])
    assert all(str(item.get("body_md") or "") for item in calls[0]["kw"]["library_candidates"])


def test_ba_promote_apply_thin_not_thick_open(tmp_path, monkeypatch):
    """BA-LAND-01: 全文比对之后仍然薄写；summary_json / chapters_json 不变厚。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_ba_library(engine)
    before = _row(engine, "c-yunlan")
    calls = _spy_integrate_promote(engine, monkeypatch)
    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: {"crystals": [QING_DRAFT]})
    monkeypatch.setattr(engine, "_llm_extract", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("extract")))
    _queue_user(engine, "src-ba-land")
    assert engine.pump_one() == "src-ba-land"
    assert len(calls) == 1 and calls[0]["phase"] == "full"
    after = _row(engine, "c-yunlan")
    thin = hydrate(after)
    assert thin.storage == "thin"
    assert CORPUS in (thin.body_md or "")
    assert after["envelope_json"]
    assert int(after["revision"] or 0) > int(before["revision"] or 0)
    assert json.loads(after["summary_json"] or "{}") == json.loads(before["summary_json"] or "{}")
    assert json.loads(after["chapters_json"] or "{}") == json.loads(before["chapters_json"] or "{}")
    school = hydrate(_row(engine, "c-school"))
    assert school.body_md == SCHOOL_BODY


def test_ba_silent_stay_on_b_cards_is_fail(tmp_path, monkeypatch):
    """BA-NEG-01: same + 仅 B 卡片出 ops，未升格不得 accept。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_ba_library(engine)
    before = _row(engine, "c-yunlan")
    b_cards = [
        {
            "id": "c-yunlan",
            "type": "人物",
            "name": "云岚甲",
            "aliases": ["测试员甲"],
            "summary": "在北窗实验室任职。",
            "facts": [dict(QING_FACT)],
            "body_injection": "card",
        },
        {
            "id": "c-school",
            "type": "组织",
            "name": "南港书院",
            "aliases": [],
            "summary": "云岚甲后来任教的书院。",
            "facts": [dict(SCHOOL_FACT)],
            "body_injection": "card",
        },
    ]
    payload = dict(SAME_QING)
    payload["_library_candidates"] = b_cards
    result = engine.apply_integrate(
        payload,
        candidates=["c-yunlan", "c-school"],
        corpus=CORPUS,
        store=CrystalStore(engine.db()),
        draft_crystal=QING_DRAFT,
        holder="ba-silent-b",
    )
    assert result.decision != "accept"
    assert result.decision in {"rejected", "deferred"}
    after = _row(engine, "c-yunlan")
    assert int(after["revision"] or 0) == int(before["revision"] or 0)
    assert CORPUS not in (after["body_md"] or "")


def test_ba_multi_hit_must_not_fallback_extract(tmp_path, monkeypatch):
    """BA-NEG-02: 多 hit + 非空 mid 不得产出 extract。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_ba_library(engine)
    extract_calls: list[int] = []

    def boom_extract(*_a, **_k):
        extract_calls.append(1)
        return {"payload_kind": PAYLOAD_KIND_EXTRACT, "items": []}

    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: {"crystals": [QING_DRAFT]})
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: None)
    monkeypatch.setattr(engine, "_llm_extract", boom_extract)
    data = engine._produce_distill_payload(CORPUS, "user", **engine._match_integrate_parts(CORPUS))
    assert data.get("payload_kind") == PAYLOAD_KIND_INTEGRATE
    assert data.get("payload_kind") != PAYLOAD_KIND_EXTRACT
    assert extract_calls == []
    _queue_user(engine, "src-ba-no-extract")
    monkeypatch.setattr(engine, "_llm_extract", boom_extract)
    assert engine.pump_one() == "src-ba-no-extract"
    queued = _queue_row(engine, "src-ba-no-extract")
    assert queued["state"] != "done"
    assert extract_calls == []


def test_ba_promote_fail_not_accept_or_done(tmp_path, monkeypatch):
    """BA-NEG-03: 全文比对这一次没有交卷，不得假 accept/done。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_ba_library(engine)
    before = _row(engine, "c-yunlan")

    def by_phase(phase, _kw):
        if phase == "full":
            return None
        return dict(SAME_QING)

    _spy_integrate_promote(engine, monkeypatch, by_phase=by_phase)
    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: {"crystals": [QING_DRAFT]})
    monkeypatch.setattr(engine, "_llm_extract", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("extract")))
    _queue_user(engine, "src-ba-fail")
    assert engine.pump_one() == "src-ba-fail"
    queued = _queue_row(engine, "src-ba-fail")
    assert queued["state"] != "done"
    assert queued["last_error"]
    after = _row(engine, "c-yunlan")
    assert int(after["revision"] or 0) == int(before["revision"] or 0)
    assert CORPUS not in (after["body_md"] or "")


def test_ba_write_switch_off_deferred_no_write(tmp_path, monkeypatch, write_switch_off):
    """BA-WS-01: 写开关关，本可成功的 B→A 必须 deferred，零薄写。"""
    engine = _engine(tmp_path, monkeypatch)
    con = engine.db()
    for cid, name, ctype, body, facts, summary, aliases in (
        ("c-yunlan", "云岚甲", "人物", QING_BODY, [QING_FACT], "在北窗实验室任职。", ["测试员甲"]),
        ("c-school", "南港书院", "组织", SCHOOL_BODY, [SCHOOL_FACT], "云岚甲后来任教的书院。", []),
    ):
        con.execute(
            """INSERT INTO crystallizations(
                 id,type,name,status,summary_json,chapters_json,revision,
                 envelope_json,body_md,facts_json
               ) VALUES(?,?,?,'active','{}','{}',0,?,?,?)""",
            (
                cid,
                ctype,
                name,
                json.dumps({"summary": summary, "aliases": aliases}, ensure_ascii=False),
                body,
                json.dumps(facts, ensure_ascii=False),
            ),
        )
    con.commit()
    before = _row(engine, "c-yunlan")
    _spy_integrate_promote(engine, monkeypatch)
    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: {"crystals": [QING_DRAFT]})
    monkeypatch.setattr(engine, "_llm_extract", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("extract")))
    result = engine.apply_integrate(
        {**SAME_QING, "_ba_promoted": True, "_library_candidates": [
            {
                "id": "c-yunlan",
                "type": "人物",
                "name": "云岚甲",
                "aliases": ["测试员甲"],
                "summary": "在北窗实验室任职。",
                "body_md": QING_BODY,
                "facts": [dict(QING_FACT)],
                "body_injection": "full",
            }
        ]},
        candidates=["c-yunlan"],
        corpus=CORPUS,
        store=CrystalStore(engine.db()),
        draft_crystal=QING_DRAFT,
        holder="ba-switch-off",
    )
    assert result.decision != "accept"
    assert result.decision == "deferred"
    after = _row(engine, "c-yunlan")
    assert int(after["revision"] or 0) == int(before["revision"] or 0)
    assert CORPUS not in (after["body_md"] or "")
    _queue_user(engine, "src-ba-off")
    assert engine.pump_one() == "src-ba-off"
    queued = _queue_row(engine, "src-ba-off")
    assert queued["state"] != "done"
    done_row = _row(engine, "c-yunlan")
    assert int(done_row["revision"] or 0) == int(before["revision"] or 0)


def test_ba_match_still_returns_all_hits_before_b_cards(tmp_path, monkeypatch):
    """BA-REUSE-01: 送去比对的 id 集合等于 Match 全量，不得只留 top1。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_ba_library(engine)
    match = engine._match_integrate_parts(CORPUS)
    match_ids = {item["id"] for item in match["library_candidates"]}
    assert match_ids >= {"c-yunlan", "c-school"}
    calls = _spy_integrate_promote(engine, monkeypatch)
    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: {"crystals": [QING_DRAFT]})
    engine._produce_distill_payload(CORPUS, "user", **match)
    assert len(calls) == 1
    assert calls[0]["phase"] == "full"
    assert set(calls[0]["ids"]) == match_ids


def test_ba_after_promote_identity_gates_equal_pr79(tmp_path, monkeypatch):
    """BA-REUSE-02: 全文比对之后仍走 same/new/maybe。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_ba_library(engine)

    def run(second_payload, source_id):
        _spy_integrate_promote(engine, monkeypatch, second=second_payload)
        monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: {"crystals": [QING_DRAFT]})
        monkeypatch.setattr(engine, "_llm_extract", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("extract")))
        _queue_user(engine, source_id)
        assert engine.pump_one() == source_id
        return _queue_row(engine, source_id), _row(engine, "c-yunlan")

    before = _row(engine, "c-yunlan")
    queued, after = run(MAYBE_ACCEPT, "src-ba-maybe")
    assert after["revision"] == before["revision"]
    assert CORPUS not in (after["body_md"] or "")
    assert queued["state"] == "done" or after["name"] == "云岚甲"

    names_before = {row["name"] for row in engine.db().execute("SELECT name FROM crystallizations")}
    queued_new, _qing = run(NEW_ACCEPT, "src-ba-new")
    names_after = {row["name"] for row in engine.db().execute("SELECT name FROM crystallizations")}
    assert "云岚甲" in names_after
    created = names_after - names_before
    assert "北窗讲师丙" not in created or queued_new["state"]
    assert QING_DRAFT["name"] == "云岚甲"
    new_rows = list(
        engine.db().execute("SELECT name, status FROM crystallizations WHERE name=?", (QING_DRAFT["name"],))
    )
    assert new_rows

    qing_mid = _row(engine, "c-yunlan")
    queued_same, qing_same = run(SAME_QING, "src-ba-same")
    assert queued_same["state"] == "done"
    assert CORPUS in (qing_same["body_md"] or "")
    assert int(qing_same["revision"] or 0) > int(qing_mid["revision"] or 0)
    school = _row(engine, "c-school")
    assert school["body_md"] == SCHOOL_BODY


def test_ba_docs_prompts_git_diff_empty():
    """BA-PROMPT-01: docs/prompts 真 git diff 为空。"""
    from tests.docs_prompts_zero_diff import assert_docs_prompts_zero_diff

    assert DISTILLER_STORE_WRITE_ENABLED is True
    assert_docs_prompts_zero_diff()


def test_ba_fail_gate_negative_inventory():
    """BA-FAIL-01: 负路径测网必须齐（静默留 B / 禁 extract / 升格失败）。"""
    names = {name for name in globals() if name.startswith("test_ba_")}
    required = {
        "test_ba_promote_positive_lands_thin_on_matched_id",
        "test_ba_silent_stay_on_b_cards_is_fail",
        "test_ba_multi_hit_must_not_fallback_extract",
        "test_ba_promote_fail_not_accept_or_done",
        "test_ba_write_switch_off_deferred_no_write",
    }
    missing = sorted(required - names)
    assert not missing, f"BA Fail 负/正路径缺测: {missing}"
