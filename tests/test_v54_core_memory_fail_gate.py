"""v5.4 主人核心记忆 — 引擎 + Integrate 落地 P0 Fail 闸。

行为证据：注入后的 user 文本、statement 读回、core_memory 包、git diff。
禁止 inspect.getsource / 源码 sniff 冒充绿。
prompts 零 diff；≠可上线；wipe 仍卡。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rogmem.crystal_store import (
    CrystalStore,
    ThinCrystal,
    parse_valid_until,
)
from rogmem.distill_gate import (
    CORE_MODULE_ALIASES,
    GateIssue,
    GateResult,
    check_core_module_complete,
    stamp_integrate_payload,
    _core_canonical,
)
from rogmem.distill_resim import approved_draft_prompts, fill_draft_user

pytestmark = pytest.mark.v54_core_memory_fail_gate

CORPUS = "云岚测试员甲于 2019 年在北窗实验室任职。后来到南港书院做讲师。"
FOCUS_QUOTE = "最近在搞云岚核忆这个仓库。"
AGREE_QUOTE = "以后回答都先给结论。"
MEAT_QUOTE = "我这段时间爱吃红烧肉。"
QUIT_QUOTE = "我现在戒肉吃素了。"
REPO = "https://github.com/example/memory"
EVENT0 = "2026-01-01T00:00:00+00:00"

CORE_BODY = """## 身份速写

云岚核忆主人，现居南港。

## 沟通与决策风格

结论先行，少步骤。

## 约定与待办

以后回答都先给结论。

## 兴趣与偏好

爱吃红烧肉。

## 当前关注与计划

最近在搞云岚核忆。

## 教育背景

南港书院。
"""

LEGACY_STYLE_BODY = """## 身份速写

一句话身份。

## 能力与风格

旧库里的沟通决策写法。

## 约定与待办

长期先给结论。

## 兴趣与偏好

收集青石标本。

## 当前关注与计划

在推进云岚核忆。
"""


def _engine(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _insert_source(
    engine,
    *,
    sid: str,
    kind: str,
    body: str,
    event_at: str = EVENT0,
    turn_id: str = "",
    project_ref: str = "",
):
    engine.db().execute(
        """INSERT INTO sources(
             id,kind,turn_id,project_ref,event_at,ingested_at,content_hash,body,status,lane,pair_ready
           ) VALUES(?,?,?,?,?,?,?,?, '已蒸馏','fast',1)""",
        (sid, kind, turn_id or None, project_ref or None, event_at, event_at, f"hash-{sid}", body),
    )
    engine.db().commit()
    return engine.db().execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()


def _set_owner_body(engine, body_md: str) -> None:
    store = CrystalStore(engine.db())
    owner = store.get("owner")
    assert owner is not None
    store.put(
        ThinCrystal(
            id="owner",
            type=owner.type,
            name=owner.name,
            status="active",
            revision=owner.revision,
            summary=owner.summary,
            aliases=list(owner.aliases),
            owner_relation=owner.owner_relation,
            body_md=body_md,
            facts=list(owner.facts),
            keys=dict(owner.keys),
        ),
        holder="test:v54",
    )


def _loaded_names(payload: dict) -> list[str]:
    return [item["name"] for item in payload.get("loaded") or []]


def _loaded_text(payload: dict, name: str) -> str:
    for item in payload.get("loaded") or []:
        if item["name"] == name:
            return item["text"]
    return ""


def _queue_user(engine, source_id: str, body: str) -> None:
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


def _core_interest_rows(engine):
    return list(
        engine.db().execute(
            """SELECT text, quote, invalid_at, event_at, field, chapter FROM statements
               WHERE crystal_id='owner'
                 AND (COALESCE(field,'') IN ('兴趣爱好','兴趣与偏好')
                      OR COALESCE(chapter,'') IN ('兴趣爱好','兴趣与偏好'))
               ORDER BY COALESCE(event_at, created_at), created_at"""
        )
    )


def _apply_interest_op(engine, source, *, value: str, quote: str):
    return engine.apply_integrate(
        {
            "identity": "same",
            "matched_id": "owner",
            "ops": [
                {
                    "op": "update",
                    "path": "body_md##兴趣与偏好",
                    "value": value,
                    "fact": {"claim": value, "evidence": quote, "anchor": "兴趣与偏好"},
                }
            ],
            "conflicts": [],
            "facts": [{"claim": value, "evidence": quote, "anchor": "兴趣与偏好"}],
        },
        candidates=["owner"],
        corpus=quote,
        store=CrystalStore(engine.db()),
        crystal_id="owner",
        holder=f"distill:{source['id']}",
        draft_crystal={
            "type": "人物",
            "name": "记忆主人",
            "summary": "云岚核忆主人。",
            "body_md": CORE_BODY.replace("爱吃红烧肉。", value),
            "facts": [{"claim": value, "evidence": quote, "anchor": "兴趣与偏好"}],
        },
    )


def test_PROMPT_V54_02_prompts_marked_expired():
    """旧蒸馏稿不再冻成现行契约，文首必须标明过期。"""
    root = Path(__file__).resolve().parents[1]
    draft = (root / "docs/prompts/wiki-distill-draft-prompt-v5.md").read_text(encoding="utf-8")
    current = (root / "docs/CURRENT.md").read_text(encoding="utf-8")
    assert draft.startswith("> **已过期。**")
    assert "wiki-distill-draft-prompt-v5.md" in current


def test_V54_MK_01_material_kind_maps_and_fills_user(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _, tmpl = approved_draft_prompts()
    mapping = {"user": "会话", "agent": "会话", "summary": "会话", "file": "文件"}
    for kind, label in mapping.items():
        source = _insert_source(engine, sid=f"mk-{kind}", kind=kind, body="我喜欢爵士。")
        inject = engine._prompt_injection(source)
        assert inject["material_kind"] == label
        filled = fill_draft_user(
            tmpl,
            filename="档案.md",
            material="我喜欢爵士。",
            owner_real_name="云岚甲",
            owner_aliases="测试员甲",
            material_kind=inject["material_kind"],
        )
        assert f"材料类型：{label}" in filled
        assert "{{material_kind}}" not in filled

    seen: dict[str, str] = {}

    def fake_complete(_base, _key, payload, **_kwargs):
        seen[payload["messages"][1]["content"].split("材料类型：", 1)[-1][:2]] = payload["messages"][1]["content"]
        return json.dumps({"crystals": [{"name": "云岚甲", "type": "人物", "facts": [{"claim": "x", "evidence": "我喜欢爵士。"}]}]}), "stop"

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m") if role == "distill" else None)
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    engine._llm_draft("我喜欢爵士。", "user", filename="a.md", owner_real_name="云岚甲", owner_aliases="甲")
    engine._llm_draft("我喜欢爵士。", "file", filename="a.md", owner_real_name="云岚甲", owner_aliases="甲")
    assert any("材料类型：会话" in text for text in seen.values())
    assert any("材料类型：文件" in text for text in seen.values())


def test_V54_SLICE_01_core_memory_reads_five_headings(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _set_owner_body(engine, CORE_BODY)
    packed = engine.core_memory(budget=8000)
    names = _loaded_names(packed)
    assert names.index("身份速写") < names.index("沟通与决策风格")
    assert names.index("沟通与决策风格") < names.index("约定与待办")
    assert names.index("约定与待办") < names.index("兴趣与偏好")
    assert names.index("兴趣与偏好") < names.index("当前关注与计划")
    if "教育背景" in names:
        assert names.index("当前关注与计划") < names.index("教育背景")
    assert "结论先行" in _loaded_text(packed, "沟通与决策风格")
    assert "爱吃红烧肉" in _loaded_text(packed, "兴趣与偏好")


def test_V54_SLICE_02_budget_order_and_legacy_alias(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _set_owner_body(engine, LEGACY_STYLE_BODY)
    packed = engine.core_memory(budget=8000)
    assert "旧库里的沟通决策写法" in _loaded_text(packed, "能力与风格")
    huge = "\n".join(
        [
            "## 身份速写\n" + ("甲" * 80),
            "## 沟通与决策风格\n" + ("乙" * 80),
            "## 约定与待办\n" + ("丙" * 80),
            "## 兴趣与偏好\n" + ("丁" * 80),
            "## 当前关注与计划\n" + ("戊" * 80),
        ]
    )
    _set_owner_body(engine, huge)
    tight = engine.core_memory(budget=120)
    cores = [
        name
        for name in _loaded_names(tight)
        if name in {"身份速写", "沟通与决策风格", "约定与待办", "兴趣与偏好", "当前关注与计划"}
    ]
    assert cores[0] == "身份速写"
    if "沟通与决策风格" in cores:
        assert cores.index("沟通与决策风格") == 1
    assert "当前关注与计划" not in cores


def test_V54_TTL_01_soft_ttl_write_and_lazy_drop(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    source = _insert_source(engine, sid="ttl-focus", kind="user", body=FOCUS_QUOTE, event_at=EVENT0)
    engine._write_field(
        source,
        {"field": "当前关注", "value": "最近在搞云岚核忆", "quote": FOCUS_QUOTE, "crystal": "记忆主人", "trust": "written"},
        "user",
        FOCUS_QUOTE,
    )
    row = engine.db().execute(
        "SELECT text, valid_until, invalid_at FROM statements WHERE field='当前关注' AND invalid_at IS NULL"
    ).fetchone()
    assert row is not None
    assert row["valid_until"] == "2026-01-31"
    packed = engine.core_memory(budget=8000, now=EVENT0)
    assert "云岚核忆" in json.dumps(packed, ensure_ascii=False)
    later = engine.core_memory(budget=8000, now="2026-02-02T00:00:00+00:00")
    assert "云岚核忆" not in _loaded_text(later, "当前关注与计划")
    hist = engine.db().execute("SELECT text, invalid_at FROM statements WHERE field='当前关注'").fetchone()
    assert hist["text"]
    assert hist["invalid_at"] is None


def test_V54_TTL_02_and_VU_explicit_date_no_invent(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    source = _insert_source(engine, sid="vu-dated", kind="user", body="这周五前交设计稿。")
    engine._write_ops_sidecar(
        engine.db(),
        "owner",
        [
            {
                "op": "add",
                "path": "body_md##当前关注与计划",
                "value": "周五前交设计稿",
                "fact": {
                    "claim": "周五前交设计稿",
                    "evidence": "这周五前交设计稿。",
                    "anchor": "当前关注与计划",
                    "valid_until": "2026-01-09",
                },
            }
        ],
        holder="distill:vu-dated",
    )
    engine.db().commit()
    row = engine.db().execute(
        "SELECT valid_until, text FROM statements WHERE crystal_id='owner' AND text LIKE '%设计稿%'"
    ).fetchone()
    assert row["valid_until"] == "2026-01-09"
    after = engine.core_memory(budget=8000, now="2026-01-10T00:00:00+00:00")
    assert "设计稿" not in _loaded_text(after, "当前关注与计划")
    still = engine.db().execute("SELECT text FROM statements WHERE text LIKE '%设计稿%'").fetchone()
    assert still is not None
    assert parse_valid_until("明天") is None
    assert parse_valid_until("2026-13-40") is None
    assert parse_valid_until("2026-01-09") == "2026-01-09"
    assert parse_valid_until(None) is None


def test_V54_AGREE_01_accept_agreement_survives_ttl(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _insert_source(engine, sid="ag-agent", kind="agent", body="我会先给结论再展开。", turn_id="t-agree")
    user = _insert_source(engine, sid="ag-user", kind="user", body="同意，就这么做。", turn_id="t-agree")
    engine._accept_agreement(user)
    row = engine.db().execute(
        "SELECT field, text, valid_until, invalid_at FROM statements WHERE field='约定待办' AND invalid_at IS NULL"
    ).fetchone()
    assert row is not None
    assert row["valid_until"] in {None, ""}
    packed = engine.core_memory(budget=8000, now="2027-01-01T00:00:00+00:00")
    blob = json.dumps(packed, ensure_ascii=False)
    assert "先给结论" in blob


def test_V54_CHAIN_01_meat_to_quit_keeps_history(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _set_owner_body(engine, CORE_BODY)
    first = _insert_source(engine, sid="meat-1", kind="user", body=MEAT_QUOTE, event_at="2026-03-01T00:00:00+00:00")
    first_result = _apply_interest_op(engine, first, value="爱吃红烧肉", quote=MEAT_QUOTE)
    assert first_result.decision == "accept"
    second = _insert_source(engine, sid="meat-2", kind="user", body=QUIT_QUOTE, event_at="2026-04-01T00:00:00+00:00")
    second_result = _apply_interest_op(engine, second, value="戒肉吃素", quote=QUIT_QUOTE)
    assert second_result.decision == "accept"
    rows = _core_interest_rows(engine)
    texts = [row["text"] for row in rows]
    quotes = [row["quote"] for row in rows]
    assert "爱吃红烧肉" in texts
    assert "戒肉吃素" in texts
    assert MEAT_QUOTE in quotes
    assert QUIT_QUOTE in quotes
    old = [row for row in rows if "爱吃红烧肉" in (row["text"] or "")]
    new = [row for row in rows if "戒肉吃素" in (row["text"] or "")]
    assert old and all(row["invalid_at"] for row in old)
    assert new and all(row["invalid_at"] is None for row in new)
    live = [row["text"] for row in rows if row["invalid_at"] is None]
    assert live == ["戒肉吃素"]
    packed = engine.core_memory(budget=8000, now="2026-04-01T00:00:00+00:00")
    interest = _loaded_text(packed, "兴趣与偏好")
    assert "戒肉吃素" in interest
    assert "爱吃红烧肉" not in interest


def test_V54_INT_01_same_summary_only_does_not_drop_core(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _set_owner_body(engine, CORE_BODY)
    before = engine.db().execute("SELECT body_md, revision FROM crystallizations WHERE id='owner'").fetchone()
    draft = {
        "type": "人物",
        "name": "记忆主人",
        "summary": "只改这一句概述。",
        "body_md": CORE_BODY,
        "facts": [
            {
                "claim": "爱吃红烧肉",
                "evidence": MEAT_QUOTE,
                "anchor": "兴趣与偏好",
            }
        ],
    }
    result = engine.apply_integrate(
        {
            "identity": "same",
            "matched_id": "owner",
            "ops": [{"op": "update", "path": "summary", "value": "只改这一句概述。"}],
            "conflicts": [],
        },
        candidates=["owner"],
        corpus=MEAT_QUOTE,
        store=CrystalStore(engine.db()),
        crystal_id="owner",
        draft_crystal=draft,
    )
    assert result.decision == "rejected"
    assert any(item.code == "core_module_ops_missing" for item in result.issues)
    after = engine.db().execute("SELECT body_md, revision FROM crystallizations WHERE id='owner'").fetchone()
    assert after["body_md"] == before["body_md"]
    assert "爱吃红烧肉" in (after["body_md"] or "")
    with pytest.raises(RuntimeError, match="silent"):
        engine._raise_if_new_land_unwritten(result)
    dropped = GateResult(
        decision="rejected",
        identity="same",
        issues=[GateIssue("core_module_dropped", "same 合并不得挤掉核心节")],
    )
    with pytest.raises(RuntimeError, match="silent"):
        engine._raise_if_new_land_unwritten(dropped)


def test_V54_INT_01_core_module_reject_pump_not_done(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _set_owner_body(engine, CORE_BODY)
    draft = {
        "type": "人物",
        "name": "记忆主人",
        "summary": "只改这一句概述。",
        "body_md": CORE_BODY,
        "facts": [{"claim": "爱吃红烧肉", "evidence": MEAT_QUOTE, "anchor": "兴趣与偏好"}],
    }
    raw = {
        "identity": "same",
        "matched_id": "owner",
        "ops": [{"op": "update", "path": "summary", "value": "只改这一句概述。"}],
        "conflicts": [],
    }

    def fake_produce(*_a, **_k):
        stamped = stamp_integrate_payload(dict(raw))
        stamped["_draft_crystal"] = draft
        return stamped

    monkeypatch.setattr(engine, "_produce_distill_payload", fake_produce)
    _queue_user(engine, "src-int01-silent", MEAT_QUOTE)
    assert engine.pump_one() == "src-int01-silent"
    queued = _queue_row(engine, "src-int01-silent")
    assert queued["state"] != "done"
    assert queued["last_error"]
    assert "silent" in (queued["last_error"] or "")


def test_V54_INT_01_summary_only_without_touch_accepts(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _set_owner_body(engine, CORE_BODY)
    before = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='owner'").fetchone()["body_md"]
    result = engine.apply_integrate(
        {
            "identity": "same",
            "matched_id": "owner",
            "ops": [{"op": "update", "path": "summary", "value": "只改这一句概述。"}],
            "conflicts": [],
        },
        candidates=["owner"],
        corpus="只改这一句概述。",
        store=CrystalStore(engine.db()),
        crystal_id="owner",
        draft_crystal={
            "type": "人物",
            "name": "记忆主人",
            "summary": "只改这一句概述。",
            "body_md": CORE_BODY,
            "facts": [],
        },
    )
    assert result.decision == "accept"
    after = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='owner'").fetchone()
    assert after["body_md"] == before
    assert "## 兴趣与偏好" in (after["body_md"] or "")
    assert "## 沟通与决策风格" in (after["body_md"] or "")
    assert "## 约定与待办" in (after["body_md"] or "")
    assert "爱吃红烧肉" in (after["body_md"] or "")
    thin = CrystalStore(engine.db()).get("owner")
    assert thin is not None
    assert "只改这一句概述" in (thin.summary or "")


def test_V54_INT_01_same_with_section_ops_keeps_modules(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _set_owner_body(engine, CORE_BODY)
    result = engine.apply_integrate(
        {
            "identity": "same",
            "matched_id": "owner",
            "ops": [
                {"op": "update", "path": "summary", "value": "云岚核忆主人。"},
                {"op": "update", "path": "body_md##兴趣与偏好", "value": "改为戒肉吃素。"},
            ],
            "conflicts": [],
            "facts": [
                {"claim": "改为戒肉吃素", "evidence": QUIT_QUOTE, "anchor": "兴趣与偏好"},
            ],
        },
        candidates=["owner"],
        corpus=QUIT_QUOTE + MEAT_QUOTE,
        store=CrystalStore(engine.db()),
        crystal_id="owner",
        draft_crystal={
            "type": "人物",
            "name": "记忆主人",
            "summary": "云岚核忆主人。",
            "body_md": CORE_BODY.replace("爱吃红烧肉。", "改为戒肉吃素。"),
            "facts": [{"claim": "改为戒肉吃素", "evidence": QUIT_QUOTE, "anchor": "兴趣与偏好"}],
        },
    )
    assert result.decision == "accept"
    body = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='owner'").fetchone()["body_md"]
    assert "## 兴趣与偏好" in body
    assert "## 沟通与决策风格" in body
    assert "## 约定与待办" in body
    assert "戒肉吃素" in body


def test_V54_INT_02_supersede_keeps_old_statement(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _set_owner_body(engine, CORE_BODY)
    first = _insert_source(engine, sid="int-meat", kind="user", body=MEAT_QUOTE, event_at="2026-03-01T00:00:00+00:00")
    assert _apply_interest_op(engine, first, value="爱吃红烧肉", quote=MEAT_QUOTE).decision == "accept"
    second = _insert_source(engine, sid="int-quit", kind="user", body=QUIT_QUOTE, event_at="2026-04-01T00:00:00+00:00")
    assert _apply_interest_op(engine, second, value="戒肉吃素", quote=QUIT_QUOTE).decision == "accept"
    rows = _core_interest_rows(engine)
    assert [row["quote"] for row in rows] == [MEAT_QUOTE, QUIT_QUOTE]
    old = next(row for row in rows if row["quote"] == MEAT_QUOTE)
    new = next(row for row in rows if row["quote"] == QUIT_QUOTE)
    assert old["invalid_at"]
    assert new["invalid_at"] is None
    live = [row["text"] for row in rows if row["invalid_at"] is None]
    assert live == ["戒肉吃素"]
    issues = check_core_module_complete(
        {
            "identity": "same",
            "ops": [{"op": "update", "path": "summary", "value": "x"}],
        },
        draft={"body_md": "## 兴趣与偏好\n爱吃红烧肉。", "facts": [{"anchor": "兴趣与偏好", "claim": "x"}]},
    )
    assert issues and issues[0].code == "core_module_ops_missing"
    untouched = check_core_module_complete(
        {
            "identity": "same",
            "ops": [{"op": "update", "path": "summary", "value": "只改概述"}],
        },
        draft={"body_md": CORE_BODY, "facts": []},
    )
    assert untouched == []


def test_V54_ENG_dual_land_and_agent_gates(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    user = _insert_source(
        engine,
        sid="eng-user",
        kind="user",
        body=f"我在推进 {REPO} 的核心记忆。",
        project_ref=REPO,
    )
    engine._write_field(
        user,
        {
            "field": "当前关注",
            "value": f"正在推进：云岚核忆（{REPO}）——核心记忆",
            "quote": f"我在推进 {REPO} 的核心记忆。",
            "crystal": "记忆主人",
            "trust": "written",
        },
        "user",
        f"我在推进 {REPO} 的核心记忆。",
    )
    project = engine.db().execute(
        "SELECT id, name, type FROM crystallizations WHERE type='工程' AND status='active'"
    ).fetchone()
    assert project is not None
    repo_row = engine.db().execute(
        "SELECT text FROM statements WHERE crystal_id=? AND field='仓库地址'",
        (project["id"],),
    ).fetchone()
    assert repo_row is not None
    assert REPO in (repo_row["text"] or "")
    packed = engine.core_memory(budget=8000, now=EVENT0)
    assert REPO in json.dumps(packed, ensure_ascii=False) or "云岚核忆" in json.dumps(packed, ensure_ascii=False)

    agent = _insert_source(
        engine,
        sid="eng-agent",
        kind="agent",
        body=f"我在 {REPO} 修了 TTL，卡在 wipe。我擅长总结。",
        project_ref=REPO,
    )
    engine._write_field(
        agent,
        {"field": "沟通风格", "value": "我擅长总结", "quote": "我擅长总结。", "crystal": "记忆主人", "trust": "written"},
        "agent",
        agent["body"],
    )
    engine._write_field(
        agent,
        {"field": "约定待办", "value": "以后都由我总结", "quote": "我擅长总结。", "crystal": "记忆主人", "trust": "written"},
        "agent",
        agent["body"],
    )
    style = engine.db().execute(
        "SELECT 1 FROM statements WHERE field IN ('沟通风格','约定待办') AND source_id='eng-agent'"
    ).fetchone()
    assert style is None
    engine._write_field(
        agent,
        {
            "field": "当前关注",
            "value": f"正在推进：云岚核忆（{REPO}）——修了 TTL，卡在 wipe",
            "quote": f"我在 {REPO} 修了 TTL，卡在 wipe。",
            "crystal": "记忆主人",
            "trust": "written",
        },
        "agent",
        agent["body"],
    )
    focus = engine.db().execute(
        "SELECT text FROM statements WHERE field='当前关注' AND source_id='eng-agent' AND invalid_at IS NULL"
    ).fetchone()
    assert focus is not None
    assert "TTL" in focus["text"]


def test_V54_ENG_02_repo_only_and_hearsay_do_not_dual_land(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    repo_only = _insert_source(engine, sid="eng-repo-only", kind="user", body=f"仓库是 {REPO}。")
    con = engine.db()
    engine._insert_crystal_page(con, "proj-repo", "工程", "云岚核忆", holder=f"distill:{repo_only['id']}")
    con.commit()
    engine._write_field(
        repo_only,
        {
            "field": "仓库地址",
            "value": REPO,
            "quote": REPO,
            "crystal": "云岚核忆",
            "type": "工程",
            "trust": "written",
        },
        "user",
        repo_only["body"],
    )
    hear = _insert_source(
        engine,
        sid="eng-hear-react",
        kind="user",
        body=f"听说过 React，仓库是 {REPO}。",
    )
    engine._write_ops_sidecar(
        con,
        "proj-repo",
        [
            {
                "op": "update",
                "path": "keys.仓库地址",
                "value": REPO,
                "fact": {"claim": REPO, "evidence": f"听说过 React，仓库是 {REPO}。"},
            }
        ],
        holder=f"distill:{hear['id']}",
    )
    con.commit()
    owner_focus = list(
        con.execute(
            """SELECT text, source_id FROM statements
               WHERE crystal_id='owner'
                 AND (field='当前关注' OR chapter IN ('当前关注与计划','当前关注'))"""
        )
    )
    assert not any("正在推进" in (row["text"] or "") for row in owner_focus)
    assert not any(row["source_id"] in {"eng-repo-only", "eng-hear-react"} for row in owner_focus)

    progressing = _insert_source(
        engine,
        sid="eng-repo-progress",
        kind="user",
        body=f"我在推进 {REPO} 的核心记忆。",
    )
    engine._write_field(
        progressing,
        {
            "field": "仓库地址",
            "value": REPO,
            "quote": f"我在推进 {REPO} 的核心记忆。",
            "crystal": "云岚核忆",
            "type": "工程",
            "trust": "written",
        },
        "user",
        progressing["body"],
    )
    dual = con.execute(
        """SELECT text FROM statements
           WHERE crystal_id='owner' AND source_id='eng-repo-progress'
             AND (field='当前关注' OR chapter IN ('当前关注与计划','当前关注'))
             AND invalid_at IS NULL"""
    ).fetchone()
    assert dual is not None
    assert "正在推进" in (dual["text"] or "")
    assert REPO in (dual["text"] or "")


def test_V54_focus_change_supersedes_immediately(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    first = _insert_source(engine, sid="focus-a", kind="user", body="最近在搞云岚核忆。", event_at=EVENT0)
    engine._write_field(
        first,
        {"field": "当前关注", "value": "最近在搞云岚核忆", "quote": "最近在搞云岚核忆。", "crystal": "记忆主人", "trust": "written"},
        "user",
        "最近在搞云岚核忆。",
    )
    later = (datetime(2026, 1, 5, tzinfo=timezone.utc)).isoformat()
    second = _insert_source(engine, sid="focus-b", kind="user", body="改做南港书院讲义了。", event_at=later)
    engine._write_field(
        second,
        {"field": "当前关注", "value": "改做南港书院讲义", "quote": "改做南港书院讲义了。", "crystal": "记忆主人", "trust": "written"},
        "user",
        "改做南港书院讲义了。",
    )
    old = engine.db().execute(
        "SELECT invalid_at, text FROM statements WHERE text LIKE '%云岚核忆%' AND field='当前关注'"
    ).fetchone()
    new = engine.db().execute(
        "SELECT invalid_at, text FROM statements WHERE text LIKE '%讲义%' AND field='当前关注'"
    ).fetchone()
    assert old is not None and old["invalid_at"]
    assert new is not None and new["invalid_at"] is None
    packed = engine.core_memory(budget=8000, now=later)
    blob = json.dumps(packed, ensure_ascii=False)
    assert "讲义" in blob
    assert "云岚核忆" not in _loaded_text(packed, "当前关注与计划")


def test_soft_ttl_days_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_SOFT_TTL_DAYS", "7")
    engine = _engine(tmp_path, monkeypatch)
    source = _insert_source(engine, sid="ttl-7", kind="user", body=FOCUS_QUOTE, event_at=EVENT0)
    engine._write_field(
        source,
        {"field": "当前关注", "value": "最近在搞云岚核忆", "quote": FOCUS_QUOTE, "crystal": "记忆主人", "trust": "written"},
        "user",
        FOCUS_QUOTE,
    )
    row = engine.db().execute("SELECT valid_until FROM statements WHERE field='当前关注'").fetchone()
    assert row["valid_until"] == "2026-01-08"


def _insert_undated_focus(engine, *, sid: str, text: str, event_at: str) -> None:
    """Simulate legacy stock: current-focus statement with no valid_until."""
    _insert_source(engine, sid=sid, kind="user", body=text + "。", event_at=event_at)
    engine.db().execute(
        """INSERT INTO statements(
             id,crystal_id,field,chapter,text,quote,source_id,trust,rank,event_at,created_at,valid_until
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)""",
        (
            f"st-{sid}",
            "owner",
            "当前关注",
            "当前关注与计划",
            text,
            text + "。",
            sid,
            "written",
            "user",
            event_at,
            event_at,
        ),
    )
    engine.db().commit()


def test_undated_focus_lazy_pack_filters_by_soft_ttl_and_backfills(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _insert_undated_focus(engine, sid="legacy-focus", text="最近在搞南港青石志", event_at=EVENT0)
    before = engine.db().execute(
        "SELECT valid_until, invalid_at FROM statements WHERE id='st-legacy-focus'"
    ).fetchone()
    assert before["valid_until"] in {None, ""}
    assert before["invalid_at"] is None

    engine.core_memory(budget=8000, now=EVENT0)
    engine._backfill_undated_focus_statements(engine.db())
    engine.db().commit()

    filled = engine.db().execute(
        "SELECT valid_until, invalid_at, text FROM statements WHERE id='st-legacy-focus'"
    ).fetchone()
    assert filled["valid_until"] == "2026-01-31"
    assert filled["invalid_at"] is None
    assert filled["text"] == "最近在搞南港青石志"

    later = engine.core_memory(budget=8000, now="2026-02-02T00:00:00+00:00")
    assert "南港青石志" not in json.dumps(later, ensure_ascii=False)
    hist = engine.db().execute(
        "SELECT text, invalid_at FROM statements WHERE id='st-legacy-focus'"
    ).fetchone()
    assert hist["text"] == "最近在搞南港青石志"
    assert hist["invalid_at"] is None


def test_core_alias_single_source_legacy_style_still_maps(tmp_path, monkeypatch):
    from rogmem.distill_gate import CORE_MEMORY_SLICES as GATE_SLICES

    assert _core_canonical("能力与风格") == "沟通与决策风格"
    gate_map = {name: frozenset(aliases) for name, aliases in GATE_SLICES}
    alias_map = {name: frozenset(aliases) for name, aliases in CORE_MODULE_ALIASES.items()}
    assert gate_map == alias_map
    assert "能力与风格" in alias_map["沟通与决策风格"]

    engine = _engine(tmp_path, monkeypatch)
    _set_owner_body(engine, LEGACY_STYLE_BODY)
    packed = engine.core_memory(budget=8000)
    assert "旧库里的沟通决策写法" in _loaded_text(packed, "能力与风格")
