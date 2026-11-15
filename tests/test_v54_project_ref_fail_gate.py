"""v5.4 第 2 刀 project_ref — Draft 注入 + Match/Integrate 对齐已有项目。

行为证据：组装后的 user 文本、library_candidates、词条读回。
禁止 inspect.getsource / 源码 sniff 冒充绿。
prompts 零 diff；≠可上线。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rogmem.crystal_store import CrystalStore
from rogmem.distill_resim import (
    PROJECT_REF_LINE_PREFIX,
    approved_draft_prompts,
    assemble_draft_user,
    fill_draft_user,
    inject_draft_project_ref,
)

pytestmark = pytest.mark.v54_project_ref_fail_gate

REPO = "https://github.com/yunlan/rogmem"
OTHER_REPO = "https://github.com/nangang/lecture"
FOCUS_BODY = "今天把核心记忆的软过期又往前推了一步。"
NO_ENGINE_BODY = "今天只谈天气，不提任何仓库。"
EVENT0 = "2026-01-01T00:00:00+00:00"

PROJECT_DRAFT = {
    "name": "核忆二号",
    "type": "工程",
    "aliases": [],
    "summary": "云岚核忆的后续仓库。",
    "owner_relation": "主人在推进",
    "repo": REPO,
    "body_md": "## 目的目标\n\n今天把核心记忆的软过期又往前推了一步。",
    "facts": [
        {
            "claim": "继续推进核心记忆软过期",
            "evidence": FOCUS_BODY,
        }
    ],
}

ZOMBIE_DRAFT = {
    "name": "随便一个想法",
    "type": "工程",
    "aliases": [],
    "summary": "没有仓库身份的空壳项目。",
    "owner_relation": "暂无信息",
    "body_md": "## 目的目标\n\n今天只谈天气，不提任何仓库。",
    "facts": [
        {
            "claim": "今天只谈天气",
            "evidence": NO_ENGINE_BODY,
        }
    ],
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


def _insert_source(
    engine,
    *,
    sid: str,
    kind: str,
    body: str,
    event_at: str = EVENT0,
    project_ref: str = "",
):
    engine.db().execute(
        """INSERT INTO sources(
             id,kind,turn_id,project_ref,event_at,ingested_at,content_hash,body,status,lane,pair_ready
           ) VALUES(?,?,?,?,?,?,?,?, '已蒸馏','fast',1)""",
        (sid, kind, None, project_ref or None, event_at, event_at, f"hash-{sid}", body),
    )
    engine.db().commit()
    return engine.db().execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()


def _seed_project(engine, *, cid: str, name: str, repo: str) -> None:
    source = _insert_source(engine, sid=f"seed-{cid}", kind="user", body=f"仓库是 {repo}。")
    con = engine.db()
    engine._insert_crystal_page(con, cid, "工程", name, holder=f"distill:{source['id']}")
    con.commit()
    engine._write_field(
        source,
        {
            "field": "仓库地址",
            "value": repo,
            "quote": repo,
            "crystal": name,
            "type": "工程",
            "trust": "written",
        },
        "user",
        source["body"],
    )


def _active_projects(engine):
    return list(
        engine.db().execute(
            "SELECT id, name, type, status FROM crystallizations WHERE type='工程' AND status='active' ORDER BY id"
        )
    )


def _apply_new(engine, source, draft: dict):
    return engine.apply_integrate(
        {"identity": "new", "ops": [], "conflicts": []},
        candidates=[],
        corpus=source["body"] or "",
        store=CrystalStore(engine.db()),
        holder=f"distill:{source['id']}",
        draft_crystal=draft,
    )


def test_PROMPT_V54_PREF_prompts_marked_expired():
    """旧提示稿文首标明过期；过期标记以外的正文仍与当前提交一致。"""
    import subprocess

    from tests.docs_prompts_zero_diff import without_expiry_banner

    root = Path(__file__).resolve().parents[1]
    rel = "docs/prompts/wiki-distill-draft-prompt-v5.md"
    live = (root / rel).read_bytes()
    head = subprocess.check_output(["git", "show", f"HEAD:{rel}"], cwd=root)
    assert live.startswith("> **已过期。**".encode())
    assert without_expiry_banner(live) == head


def test_V54_PREF_01_draft_injects_associated_project_line(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    source = _insert_source(
        engine,
        sid="pref-draft",
        kind="user",
        body=FOCUS_BODY,
        project_ref=REPO,
    )
    inject = engine._prompt_injection(source)
    assert inject["project_ref"] == REPO
    _, tmpl = approved_draft_prompts()
    filled = assemble_draft_user(
        tmpl,
        filename="会话.md",
        material=FOCUS_BODY,
        owner_real_name="云岚甲",
        owner_aliases="测试员甲",
        material_kind="会话",
        project_ref=inject["project_ref"],
    )
    assert f"{PROJECT_REF_LINE_PREFIX}{REPO}" in filled
    assert "{{project_ref}}" not in filled
    bare = fill_draft_user(
        tmpl,
        filename="会话.md",
        material=FOCUS_BODY,
        owner_real_name="云岚甲",
        owner_aliases="测试员甲",
        material_kind="会话",
    )
    assert PROJECT_REF_LINE_PREFIX not in bare
    assert inject_draft_project_ref(bare, "") == bare
    assert inject_draft_project_ref(bare, "   ") == bare

    empty = _insert_source(engine, sid="pref-none", kind="user", body=NO_ENGINE_BODY)
    assert engine._prompt_injection(empty)["project_ref"] == ""
    assert PROJECT_REF_LINE_PREFIX not in assemble_draft_user(
        tmpl,
        filename="会话.md",
        material=NO_ENGINE_BODY,
        owner_real_name="云岚甲",
        owner_aliases="测试员甲",
        material_kind="会话",
        project_ref="",
    )

    seen: dict[str, str] = {}

    def fake_complete(_base, _key, payload, **_kwargs):
        seen["user"] = payload["messages"][1]["content"]
        return json.dumps({"crystals": [PROJECT_DRAFT]}, ensure_ascii=False), "stop"

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m") if role == "distill" else None)
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    raw = engine._llm_draft(
        FOCUS_BODY,
        "user",
        filename="会话.md",
        owner_real_name="云岚甲",
        owner_aliases="测试员甲",
        project_ref=REPO,
    )
    assert raw is not None
    assert f"{PROJECT_REF_LINE_PREFIX}{REPO}" in seen["user"]
    assert "材料类型：会话" in seen["user"]
    assert "{{material}}" not in seen["user"]


def test_V54_PREF_02_match_prefers_existing_project(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _seed_project(engine, cid="proj-yunlan", name="云岚核忆", repo=REPO)
    miss = engine._match_integrate_parts(FOCUS_BODY)
    assert miss == {} or "proj-yunlan" not in {
        item.get("id") for item in (miss.get("library_candidates") or []) if isinstance(item, dict)
    }

    aligned = engine._align_project_candidates(FOCUS_BODY, {"project_ref": REPO})
    ids = [item["id"] for item in aligned["library_candidates"]]
    assert ids[0] == "proj-yunlan"
    assert aligned["library_candidates"][0]["type"] == "工程"
    assert aligned["library_candidates"][0]["name"] == "云岚核忆"

    none = engine._align_project_candidates(NO_ENGINE_BODY, {"project_ref": ""})
    assert none.get("library_candidates") in (None, [])
    unknown = engine._align_project_candidates(NO_ENGINE_BODY, {"project_ref": OTHER_REPO})
    assert unknown.get("library_candidates") in (None, [])
    assert engine._project_library_hits("", NO_ENGINE_BODY) == []


def test_V54_PREF_03_integrate_aligns_existing_project(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _seed_project(engine, cid="proj-yunlan", name="云岚核忆", repo=REPO)
    source = _insert_source(
        engine,
        sid="pref-align",
        kind="user",
        body=FOCUS_BODY,
        project_ref=REPO,
    )
    result = _apply_new(engine, source, PROJECT_DRAFT)
    assert result.decision == "accept"
    assert result.matched_id == "proj-yunlan"
    rows = _active_projects(engine)
    assert [row["id"] for row in rows] == ["proj-yunlan"]
    assert rows[0]["name"] == "云岚核忆"
    body = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='proj-yunlan'").fetchone()["body_md"]
    assert "软过期" in (body or "")

    seen: dict = {}

    def fake_complete(_base, _key, payload, **_kwargs):
        system = payload["messages"][0]["content"]
        user = payload["messages"][1]["content"]
        if "蒸馏写作者" in system:
            seen["draft"] = user
            return json.dumps({"crystals": [PROJECT_DRAFT]}, ensure_ascii=False), "stop"
        seen["integrate"] = user
        return json.dumps(
            {
                "identity": "same",
                "matched_id": "proj-yunlan",
                "ops": [
                    {
                        "op": "update",
                        "path": "summary",
                        "value": "继续推进核心记忆。",
                        "fact": {"claim": "继续推进", "evidence": FOCUS_BODY},
                    }
                ],
                "conflicts": [],
            },
            ensure_ascii=False,
        ), "stop"

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m") if role == "distill" else None)
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    inject = engine._prompt_injection(source)
    data = engine._produce_distill_payload(FOCUS_BODY, "user", **{**inject, **engine._match_integrate_parts(FOCUS_BODY)})
    assert data.get("identity") == "same"
    assert data.get("matched_id") == "proj-yunlan"
    assert f"{PROJECT_REF_LINE_PREFIX}{REPO}" in seen["draft"]
    assert "proj-yunlan" in seen["integrate"]
    assert _active_projects(engine)[0]["id"] == "proj-yunlan"


def test_V54_PREF_04_no_project_ref_does_not_mint_zombie(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    source = _insert_source(engine, sid="pref-zombie", kind="user", body=NO_ENGINE_BODY)
    result = _apply_new(engine, source, ZOMBIE_DRAFT)
    # 模型标成项目就按项目留下，不因没有仓库地址改成别的类型。
    assert result.decision == "accept"
    products = list(
        engine.db().execute(
            "SELECT name, type, status FROM crystallizations WHERE name=? AND status='active'",
            (ZOMBIE_DRAFT["name"],),
        )
    )
    assert len(products) == 1 and products[0]["type"] == "工程"
    assert engine._project_library_hits("", NO_ENGINE_BODY) == []
    aligned = engine._align_project_candidates(NO_ENGINE_BODY, engine._prompt_injection(source))
    assert aligned.get("library_candidates") in (None, [])

    engine._write_field(
        source,
        {
            "field": "当前关注",
            "value": "今天只谈天气",
            "quote": NO_ENGINE_BODY,
            "crystal": "记忆主人",
            "trust": "written",
        },
        "user",
        NO_ENGINE_BODY,
    )
    assert [row["name"] for row in _active_projects(engine)] == [ZOMBIE_DRAFT["name"]]
