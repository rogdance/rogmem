"""maybe→deferred 全自动消化 Fail 闸。

evaluate 后 identity=maybe → GateResult.decision=deferred。
有 Draft mid 则落 status=deferred 薄页（maybe_with / deferred_retries）。
零 active 新建、零 ops 落库。写开关关仍 deferred。
N-loop/batch 含 maybe 算 non-accept，挡泵假 done。
retries 递增可测；触顶仍 deferred、不 done、不双建。
禁退 extract / 假 new。docs/prompts 零 diff。
禁止 inspect.getsource 纸老虎。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rogmem.crystal_store import (
    DEFERRED_RETRY_LIMIT,
    DISTILLER_STORE_WRITE_ENABLED,
    CrystalStore,
    hydrate,
)
from rogmem.distill_gate import PAYLOAD_KIND_EXTRACT, evaluate_integrate

pytestmark = pytest.mark.maybe_deferred_fail_gate

CORPUS = "云岚测试员甲于 2019 年在北窗实验室任职。后来到南港书院做讲师。"
MAYBE_DRAFT = {
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
}
YUNLAN_DRAFT = {
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
MAYBE = {"identity": "maybe", "ops": [], "conflicts": []}
MAYBE_WITH_OWNER = {"identity": "maybe", "matched_id": "owner", "ops": [], "conflicts": []}
NEW_ACCEPT = {"identity": "new", "ops": [], "conflicts": []}
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


def _queue_row(engine, source_id: str):
    return engine.db().execute(
        "SELECT state, last_error FROM queue WHERE source_id=?", (source_id,)
    ).fetchone()


def _active_rows(engine):
    return list(
        engine.db().execute(
            "SELECT id, type, name, status, revision, body_md FROM crystallizations "
            "WHERE status='active' ORDER BY id"
        )
    )


def _deferred_rows(engine, name: str | None = None):
    sql = (
        "SELECT id, type, name, status, maybe_with, deferred_retries, revision, body_md "
        "FROM crystallizations WHERE status='deferred'"
    )
    params: tuple = ()
    if name is not None:
        sql += " AND name=?"
        params = (name,)
    return list(engine.db().execute(sql + " ORDER BY id", params))


def _ids(rows):
    return {row["id"] for row in rows}


def _owner_snap(engine):
    return engine.db().execute(
        "SELECT revision, unresolved_conflicts, envelope_json, body_md, status "
        "FROM crystallizations WHERE id='owner'"
    ).fetchone()


def _seed_named(engine, cid: str, name: str, ctype: str = "人物") -> None:
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json)
           VALUES(?,?,?,'active',?,?)""",
        (cid, ctype, name, json.dumps({"名称": name}, ensure_ascii=False), "{}"),
    )
    engine.db().commit()


def _apply_maybe(engine, payload=None, draft=None, **kwargs):
    return engine.apply_integrate(
        payload if payload is not None else dict(MAYBE),
        candidates=kwargs.pop("candidates", ["owner"]),
        corpus=kwargs.pop("corpus", CORPUS),
        store=kwargs.pop("store", CrystalStore(engine.db())),
        draft_crystal=MAYBE_DRAFT if draft is None else draft,
        holder=kwargs.pop("holder", "maybe-defer-qa"),
        **kwargs,
    )


def test_maybe_evaluate_accept_apply_deferred(tmp_path, monkeypatch):
    """MD-DEC-01: evaluate 可 accept；apply_integrate 必须 decision=deferred。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    gated = evaluate_integrate(MAYBE, candidates=["owner"], corpus=CORPUS)
    assert gated.identity == "maybe"
    assert gated.decision == "accept"
    engine = _engine(tmp_path, monkeypatch)
    result = _apply_maybe(engine)
    assert result.identity == "maybe"
    assert result.decision == "deferred"
    assert result.ok is False


def test_maybe_zero_active_and_zero_ops(tmp_path, monkeypatch):
    """MD-ZERO-01: maybe 零新建 active，零 ops 改旧条。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    owner_before = _owner_snap(engine)
    statements_before = engine.db().execute("SELECT COUNT(*) AS n FROM statements").fetchone()["n"]
    result = _apply_maybe(engine, payload=MAYBE_WITH_OWNER, crystal_id="owner")
    assert result.decision == "deferred"
    assert result.identity == "maybe"
    assert result.matched_id == "owner"
    after = _active_rows(engine)
    assert _ids(after) == _ids(before)
    owner_after = _owner_snap(engine)
    assert owner_after["status"] == "active"
    assert owner_after["revision"] == owner_before["revision"]
    assert owner_after["body_md"] == owner_before["body_md"]
    assert owner_after["envelope_json"] == owner_before["envelope_json"]
    assert owner_after["unresolved_conflicts"] == owner_before["unresolved_conflicts"]
    statements_after = engine.db().execute("SELECT COUNT(*) AS n FROM statements").fetchone()["n"]
    assert statements_after == statements_before
    assert all(row["name"] != MAYBE_DRAFT["name"] for row in after)


def test_maybe_mid_lands_deferred_page_maybe_with(tmp_path, monkeypatch):
    """MD-PAGE-01: 有 mid 则 deferred 页可查；maybe_with ← 模型 matched_id。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    result = _apply_maybe(engine, payload=MAYBE_WITH_OWNER, crystal_id="owner")
    assert result.decision == "deferred"
    pages = _deferred_rows(engine, MAYBE_DRAFT["name"])
    assert len(pages) == 1
    page = pages[0]
    assert page["status"] == "deferred"
    assert page["maybe_with"] == "owner"
    assert int(page["deferred_retries"] or 0) == 1
    loaded = CrystalStore(engine.db()).get(page["id"])
    assert loaded is not None
    assert loaded.status == "deferred"
    assert loaded.maybe_with == "owner"
    assert loaded.name == MAYBE_DRAFT["name"]
    thin = hydrate(page)
    assert thin.storage == "thin"
    assert thin.status == "deferred"


def test_maybe_with_may_be_empty(tmp_path, monkeypatch):
    """MD-PAGE-02: maybe_with 可空。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    result = _apply_maybe(engine, payload=MAYBE)
    assert result.decision == "deferred"
    pages = _deferred_rows(engine, MAYBE_DRAFT["name"])
    assert len(pages) == 1
    assert pages[0]["maybe_with"] in (None, "")


def test_write_switch_off_still_deferred_zero_active(tmp_path, monkeypatch, write_switch_off):
    """MD-WS-01: 写开关关仍 deferred，零写 active，也不落 deferred 页。"""
    import rogmem.crystal_store as crystal_store_mod

    assert crystal_store_mod.DISTILLER_STORE_WRITE_ENABLED is False
    engine = _engine(tmp_path, monkeypatch)
    before_all = list(engine.db().execute("SELECT id, status FROM crystallizations"))
    before_active = _active_rows(engine)
    result = _apply_maybe(engine, payload=MAYBE_WITH_OWNER, crystal_id="owner")
    assert result.decision == "deferred"
    assert result.identity == "maybe"
    after_all = list(engine.db().execute("SELECT id, status FROM crystallizations"))
    assert {(row["id"], row["status"]) for row in after_all} == {
        (row["id"], row["status"]) for row in before_all
    }
    assert _ids(_active_rows(engine)) == _ids(before_active)
    assert _deferred_rows(engine) == []


def test_batch_containing_maybe_blocks_done(tmp_path, monkeypatch):
    """MD-BATCH-01: batch 含 maybe 禁 done；先写下的 same 一并撤回。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    before_active = _active_rows(engine)
    extract_calls: list[int] = []

    def spy_integrate(_text, _kind="", **kw):
        name = (kw.get("draft_crystal") or {}).get("name")
        if name == "云岚甲":
            return dict(SAME_QING)
        return dict(MAYBE_WITH_OWNER)

    monkeypatch.setattr(
        engine,
        "_llm_draft",
        lambda *_a, **_k: {"crystals": [YUNLAN_DRAFT, MAYBE_DRAFT]},
    )
    monkeypatch.setattr(engine, "_llm_integrate", spy_integrate)
    monkeypatch.setattr(
        engine,
        "_llm_extract",
        lambda *_a, **_k: extract_calls.append(1) or {"payload_kind": PAYLOAD_KIND_EXTRACT, "items": []},
    )
    _queue_user(engine, "src-batch-maybe")
    assert engine.pump_one() == "src-batch-maybe"
    queued = _queue_row(engine, "src-batch-maybe")
    assert queued["state"] != "done"
    assert queued["last_error"]
    qing = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='c-yunlan'").fetchone()
    assert CORPUS not in (qing["body_md"] or "")
    created_active = [row for row in _active_rows(engine) if row["id"] not in _ids(before_active)]
    assert created_active == []
    assert all(row["name"] != MAYBE_DRAFT["name"] for row in _active_rows(engine))
    assert _deferred_rows(engine, MAYBE_DRAFT["name"]) == []
    assert extract_calls == []


def test_maybe_does_not_fallback_extract_or_fake_new(tmp_path, monkeypatch):
    """MD-NEG-01: maybe 禁退 extract、禁假 new / 假 accept。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    before_active = _active_rows(engine)
    extract_calls: list[int] = []

    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: {"crystals": [MAYBE_DRAFT]})
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: dict(MAYBE))
    monkeypatch.setattr(
        engine,
        "_llm_extract",
        lambda *_a, **_k: extract_calls.append(1) or {"payload_kind": PAYLOAD_KIND_EXTRACT, "items": []},
    )
    _queue_user(engine, "src-maybe-no-extract")
    assert engine.pump_one() == "src-maybe-no-extract"
    queued = _queue_row(engine, "src-maybe-no-extract")
    assert queued["state"] != "done"
    assert extract_calls == []
    result = _apply_maybe(engine)
    assert result.identity == "maybe"
    assert result.decision != "accept"
    assert result.decision == "deferred"
    created_active = [row for row in _active_rows(engine) if row["id"] not in _ids(before_active)]
    assert created_active == []
    assert all(row["status"] != "active" or row["name"] != MAYBE_DRAFT["name"] for row in _active_rows(engine))


def test_deferred_retries_increment_then_cap_no_double(tmp_path, monkeypatch):
    """MD-QUOTA-01: retries 递增可测；触顶仍 deferred、不双建、不 accept。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    assert DEFERRED_RETRY_LIMIT == 3
    engine = _engine(tmp_path, monkeypatch)
    before_active = _active_rows(engine)
    seen_ids: set[str] = set()
    last = None
    for step in range(1, DEFERRED_RETRY_LIMIT + 2):
        last = _apply_maybe(engine, payload=MAYBE_WITH_OWNER, crystal_id="owner")
        assert last.decision == "deferred"
        assert last.identity == "maybe"
        pages = _deferred_rows(engine, MAYBE_DRAFT["name"])
        assert len(pages) == 1
        seen_ids.add(pages[0]["id"])
        assert len(seen_ids) == 1
        expected = min(step, DEFERRED_RETRY_LIMIT)
        assert int(pages[0]["deferred_retries"] or 0) == expected
        assert pages[0]["maybe_with"] == "owner"
    assert last is not None
    assert last.decision == "deferred"
    assert last.ok is False
    assert _ids(_active_rows(engine)) == _ids(before_active)
    assert len(_deferred_rows(engine, MAYBE_DRAFT["name"])) == 1


def test_cap_pump_not_done_no_second_page(tmp_path, monkeypatch):
    """MD-QUOTA-02: 触顶后泵仍不 done，不双建 active / deferred。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    store = CrystalStore(engine.db())
    for _ in range(DEFERRED_RETRY_LIMIT):
        landed = _apply_maybe(engine, store=store)
        assert landed.decision == "deferred"
    page_id = _deferred_rows(engine, MAYBE_DRAFT["name"])[0]["id"]
    before_active = _active_rows(engine)
    extract_calls: list[int] = []
    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: {"crystals": [MAYBE_DRAFT]})
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: dict(MAYBE))
    monkeypatch.setattr(
        engine,
        "_llm_extract",
        lambda *_a, **_k: extract_calls.append(1) or {"payload_kind": PAYLOAD_KIND_EXTRACT, "items": []},
    )
    _queue_user(engine, "src-maybe-cap")
    assert engine.pump_one() == "src-maybe-cap"
    queued = _queue_row(engine, "src-maybe-cap")
    assert queued["state"] != "done"
    assert queued["last_error"]
    pages = _deferred_rows(engine, MAYBE_DRAFT["name"])
    assert len(pages) == 1
    assert pages[0]["id"] == page_id
    assert int(pages[0]["deferred_retries"] or 0) == DEFERRED_RETRY_LIMIT
    assert _ids(_active_rows(engine)) == _ids(before_active)
    assert extract_calls == []


def test_maybe_docs_prompts_git_diff_empty():
    """MD-PROMPT-01: docs/prompts 真 git diff 为空。"""
    from tests.docs_prompts_zero_diff import assert_docs_prompts_zero_diff

    assert DISTILLER_STORE_WRITE_ENABLED is True
    assert_docs_prompts_zero_diff()


def test_maybe_fail_gate_negative_inventory():
    """MD-FAIL-01: 负路径测网必须齐。"""
    names = {name for name in globals() if name.startswith("test_")}
    required = {
        "test_maybe_evaluate_accept_apply_deferred",
        "test_maybe_zero_active_and_zero_ops",
        "test_maybe_mid_lands_deferred_page_maybe_with",
        "test_maybe_with_may_be_empty",
        "test_write_switch_off_still_deferred_zero_active",
        "test_batch_containing_maybe_blocks_done",
        "test_maybe_does_not_fallback_extract_or_fake_new",
        "test_deferred_retries_increment_then_cap_no_double",
        "test_cap_pump_not_done_no_second_page",
        "test_maybe_docs_prompts_git_diff_empty",
    }
    missing = sorted(required - names)
    assert not missing, f"maybe→deferred Fail 负/正路径缺测: {missing}"
