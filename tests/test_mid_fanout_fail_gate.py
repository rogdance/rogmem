"""≥3 mid 全量 fan-out Fail 闸（NL-MULTI-02 + MULTI-03）。

契约：N mid 全量 fan-out，禁「修到 2 就停」。
Draft×1；每条非空 facts 中稿各走一次 Integrate（外层调用次数=N）；
全量 library_candidates 每 mid 同注入；静默丢 mid=Fail。

不抢 #88 LIVE / B→A 热修缝。docs/prompts 零改。
禁止 inspect.getsource 纸老虎。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rogmem.crystal_store import DISTILLER_STORE_WRITE_ENABLED, hydrate
from rogmem.distill_gate import PAYLOAD_KIND_EXTRACT

pytestmark = pytest.mark.mid_fanout_fail_gate

CORPUS = (
    "云岚测试员甲于 2019 年在北窗实验室任职。"
    "后来到南港书院做讲师。周末常去西湾茶社。"
)
QING_FACT = {
    "claim": "2019 年在北窗实验室任职",
    "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
}
SCHOOL_FACT = {
    "claim": "后来到南港书院做讲师",
    "evidence": "后来到南港书院做讲师。",
}
TEA_FACT = {
    "claim": "周末常去西湾茶社",
    "evidence": "周末常去西湾茶社。",
}
QING_DRAFT = {
    "name": "云岚甲",
    "type": "人物",
    "aliases": ["测试员甲"],
    "summary": "在北窗实验室任职。",
    "owner_relation": "同事",
    "body_md": "## 事业经历\n\n云岚测试员甲于 2019 年在北窗实验室任职。",
    "facts": [dict(QING_FACT)],
}
SCHOOL_DRAFT = {
    "name": "南港书院",
    "type": "组织",
    "aliases": [],
    "summary": "云岚甲后来任教的书院。",
    "owner_relation": "任职机构",
    "body_md": "## 发展沿革\n\n后来到南港书院做讲师。",
    "facts": [dict(SCHOOL_FACT)],
}
TEA_DRAFT = {
    "name": "西湾茶社",
    "type": "组织",
    "aliases": [],
    "summary": "周末常去的茶社。",
    "owner_relation": "常去场所",
    "body_md": "## 发展沿革\n\n周末常去西湾茶社。",
    "facts": [dict(TEA_FACT)],
}
THREE_MIDS = (QING_DRAFT, SCHOOL_DRAFT, TEA_DRAFT)
THREE_NAMES = [item["name"] for item in THREE_MIDS]
NEW_ACCEPT = {"identity": "new", "ops": [], "conflicts": []}
EXPECTED_LIB_IDS = {"c-yunlan", "c-school", "c-lab", "c-tea"}


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


def _seed_named(engine, cid: str, name: str, ctype: str = "人物") -> None:
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json)
           VALUES(?,?,?,'active',?,?)""",
        (cid, ctype, name, json.dumps({"名称": name}, ensure_ascii=False), "{}"),
    )
    engine.db().commit()


def _seed_four_library(engine) -> None:
    _seed_named(engine, "c-yunlan", "云岚甲")
    _seed_named(engine, "c-school", "南港书院", "组织")
    _seed_named(engine, "c-lab", "北窗实验室", "组织")
    _seed_named(engine, "c-tea", "西湾茶社", "组织")


def _active_rows(engine):
    return list(
        engine.db().execute(
            "SELECT id, type, name, status, summary_json, chapters_json, "
            "body_md, revision FROM crystallizations WHERE status='active' ORDER BY id"
        )
    )


def _ids(rows):
    return {row["id"] for row in rows}


def _cand_ids(candidates) -> list[str]:
    return [str(item.get("id") or "") for item in (candidates or []) if isinstance(item, dict)]


def _wire_three_mid_draft(engine, monkeypatch, drafts=THREE_MIDS):
    draft_calls: list[int] = []

    def spy_draft(*_a, **_k):
        draft_calls.append(1)
        return {"crystals": [dict(item) for item in drafts]}

    monkeypatch.setattr(engine, "_llm_draft", spy_draft)
    return draft_calls


def _spy_fanout(engine, monkeypatch, *, integrate):
    """Count outer Integrate (per-mid) and inner LLM calls separately.

    B→A 升格会让 ``_llm_integrate`` 每 mid 多于 1 次；fan-out 契约看
    ``_complete_integrate_mid`` 次数 = N，不把升格二次调用算进 N。
    """
    outer: list[dict] = []
    inner: list[dict] = []
    orig_complete = engine._complete_integrate_mid

    def spy_complete(text, kind="", **kw):
        outer.append(
            {
                "name": (kw.get("draft_crystal") or {}).get("name"),
                "ids": _cand_ids(kw.get("library_candidates")),
                "library_candidates": list(kw.get("library_candidates") or []),
            }
        )
        return orig_complete(text, kind, **kw)

    def spy_llm(text, kind="", **kw):
        inner.append(
            {
                "name": (kw.get("draft_crystal") or {}).get("name"),
                "ids": _cand_ids(kw.get("library_candidates")),
            }
        )
        return integrate(text, kind, **kw)

    monkeypatch.setattr(engine, "_complete_integrate_mid", spy_complete)
    monkeypatch.setattr(engine, "_llm_integrate", spy_llm)
    return outer, inner


def test_nl_multi_02_03_three_mids_full_fanout_same_library(tmp_path, monkeypatch):
    """NL-MULTI-02 + MULTI-03: 一稿 ≥3 mid → Integrate×N；全量 candidates 同注入。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_four_library(engine)
    before = _active_rows(engine)
    match = engine._match_integrate_parts(CORPUS)
    match_ids = _cand_ids(match.get("library_candidates"))
    assert set(match_ids) >= EXPECTED_LIB_IDS
    assert len(match_ids) >= 3

    draft_calls = _wire_three_mid_draft(engine, monkeypatch)
    apply_names: list[str] = []
    orig_apply = engine.apply_integrate

    def spy_apply(*args, **kwargs):
        apply_names.append(str((kwargs.get("draft_crystal") or {}).get("name") or ""))
        return orig_apply(*args, **kwargs)

    def on_integrate(_text, _kind="", **kw):
        name = (kw.get("draft_crystal") or {}).get("name")
        if name not in THREE_NAMES:
            raise AssertionError(f"unexpected mid {name}")
        return dict(NEW_ACCEPT)

    outer, inner = _spy_fanout(engine, monkeypatch, integrate=on_integrate)
    monkeypatch.setattr(engine, "apply_integrate", spy_apply)
    extract_calls: list[int] = []
    monkeypatch.setattr(
        engine,
        "_llm_extract",
        lambda *_a, **_k: extract_calls.append(1)
        or {"payload_kind": PAYLOAD_KIND_EXTRACT, "items": []},
    )
    _queue_user(engine, "src-multi-02")
    assert engine.pump_one() == "src-multi-02"
    assert _queue_row(engine, "src-multi-02")["state"] == "done"
    assert draft_calls == [1]
    assert [item["name"] for item in outer] == THREE_NAMES
    assert len(outer) == 3
    assert [item["name"] for item in inner] == THREE_NAMES
    assert len(inner) == 3
    outer_id_sets = [tuple(item["ids"]) for item in outer]
    assert all(ids == outer_id_sets[0] for ids in outer_id_sets)
    assert set(outer_id_sets[0]) >= EXPECTED_LIB_IDS
    assert set(outer_id_sets[0]) == set(match_ids)
    inner_id_sets = [tuple(item["ids"]) for item in inner]
    assert all(ids == inner_id_sets[0] for ids in inner_id_sets)
    assert set(inner_id_sets[0]) >= EXPECTED_LIB_IDS
    assert apply_names == THREE_NAMES
    assert extract_calls == []
    after = _active_rows(engine)
    created = [row for row in after if row["id"] not in _ids(before)]
    # #88: identity=new 复用同名 active，禁止第二张页。落地看原 id 吃到 mid 正文。
    assert created == []
    reused = {row["name"]: row for row in after if row["name"] in THREE_NAMES}
    assert set(reused) == set(THREE_NAMES)
    assert reused["云岚甲"]["id"] == "c-yunlan"
    assert reused["南港书院"]["id"] == "c-school"
    assert reused["西湾茶社"]["id"] == "c-tea"
    for draft in THREE_MIDS:
        row = reused[draft["name"]]
        thin = hydrate(row)
        assert thin.storage == "thin"
        assert row["status"] == "active"
        assert draft["body_md"] in (row["body_md"] or "")


def test_nl_multi_must_not_truncate_when_n_gt_2(tmp_path, monkeypatch):
    """NL-MULTI-02: 不得因 N>2 只走前两条、丢第三条。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_four_library(engine)
    _wire_three_mid_draft(engine, monkeypatch)
    seen: list[str] = []

    def on_integrate(_text, _kind="", **kw):
        name = (kw.get("draft_crystal") or {}).get("name")
        seen.append(name)
        return dict(NEW_ACCEPT)

    outer, _inner = _spy_fanout(engine, monkeypatch, integrate=on_integrate)
    monkeypatch.setattr(
        engine,
        "_llm_extract",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("extract")),
    )
    _queue_user(engine, "src-no-trunc")
    assert engine.pump_one() == "src-no-trunc"
    assert seen == THREE_NAMES
    assert [item["name"] for item in outer] == THREE_NAMES
    assert "西湾茶社" in seen
    assert len(seen) != 2


def test_nl_multi_drop_third_mid_is_fail(tmp_path, monkeypatch):
    """静默丢 mid=Fail：第三条 Integrate 返回 None，不得假 done，也不得只消化前两条。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_four_library(engine)
    before = _active_rows(engine)
    _wire_three_mid_draft(engine, monkeypatch)
    extract_calls: list[int] = []

    def on_integrate(_text, _kind="", **kw):
        name = (kw.get("draft_crystal") or {}).get("name")
        if name == "西湾茶社":
            return None
        return dict(NEW_ACCEPT)

    outer, inner = _spy_fanout(engine, monkeypatch, integrate=on_integrate)
    monkeypatch.setattr(
        engine,
        "_llm_extract",
        lambda *_a, **_k: extract_calls.append(1)
        or {"payload_kind": PAYLOAD_KIND_EXTRACT, "items": []},
    )
    _queue_user(engine, "src-drop-mid")
    assert engine.pump_one() == "src-drop-mid"
    queued = _queue_row(engine, "src-drop-mid")
    assert queued["state"] != "done"
    assert queued["last_error"]
    assert [item["name"] for item in outer] == THREE_NAMES
    assert [item["name"] for item in inner] == THREE_NAMES
    assert extract_calls == []
    after = _active_rows(engine)
    created = [row for row in after if row["id"] not in _ids(before)]
    assert created == []
    by_name = {row["name"]: row for row in after}
    assert by_name["云岚甲"]["id"] == "c-yunlan"
    assert by_name["南港书院"]["id"] == "c-school"
    assert QING_DRAFT["body_md"] not in (by_name["云岚甲"]["body_md"] or "")
    assert SCHOOL_DRAFT["body_md"] not in (by_name["南港书院"]["body_md"] or "")
    assert all(row["id"] == "c-tea" for row in after if row["name"] == "西湾茶社")
    tea = engine.db().execute(
        "SELECT body_md, revision FROM crystallizations WHERE id='c-tea'"
    ).fetchone()
    assert CORPUS not in (tea["body_md"] or "")
    assert int(tea["revision"] or 0) == 0


def test_nl_multi_prompts_git_diff_empty():
    """本刀 docs/prompts 真 git diff 必须空。"""
    from tests.docs_prompts_zero_diff import assert_docs_prompts_zero_diff

    assert DISTILLER_STORE_WRITE_ENABLED is True
    assert_docs_prompts_zero_diff()


def test_nl_multi_fail_gate_inventory():
    """NL-MULTI 负/正路径测网必须齐。"""
    names = {name for name in globals() if name.startswith("test_nl_multi_")}
    required = {
        "test_nl_multi_02_03_three_mids_full_fanout_same_library",
        "test_nl_multi_must_not_truncate_when_n_gt_2",
        "test_nl_multi_drop_third_mid_is_fail",
        "test_nl_multi_prompts_git_diff_empty",
    }
    missing = sorted(required - names)
    assert not missing, f"NL-MULTI Fail 闸缺测: {missing}"
