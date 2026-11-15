"""先成后败（PART-01..03 / NL-FAIL-04）。

N-loop / batch：后一条 rejected / deferred / Integrate 失败时，
这一批已经写下的一并撤回。重试整批再写。

锁契约，不改 Draft/Integrate prompts，不碰 wipe，不宣称可上线。
断言 GateResult / 库读回 / 泵 queue；禁止 sniff 源码字符串冒充绿。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rogmem.crystal_store import DISTILLER_STORE_WRITE_ENABLED
from rogmem.distill_gate import PAYLOAD_KIND_EXTRACT, PAYLOAD_KIND_INTEGRATE

pytestmark = pytest.mark.partial_compensate_fail_gate

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
SCHOOL_DRAFT = {
    "name": "南港书院",
    "type": "组织",
    "aliases": [],
    "summary": "云岚甲后来任教的书院。",
    "owner_relation": "任职机构",
    "body_md": SCHOOL_BODY,
    "facts": [dict(SCHOOL_FACT)],
}
NEW_DRAFT = {
    "name": "北窗讲师丙",
    "type": "人物",
    "aliases": ["讲师丙"],
    "summary": "在北窗实验室做讲师。",
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
            "fact": {"claim": "2019 入职", "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。"},
        }
    ],
    "conflicts": [],
}
SAME_SCHOOL = {
    "identity": "same",
    "matched_id": "c-school",
    "ops": [
        {
            "op": "update",
            "path": "body_md",
            "value": CORPUS,
            "fact": {"claim": "南港任教", "evidence": "后来到南港书院做讲师。"},
        }
    ],
    "conflicts": [],
}
NEW_ACCEPT = {"identity": "new", "ops": [], "conflicts": []}
ILLEGAL_NEW = {"identity": "new", "ops": [{"op": "add", "path": "facts"}], "conflicts": []}
RETRY_SCHOOL = {
    "identity": "same",
    "matched_id": "c-school",
    "ops": [
        {
            "op": "update",
            "path": "body_md",
            "value": CORPUS,
            "covering": True,
            "fact": {"claim": "南港任教", "evidence": "后来到南港书院做讲师。"},
        }
    ],
    "conflicts": [],
}

REQUIRED_TESTS = {
    "test_part01_per_mid_results_observable",
    "test_part02_first_success_then_fail_persists_and_matches_queue",
    "test_part03_retry_does_not_double_create_accepted_mid",
    "test_fail04_partial_fail_pump_not_done",
    "test_compensate_non_accept_decisions_block_done",
    "test_compensate_deferred_blocks_done",
    "test_partial_fail_never_falls_back_to_extract",
    "test_docs_prompts_git_diff_empty",
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


def _queue_user(engine, source_id: str, body: str = CORPUS) -> None:
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?, 'user', 'qa', 'qa', ?, ?, ?, ?, 'fast', 1)""",
        (source_id, engine.utcnow(), source_id + "-h", body, engine.ST_VEC_READY),
    )
    con.execute("INSERT INTO queue(source_id,lane,state) VALUES(?, 'fast', 'queued')", (source_id,))
    con.commit()


def _insert_source(engine, source_id: str, body: str = CORPUS):
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?, 'user', 'qa', 'qa', ?, ?, ?, ?, 'fast', 1)""",
        (source_id, engine.utcnow(), source_id + "-h", body, engine.ST_VEC_READY),
    )
    con.commit()
    return con.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()


def _queue_row(engine, source_id: str):
    return engine.db().execute(
        "SELECT state, last_error FROM queue WHERE source_id=?", (source_id,)
    ).fetchone()


def _source_row(engine, source_id: str):
    return engine.db().execute(
        "SELECT status, last_error FROM sources WHERE id=?", (source_id,)
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


def _row(engine, cid: str):
    return engine.db().execute("SELECT * FROM crystallizations WHERE id=?", (cid,)).fetchone()


def _seed_named(engine, cid: str, name: str, ctype: str = "人物") -> None:
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json)
           VALUES(?,?,?,'active',?,?)""",
        (cid, ctype, name, "{}", "{}"),
    )
    engine.db().commit()


def _named_active(engine, name: str):
    return [
        row
        for row in _active_rows(engine)
        if row["name"] == name
    ]


def _boom_extract(*_a, **_k):
    raise AssertionError("partial compensate must not fall back to extract")


def _issue_codes(result) -> list[str]:
    return [
        str(getattr(item, "code", "") or "")
        for item in (getattr(result, "issues", None) or [])
        if str(getattr(item, "code", "") or "")
    ]


def _mid_ledger(results) -> list[dict]:
    """Test-side reconcilable view. Must come from GateResult, not source sniff."""
    items = results if isinstance(results, (list, tuple)) else [results]
    ledger = []
    for item in items:
        if item is None:
            ledger.append({"decision": "unwritten", "issue_codes": ["unwritten"], "crystal_id": None})
            continue
        decision = getattr(item, "decision", None)
        crystal_id = str(getattr(item, "matched_id", None) or "").strip() or None
        ledger.append(
            {
                "decision": decision,
                "issue_codes": _issue_codes(item),
                "crystal_id": crystal_id,
            }
        )
    return ledger


def _route_batch(engine, monkeypatch, source_id: str, drafts, spy_integrate, body: str = CORPUS):
    _wire_mixed(engine, monkeypatch, spy_integrate, drafts)
    source = _insert_source(engine, source_id, body)
    data = engine._produce_distill_payload(body, "user", **engine._match_integrate_parts(body))
    return engine._route_distill_payload(source, data, body)


def _wire_mixed(engine, monkeypatch, spy_integrate, drafts=(QING_DRAFT, SCHOOL_DRAFT)):
    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: {"crystals": list(drafts)})
    monkeypatch.setattr(engine, "_llm_integrate", spy_integrate)
    monkeypatch.setattr(engine, "_llm_extract", _boom_extract)


# --- PART-01 逐枚可观测 ---


def test_part01_per_mid_results_observable(tmp_path, monkeypatch):
    """PART-01: 先成后败的逐枚 decision + issue code（+ crystal_id）可对账。

    禁整源假 accept / 单一 accept 掩盖失败枚。
    """
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    _seed_named(engine, "c-school", "南港书院", "组织")

    def spy_integrate(_text, _kind="", **kw):
        name = (kw.get("draft_crystal") or {}).get("name")
        if name == "云岚甲":
            return dict(SAME_QING)
        return dict(ILLEGAL_NEW)

    results = _route_batch(engine, monkeypatch, "src-part01", (QING_DRAFT, NEW_DRAFT), spy_integrate)
    assert isinstance(results, (list, tuple))
    assert len(results) == 2
    ledger = _mid_ledger(results)
    assert ledger[0]["decision"] == "accept"
    assert ledger[0]["crystal_id"]
    assert ledger[1]["decision"] != "accept"
    assert ledger[1]["issue_codes"]
    accept_only = [row for row in ledger if row["decision"] == "accept"]
    failed = [row for row in ledger if row["decision"] != "accept"]
    assert len(accept_only) == 1
    assert len(failed) == 1
    with pytest.raises(RuntimeError, match="batch integrate/land not accepted"):
        engine._raise_if_new_land_unwritten(results)
    assert CORPUS not in (_row(engine, "c-yunlan")["body_md"] or "")
    assert _named_active(engine, "北窗讲师丙") == []


# --- PART-02 先成后败：成功持久 + 失败 non-accept + 对外=库 ---


def test_part02_first_success_then_fail_persists_and_matches_queue(tmp_path, monkeypatch):
    """PART-02: 后一条失败时，前一条也不留；queue 非 done。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    before = _ids(_active_rows(engine))

    def spy_integrate(_text, _kind="", **kw):
        name = (kw.get("draft_crystal") or {}).get("name")
        if name == "云岚甲":
            return dict(SAME_QING)
        return dict(ILLEGAL_NEW)

    _wire_mixed(engine, monkeypatch, spy_integrate, drafts=(QING_DRAFT, NEW_DRAFT))
    _queue_user(engine, "src-part02")
    assert engine.pump_one() == "src-part02"
    queued = _queue_row(engine, "src-part02")
    source = _source_row(engine, "src-part02")
    assert queued["state"] != "done"
    assert queued["last_error"]
    assert source["last_error"]
    qing = _row(engine, "c-yunlan")
    assert CORPUS not in (qing["body_md"] or "")
    assert int(qing["revision"] or 0) == 0
    created = [row for row in _active_rows(engine) if row["id"] not in before]
    assert all(row["name"] != "北窗讲师丙" for row in created)
    assert _named_active(engine, "北窗讲师丙") == []


# --- PART-03 重试幂等：已成功枚不双建 ---


def test_part03_retry_does_not_double_create_accepted_mid(tmp_path, monkeypatch):
    """PART-03: 失败的一批全部撤回；重试成功时每种只建一次。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    waves: list[str] = []

    def spy_integrate(_text, _kind="", **kw):
        name = (kw.get("draft_crystal") or {}).get("name")
        if name == "北窗讲师丙":
            return dict(NEW_ACCEPT)
        if waves.count("fail") < 1:
            waves.append("fail")
            return None
        waves.append("ok")
        return dict(NEW_ACCEPT)

    _wire_mixed(engine, monkeypatch, spy_integrate, drafts=(NEW_DRAFT, SCHOOL_DRAFT))
    _queue_user(engine, "src-part03")
    assert engine.pump_one() == "src-part03"
    assert _queue_row(engine, "src-part03")["state"] != "done"
    assert _named_active(engine, "北窗讲师丙") == []
    assert _named_active(engine, "南港书院") == []

    retried = engine.retry_source("src-part03")
    assert retried.get("ok") is True
    assert engine.pump_one() == "src-part03"
    after = _named_active(engine, "北窗讲师丙")
    assert len(after) == 1
    school = _named_active(engine, "南港书院")
    assert len(school) == 1
    assert school[0]["id"] != after[0]["id"]
    assert _queue_row(engine, "src-part03")["state"] == "done"


# --- FAIL-04 部分失败泵非 done ---


def test_fail04_partial_fail_pump_not_done(tmp_path, monkeypatch):
    """NL-FAIL-04: 一枚成功不得标整源 done；失败枚不得静默丢。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    _seed_named(engine, "c-school", "南港书院", "组织")
    integrate_names: list[str] = []

    def spy_integrate(_text, _kind="", **kw):
        name = (kw.get("draft_crystal") or {}).get("name")
        integrate_names.append(name)
        if name == "云岚甲":
            return dict(SAME_QING)
        return None

    _wire_mixed(engine, monkeypatch, spy_integrate)
    _queue_user(engine, "src-fail04")
    assert engine.pump_one() == "src-fail04"
    queued = _queue_row(engine, "src-fail04")
    assert queued["state"] != "done"
    assert queued["last_error"]
    assert "云岚甲" in integrate_names
    assert "南港书院" in integrate_names
    assert CORPUS not in (_row(engine, "c-yunlan")["body_md"] or "")
    school = _row(engine, "c-school")
    assert CORPUS not in (school["body_md"] or "")
    assert int(school["revision"] or 0) == 0


@pytest.mark.parametrize(
    "fail_raw,expected_decisions",
    [
        (ILLEGAL_NEW, {"rejected"}),
        (None, {"rejected"}),
        (RETRY_SCHOOL, {"retry"}),
    ],
    ids=["rejected", "unwritten", "retry"],
)
def test_compensate_non_accept_decisions_block_done(tmp_path, monkeypatch, fail_raw, expected_decisions):
    """任非 accept（rejected / retry / unwritten）挡住 done，且逐枚可对账。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    _seed_named(engine, "c-school", "南港书院", "组织")

    def spy_integrate(_text, _kind="", **kw):
        name = (kw.get("draft_crystal") or {}).get("name")
        if name == "云岚甲":
            return dict(SAME_QING)
        return None if fail_raw is None else dict(fail_raw)

    results = _route_batch(engine, monkeypatch, "src-decisions", (QING_DRAFT, SCHOOL_DRAFT), spy_integrate)
    ledger = _mid_ledger(results)
    assert ledger[0]["decision"] == "accept"
    assert ledger[1]["decision"] in expected_decisions
    assert ledger[1]["issue_codes"]
    with pytest.raises(RuntimeError, match="batch integrate/land not accepted"):
        engine._raise_if_new_land_unwritten(results)
    _wire_mixed(engine, monkeypatch, spy_integrate)
    _queue_user(engine, "src-decisions-pump")
    assert engine.pump_one() == "src-decisions-pump"
    assert _queue_row(engine, "src-decisions-pump")["state"] != "done"


def test_compensate_deferred_blocks_done(tmp_path, monkeypatch, write_switch_off):
    """deferred（写开关关的 new land）也是非 accept，泵不得 done。"""
    engine = _engine(tmp_path, monkeypatch)
    before = _ids(_active_rows(engine))

    def spy_integrate(_text, _kind="", **kw):
        return dict(NEW_ACCEPT)

    _wire_mixed(engine, monkeypatch, spy_integrate, drafts=(NEW_DRAFT, SCHOOL_DRAFT))
    _queue_user(engine, "src-deferred")
    assert engine.pump_one() == "src-deferred"
    queued = _queue_row(engine, "src-deferred")
    assert queued["state"] != "done"
    assert queued["last_error"]
    created = [row for row in _active_rows(engine) if row["id"] not in before]
    assert created == []
    results = _route_batch(engine, monkeypatch, "src-deferred-route", (NEW_DRAFT, SCHOOL_DRAFT), spy_integrate)
    ledger = _mid_ledger(results)
    assert ledger
    assert all(row["decision"] == "deferred" for row in ledger)
    assert all(row["issue_codes"] for row in ledger)
    with pytest.raises(RuntimeError, match="batch integrate/land not accepted"):
        engine._raise_if_new_land_unwritten(results)


def test_partial_fail_never_falls_back_to_extract(tmp_path, monkeypatch):
    """禁退 extract 假消化：有 mid 时失败仍走 integrate fail-closed。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    extract_calls: list[int] = []
    monkeypatch.setattr(
        engine,
        "_llm_extract",
        lambda *_a, **_k: extract_calls.append(1) or {"payload_kind": PAYLOAD_KIND_EXTRACT, "items": []},
    )
    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: {"crystals": [QING_DRAFT, SCHOOL_DRAFT]})
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: None)
    data = engine._produce_distill_payload(CORPUS, "user", **engine._match_integrate_parts(CORPUS))
    assert data.get("payload_kind") == PAYLOAD_KIND_INTEGRATE
    batch = data.get("_integrate_batch") or []
    assert len(batch) == 2
    assert all(item.get("payload_kind") == PAYLOAD_KIND_INTEGRATE for item in batch)
    _queue_user(engine, "src-no-extract")
    assert engine.pump_one() == "src-no-extract"
    assert _queue_row(engine, "src-no-extract")["state"] != "done"
    assert extract_calls == []


def test_docs_prompts_git_diff_empty():
    """本刀 prompts 零 diff。"""
    from tests.docs_prompts_zero_diff import assert_docs_prompts_zero_diff

    assert DISTILLER_STORE_WRITE_ENABLED is True
    assert_docs_prompts_zero_diff()


def test_voice_not_ship_ready():
    """旁路刀：≠可上线；wipe 仍卡；禁称闭环已完。"""
    ship = "可" + "上线"
    done = "闭环" + "完"
    here = Path(__file__).read_text(encoding="utf-8")
    assert f'claim = "{ship}"' not in here
    assert done not in here
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_partial_compensate_fail_gate_inventory():
    """闸文硬卡名必须齐。"""
    names = {name for name in globals() if name.startswith("test_")}
    missing = sorted(REQUIRED_TESTS - names)
    assert not missing, f"补偿闸缺测: {missing}"
