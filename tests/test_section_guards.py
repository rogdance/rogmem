"""并页增量合并的段级护栏：未命中段不变校验 + 段级字符上限。

只覆盖 apply_ops 这条唯一写入漏斗（并页 / 手改章节都走它）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rogmem.crystal_store import (
    CrystalStore,
    SectionIsolationViolation,
    SectionTooLong,
    ThinCrystal,
    migrate_crystal_store,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "rogmem" / "schema.sql").read_text(encoding="utf-8")


def _con(tmp_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(tmp_path / "crystal.sqlite")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    migrate_crystal_store(con)
    return con


def _setting(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value),
    )
    con.commit()


def _page(store: CrystalStore, body: str) -> None:
    store.put(
        ThinCrystal(id="x", type="人物", name="甲", status="active", revision=0, body_md=body)
    )
    store.acquire_lease("x", "integrate-1")


def test_single_section_update_keeps_other_sections(tmp_path):
    store = CrystalStore(_con(tmp_path))
    _page(store, "## 现状\n\n旧现状\n\n## 其他\n\n别的")
    store.apply_ops(
        "x", 0, [{"op": "update", "path": "body_md##现状", "value": "新现状"}], holder="integrate-1"
    )
    got = store.get("x")
    assert "新现状" in got.body_md
    assert "## 其他\n\n别的" in got.body_md
    assert "旧现状" not in got.body_md


def test_isolation_guard_blocks_collateral_section_change(tmp_path):
    store = CrystalStore(_con(tmp_path))
    # 标题「现状」是「现状分析」的前缀：朴素替换会连带吃掉「现状分析」整段。
    _page(store, "## 现状分析\n\n旧A\n\n## 其他\n\n旧B")
    with pytest.raises(SectionIsolationViolation):
        store.apply_ops(
            "x", 0, [{"op": "update", "path": "body_md##现状", "value": "新"}], holder="integrate-1"
        )
    got = store.get("x")
    assert got.body_md == "## 现状分析\n\n旧A\n\n## 其他\n\n旧B"
    assert got.revision == 0


def test_isolation_guard_can_be_turned_off(tmp_path):
    con = _con(tmp_path)
    _setting(con, "section_isolation_guard", "0")
    store = CrystalStore(con)
    _page(store, "## 现状分析\n\n旧A\n\n## 其他\n\n旧B")
    store.apply_ops(
        "x", 0, [{"op": "update", "path": "body_md##现状", "value": "新"}], holder="integrate-1"
    )
    assert store.get("x").revision == 1


def test_section_cap_off_by_default(tmp_path):
    store = CrystalStore(_con(tmp_path))
    _page(store, "## 大段\n\n" + "字" * 5000)
    store.apply_ops(
        "x", 0, [{"op": "update", "path": "body_md##大段", "value": "字" * 6000}], holder="integrate-1"
    )
    assert store.get("x").revision == 1


def test_section_cap_rejects_over_long_section(tmp_path):
    con = _con(tmp_path)
    _setting(con, "section_char_limit", "100")
    store = CrystalStore(con)
    _page(store, "## 小段\n\n短\n\n## 大段\n\n" + "字" * 50)
    with pytest.raises(SectionTooLong):
        store.apply_ops(
            "x",
            0,
            [{"op": "update", "path": "body_md##大段", "value": "字" * 200}],
            holder="integrate-1",
        )
    got = store.get("x")
    assert got.revision == 0
    assert "字" * 200 not in got.body_md


def test_new_section_appended_passes_isolation(tmp_path):
    store = CrystalStore(_con(tmp_path))
    _page(store, "## 现状\n\n旧现状")
    store.apply_ops(
        "x", 0, [{"op": "add", "path": "body_md##补充", "value": "新段"}], holder="integrate-1"
    )
    got = store.get("x")
    assert "旧现状" in got.body_md
    assert "## 补充" in got.body_md
    assert "新段" in got.body_md


def test_first_section_into_empty_body_passes_isolation(tmp_path):
    # 空正文写第一段不能被隔离护栏误拦（_body_sections 对空正文给出 ("", "")）。
    store = CrystalStore(_con(tmp_path))
    _page(store, "")
    store.apply_ops(
        "x",
        0,
        [{"op": "update", "path": "body_md##事业经历", "value": "在书院做讲师"}],
        holder="integrate-1",
    )
    got = store.get("x")
    assert got.revision == 1
    assert "## 事业经历" in got.body_md and "在书院做讲师" in got.body_md


def test_section_index_not_rebuilt_when_body_unchanged(tmp_path, monkeypatch):
    import rogmem.section_index as si

    calls: list[str] = []
    real = si.sync_sections

    def spy(con, cid, body):
        calls.append(cid)
        return real(con, cid, body)

    monkeypatch.setattr(si, "sync_sections", spy)
    store = CrystalStore(_con(tmp_path))
    store.put(ThinCrystal(id="x", type="人物", name="甲", status="active", revision=0, body_md="## A\n\n1"))
    assert calls == ["x"]
    got = store.get("x")
    got.body_md = "## A\n\n1"  # 正文不变，仅再落一次
    store.put(got)
    assert calls == ["x"]
