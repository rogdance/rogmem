"""段级索引与规则路由（并页增量合并缺口③）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rogmem import engine
from rogmem.crystal_store import CrystalStore, ThinCrystal, migrate_crystal_store
from rogmem.section_index import (
    body_terms,
    ensure_index_current,
    index_rows,
    load_body_term_sets,
    pick_sections,
    rebuild_section_index,
    split_sections,
    sync_sections,
    tokens_to_terms,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "rogmem" / "schema.sql").read_text(encoding="utf-8")


def _con(tmp_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(tmp_path / "crystal.sqlite")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    migrate_crystal_store(con)
    return con


def test_split_sections_keeps_preamble_and_headings():
    body = "开篇一句\n\n## 现状\n\n旧现状\n\n## 其他\n\n别的"
    assert split_sections(body) == [
        ("", "开篇一句\n\n"),
        ("现状", "## 现状\n\n旧现状\n\n"),
        ("其他", "## 其他\n\n别的"),
    ]


def test_pick_sections_routes_by_term_overlap():
    sections = split_sections("## 部署配置\n\nnas_path 回退\n\n## 图谱\n\nthree.js 渲染体")
    picked = pick_sections("本轮新增 nas_path 与回退逻辑", sections)
    assert [heading for heading, _ in picked] == ["部署配置"]


def test_sync_and_rebuild_section_index(tmp_path):
    con = _con(tmp_path)
    store = CrystalStore(con)
    store.put(
        ThinCrystal(id="x", type="产品", name="甲", status="active", revision=0,
                    body_md="## 现状\n\n内容\n\n## 规划\n\n待做")
    )
    rows = con.execute("SELECT heading FROM crystal_sections WHERE crystal_id='x' ORDER BY ord").fetchall()
    assert [row["heading"] for row in rows] == ["现状", "规划"]
    con.execute("DELETE FROM crystal_sections")
    assert rebuild_section_index(con) >= 1
    assert con.execute("SELECT COUNT(*) AS n FROM crystal_sections WHERE crystal_id='x'").fetchone()["n"] == 2


def _pack(body: str) -> dict:
    return {"id": "cry_x", "type": "产品", "name": "甲", "aliases": [], "summary": "",
            "body_md": body, "facts": [], "key_fields": {}, "body_injection": "full"}


def test_sectioned_pack_leaves_short_body_alone():
    pack = _pack("## 现状\n\n短正文")
    assert engine._sectioned_pack(pack, "无关材料") is pack


def test_sectioned_pack_reduces_long_body_to_routed_sections():
    routed = "## 部署配置\n\n" + "nas_path 回退逻辑说明。" * 300
    other = "## 图谱渲染\n\n" + "three.js 渲染体细节说明。" * 300
    pack = _pack(routed + "\n\n" + other)
    assert len(pack["body_md"]) > engine.SECTION_INJECT_LIMIT
    reduced = engine._sectioned_pack(pack, "本轮涉及 nas_path 回退")
    assert reduced["body_injection"] == "sectioned"
    assert "nas_path 回退逻辑说明" in reduced["body_md"]
    assert "## 其余小节（只给目录）" in reduced["body_md"]
    assert "- 图谱渲染：" in reduced["body_md"]
    # 其他段只给短摘要，不给整段正文：整体显著变小
    assert len(reduced["body_md"]) < len(pack["body_md"]) // 2


def test_sectioned_pack_falls_back_when_nothing_matches():
    other = "## 图谱渲染\n\n" + "three.js 渲染体细节。" * 300
    pack = _pack(other)
    assert engine._sectioned_pack(pack, "完全无关的材料词") is pack


def test_short_heading_beats_long_body_match():
    sections = [
        ("本机 hook 更新", "这里顺带提到验证，还有别的"),
        ("验证", "healthz version embed fastembed 逐项核对"),
    ]
    picked = pick_sections("验证 healthz version embed fastembed", sections)
    assert [heading for heading, _ in picked][0] == "验证"


def test_outline_has_short_gist_not_only_title():
    injected = "## 部署配置\n\n" + "nas_path 回退说明。" * 800
    other = "## 图谱渲染\n\nthree.js 渲染体的细节说明文字。"
    pack = _pack(injected + "\n\n" + other)
    reduced = engine._sectioned_pack(pack, "本轮涉及 nas_path")
    assert "- 图谱渲染：" in reduced["body_md"]
    assert "three.js" in reduced["body_md"]


def test_all_heading_matches_injected_beyond_limit():
    body = "".join(f"## 验证 {i}\n\n正文{i}\n\n" for i in range(9)) + "## 无关\n\n" + "填充。" * 4000
    pack = _pack(body)
    reduced = engine._sectioned_pack(pack, "验证")
    for i in range(9):
        assert f"## 验证 {i}" in reduced["body_md"]


def test_single_char_tokens_are_not_match_signals():
    from rogmem.section_index import terms

    assert terms("面 体 的 了 甲") == set()
    assert terms("端口") == {"端口"}


def test_unrelated_material_does_not_route_via_single_char():
    sections = split_sections("## 核心机制\n\n正文若干。")
    assert pick_sections("甲 乙 面 体 的", sections) == []


def test_stored_body_terms_route_same_as_live():
    """落档词复用必须与现算同分——否则复用索引就是偷偷改了路由。"""
    body = (
        "## 部署配置\n\nnas_path 回退逻辑说明，含端口与挂载。\n\n"
        "## 图谱渲染\n\nthree.js 渲染体细节说明。\n\n"
        "## 验证\n\nhealthz version embed 逐项核对。"
    )
    sections = split_sections(body)
    stored = [tokens_to_terms(row["tokens"]) for row in index_rows(body)]
    for material in (
        "本轮涉及 nas_path 回退",
        "验证 healthz version",
        "three.js 渲染体",
        "完全无关的材料词",
    ):
        assert pick_sections(material, sections) == pick_sections(
            material, sections, body_sets=stored
        )


def test_heading_words_are_not_counted_as_body_terms():
    block = "## 部署配置\n\n部署配置里的 nas_path 回退"
    got = body_terms("部署配置", block)
    # 标题词不进正文词集（否则与标题命中重复计分）
    assert "配置" not in got
    assert "回退" in got


def test_load_body_term_sets_only_when_body_matches(tmp_path):
    con = _con(tmp_path)
    body = "## 现状\n\n内容\n\n## 规划\n\n待做"
    sync_sections(con, "x", body)
    sections = split_sections(body)
    got = load_body_term_sets(con, "x", sections)
    assert got is not None and len(got) == len(sections)
    # 正文改了而索引还没重建：宁可交给调用方现算，也不返回错的词集。
    assert load_body_term_sets(con, "x", split_sections(body + "\n\n## 新增\n\n更多")) is None
    assert load_body_term_sets(con, "missing", sections) is None


def test_ensure_index_current_rebuilds_only_once(tmp_path):
    con = _con(tmp_path)
    store = CrystalStore(con)
    store.put(
        ThinCrystal(id="x", type="产品", name="甲", status="active", revision=0,
                    body_md="## 现状\n\n内容")
    )
    assert ensure_index_current(con) is True
    assert con.execute("SELECT COUNT(*) AS n FROM crystal_sections WHERE crystal_id='x'").fetchone()["n"] == 1
    con.execute("DELETE FROM crystal_sections")
    # 口径版本没变：不再全量重建（这正是「每次启动都重建」的替代）。
    assert ensure_index_current(con) is False
    assert con.execute("SELECT COUNT(*) AS n FROM crystal_sections").fetchone()["n"] == 0
