"""Knife B 厚读清扫 Fail 闸（AB-B-*）。

断言是行为结果：get/hydrate/crystal_detail/read_section 读回、revision、厚列仍在库。
禁止用「文件存在」冒充绿。inventory 用 AST 列生产调用点，不拿 inspect.getsource 当读路径绿。
本刀不改 Draft / Integrate / land 写缝；勿称全库零厚写；上线仍卡 wipe。
"""

from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path

import pytest

from rogmem.crystal_store import (
    DISTILLER_STORE_WRITE_ENABLED,
    CrystalStore,
    Fact,
    IntegrateWriteNotLive,
    ThickReadDeprecated,
    ThinCrystal,
    assert_distiller_write_not_flipped,
    hydrate,
    migrate_crystal_store,
    project_thick,
    require_thin_write,
)
from tests.docs_prompts_zero_diff import assert_docs_prompts_zero_diff, default_repo

pytestmark = pytest.mark.thick_sweep_fail_gate

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "rogmem" / "schema.sql").read_text(encoding="utf-8")
WIPE_SQL = ROOT / "scripts" / "wipe_thick_blobs.sql"
PRODUCTION_SKIP_DIRS = frozenset({"vendor", "__pycache__", "tests"})

# 写缝里仍可能读 summary_json / chapters_json 当「当前值」。那不是生产读入口，
# 本刀不改 Draft / Integrate / land / _write_*。inventory 不计这些。
WRITE_SEAM_THICK_COLUMN_LOOKUPS = (
    "_require_publish_sheet",
    "_write_chapter",
    "_write_field",
    "_apply_rejudge_update",
    "_chapters_map",
)

THICK_OVERVIEW = "北窗实验室的人必须被读路径忽略。"
THICK_CHAPTER = "2019 年在北窗实验室任职，这段厚章节不得冒充生产读。"
THIN_OVERVIEW = "只认瘦信封摘要。"
THIN_BODY = "## 事业经历\n\n瘦正文，不拼厚栏。"


def _con(tmp_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(tmp_path / "crystal.sqlite")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    migrate_crystal_store(con)
    return con


def _insert_thick_only(con: sqlite3.Connection, cid: str = "p-厚页") -> None:
    summary = json.dumps(
        {
            "名称": "云岚测试员甲",
            "别名": "测试员甲",
            "概述": THICK_OVERVIEW,
            "与记忆主人的关系": "同事",
            "所属组织及职务": "北窗实验室 研究员",
        },
        ensure_ascii=False,
    )
    chapters = json.dumps({"事业经历": THICK_CHAPTER}, ensure_ascii=False)
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,revision) "
        "VALUES(?,?,?,'active',?,?,0)",
        (cid, "人物", "云岚测试员甲", summary, chapters),
    )
    con.commit()


def _engine(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _production_py_files() -> list[Path]:
    files = []
    for path in (ROOT / "rogmem").rglob("*.py"):
        if any(part in PRODUCTION_SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def _project_thick_calls(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = ""
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name == "project_thick":
            hits.append((path.relative_to(ROOT).as_posix(), node.lineno))
    return hits


def test_ab_b_thick_read_entrypoints_inventory_zero_or_deprecated():
    """AB-B-INV-01: 生产调用 project_thick = 0；tombstone 显式废弃。"""
    leftover = []
    for path in _production_py_files():
        leftover.extend(_project_thick_calls(path))
    assert leftover == [], f"live project_thick calls remain: {leftover}"
    assert project_thick.__doc__
    assert "Deprecated" in (project_thick.__doc__ or "")
    with pytest.raises(ThickReadDeprecated):
        project_thick(
            {
                "id": "x",
                "type": "人物",
                "name": "甲",
                "status": "active",
                "revision": 0,
                "summary_json": json.dumps({"概述": THICK_OVERVIEW}, ensure_ascii=False),
                "chapters_json": json.dumps({"事业经历": THICK_CHAPTER}, ensure_ascii=False),
            }
        )


def test_ab_b_hydrate_thick_fallback_gone_or_deprecated(tmp_path):
    """AB-B-INV-02: 仅有厚列时 get/hydrate 不展开厚章节，不返 storage=thick。"""
    con = _con(tmp_path)
    _insert_thick_only(con)
    row = con.execute("SELECT * FROM crystallizations WHERE id='p-厚页'").fetchone()
    loaded = hydrate(row)
    got = CrystalStore(con).get("p-厚页")
    assert loaded is not None and got is not None
    assert loaded.storage == "thin"
    assert got.storage == "thin"
    assert THICK_CHAPTER not in (loaded.body_md or "")
    assert THICK_CHAPTER not in (got.body_md or "")
    assert THICK_OVERVIEW not in (loaded.summary or "")
    assert THICK_OVERVIEW not in (got.summary or "")
    assert loaded.facts == []
    assert got.facts == []


def test_ab_b_no_production_project_thick_calls():
    """AB-B-INV-03: engine / 生产模块无 project_thick( 调用。"""
    leftover = []
    for path in _production_py_files():
        leftover.extend(_project_thick_calls(path))
    assert leftover == []
    engine_src = (ROOT / "rogmem" / "engine.py").read_text(encoding="utf-8")
    assert "project_thick(" not in engine_src


def test_ab_b_thin_row_reads_thin_only(tmp_path):
    """AB-B-READ-01: 薄行正文来自 body_md / envelope，不拼 chapters_json。"""
    con = _con(tmp_path)
    store = CrystalStore(con)
    store.put(
        ThinCrystal(
            id="p-瘦",
            type="人物",
            name="云岚测试员甲",
            status="active",
            revision=0,
            summary=THIN_OVERVIEW,
            aliases=["瘦别名"],
            body_md=THIN_BODY,
            facts=[Fact(claim="瘦事实", evidence="源句")],
        )
    )
    con.execute(
        "UPDATE crystallizations SET chapters_json=?, summary_json=? WHERE id='p-瘦'",
        (
            json.dumps({"事业经历": THICK_CHAPTER}, ensure_ascii=False),
            json.dumps({"概述": THICK_OVERVIEW, "别名": "厚别名"}, ensure_ascii=False),
        ),
    )
    con.commit()
    got = store.get("p-瘦")
    assert got is not None
    assert got.storage == "thin"
    assert got.summary == THIN_OVERVIEW
    assert got.body_md == THIN_BODY
    assert THICK_CHAPTER not in got.body_md
    assert THICK_OVERVIEW not in got.summary
    assert got.aliases == ["瘦别名"]
    assert any(item.claim == "瘦事实" and item.evidence == "源句" for item in got.facts)


def test_ab_b_read_path_does_not_mutate_store(tmp_path, monkeypatch):
    """AB-B-READ-02: get / hydrate / 页面读 不 put、不 apply、不改 revision。"""
    engine = _engine(tmp_path, monkeypatch)
    store = CrystalStore(engine.db())
    store.put(
        ThinCrystal(
            id="p-读",
            type="人物",
            name="南港讲师乙",
            status="active",
            revision=0,
            summary=THIN_OVERVIEW,
            body_md=THIN_BODY,
        )
    )
    writes: list[str] = []
    orig_put = store.put
    orig_apply = store.apply_ops

    def spy_put(*args, **kwargs):
        writes.append("put")
        return orig_put(*args, **kwargs)

    def spy_apply(*args, **kwargs):
        writes.append("apply_ops")
        return orig_apply(*args, **kwargs)

    store.put = spy_put  # type: ignore[method-assign]
    store.apply_ops = spy_apply  # type: ignore[method-assign]
    before = engine.db().execute(
        "SELECT revision, envelope_json, body_md, summary_json, chapters_json FROM crystallizations WHERE id='p-读'"
    ).fetchone()
    row = engine.db().execute("SELECT * FROM crystallizations WHERE id='p-读'").fetchone()
    assert hydrate(row).storage == "thin"
    assert store.get("p-读") is not None
    detail = engine.crystal_detail("p-读")
    assert detail is not None
    section = engine.read_section("南港讲师乙", "事业经历")
    assert section["missing"] is False
    packed = engine._library_for_match()
    assert any(item["id"] == "p-读" for item in packed)
    after = engine.db().execute(
        "SELECT revision, envelope_json, body_md, summary_json, chapters_json FROM crystallizations WHERE id='p-读'"
    ).fetchone()
    assert writes == []
    assert after["revision"] == before["revision"]
    assert after["envelope_json"] == before["envelope_json"]
    assert after["body_md"] == before["body_md"]
    assert after["summary_json"] == before["summary_json"]
    assert after["chapters_json"] == before["chapters_json"]


def test_ab_b_thick_blobs_may_remain_until_wipe(tmp_path):
    """AB-B-READ-03 / VOICE-02: 读清扫 ≠ wipe；厚列仍在，上线仍卡 wipe 脚本。"""
    con = _con(tmp_path)
    _insert_thick_only(con)
    got = CrystalStore(con).get("p-厚页")
    row = con.execute(
        "SELECT summary_json, chapters_json FROM crystallizations WHERE id='p-厚页'"
    ).fetchone()
    assert got is not None
    assert got.storage == "thin"
    assert THICK_OVERVIEW in (row["summary_json"] or "")
    assert THICK_CHAPTER in (row["chapters_json"] or "")
    assert WIPE_SQL.is_file()
    wipe = WIPE_SQL.read_text(encoding="utf-8")
    assert "UPDATE crystallizations SET summary_json='{}', chapters_json='{}'" in wipe
    assert "零厚写" not in wipe or "勿称" in wipe


def test_ab_b_engine_page_read_does_not_surface_thick_only_blob(tmp_path, monkeypatch):
    """生产页面读口：仅有厚列时不把厚章节/概述当业务正文。"""
    engine = _engine(tmp_path, monkeypatch)
    engine.db().execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,revision) "
        "VALUES('p-页','人物','云岚测试员甲','active',?,?,0)",
        (
            json.dumps({"概述": THICK_OVERVIEW, "别名": "厚别名"}, ensure_ascii=False),
            json.dumps({"事业经历": THICK_CHAPTER}, ensure_ascii=False),
        ),
    )
    engine.db().commit()
    detail = engine.crystal_detail("p-页")
    assert detail is not None
    assert THICK_OVERVIEW not in (detail["summary"].get("概述") or "")
    assert "厚别名" not in (detail["summary"].get("别名") or "")
    assert THICK_CHAPTER not in (detail["chapters"].get("事业经历") or "")
    section = engine.read_section("云岚测试员甲", "事业经历")
    assert THICK_CHAPTER not in (section.get("text") or "")
    packed = engine._library_for_match()
    hit = next(item for item in packed if item["id"] == "p-页")
    assert THICK_CHAPTER not in (hit.get("body_md") or "")
    assert THICK_OVERVIEW not in (hit.get("summary") or "")


def test_ab_b_write_switch_semantics_unchanged(write_switch_off):
    """AB-B-SEAM-02: 写开关语义未偷改；关则 fail-closed。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    with pytest.raises(IntegrateWriteNotLive):
        assert_distiller_write_not_flipped()
    with pytest.raises(IntegrateWriteNotLive):
        require_thin_write()


def test_ab_b_docs_prompts_git_diff_empty():
    """AB-B-PROMPT-01: 真 git diff docs/prompts 为空。"""
    assert_docs_prompts_zero_diff()
    repo = default_repo()
    result = __import__("subprocess").run(
        ["git", "diff", "origin/release/2.0...HEAD", "--", "docs/prompts"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_ab_b_write_seam_symbols_still_present_untouched():
    """AB-B-SEAM-01 旁证：写缝符号仍在；本测不改它们，也不把它们算成厚读入口。"""
    from rogmem import engine

    for name in (
        "apply_integrate",
        "_land_integrate_new",
        "_draft_thin_ops",
        "_with_approved_draft",
        *WRITE_SEAM_THICK_COLUMN_LOOKUPS,
    ):
        assert hasattr(engine, name), name
        assert callable(getattr(engine, name))


def test_ab_b_write_seam_chapters_map_leftover_is_not_page_read(tmp_path, monkeypatch):
    """写缝 leftover `_chapters_map` 仍可读厚列；页面读口不得跟着展开。"""
    engine = _engine(tmp_path, monkeypatch)
    engine.db().execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,revision) "
        "VALUES('proj-leftover','工程','云岚仓','active','{}',?,0)",
        (json.dumps({"计划中特性": "云岚检索", "开发中特性": "暂无来源", "发版日志": "暂无来源"}, ensure_ascii=False),),
    )
    engine.db().commit()
    row = engine.db().execute("SELECT * FROM crystallizations WHERE id='proj-leftover'").fetchone()
    leftover = engine._chapters_map(row)
    assert leftover.get("计划中特性") == "云岚检索"
    detail = engine.crystal_detail("proj-leftover")
    assert "云岚检索" not in (detail["chapters"].get("计划中特性") or "")
    section = engine.read_section("云岚仓", "计划中特性")
    assert "云岚检索" not in (section.get("text") or "")
