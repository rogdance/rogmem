"""读路径不为「恒为空的遗留增强」付全表扫描的代价。

背景：read_section 曾无条件执行
    SELECT ... FROM chunks WHERE role='parent' AND embed_text LIKE '%标题：X%'
但 parent chunk 插入时从不写 embed_text，这个查询既不走索引、结果又恒为空。
数据规模越大，宿主读一次章节越慢。修复把「有没有可检索的 parent 文本」
变成一个走部分索引的 O(1) 能力探测，只有确实存在时才付扫描代价。
"""

from __future__ import annotations

from rogmem.crystal_store import CrystalStore, ThinCrystal


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.delenv("ROGMEM_LLM_BASE", raising=False)
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _page(engine):
    CrystalStore(engine.db()).put(
        ThinCrystal(
            id="lighthouse-page",
            type="概念",
            name="北港灯塔",
            status="active",
            revision=0,
            body_md="## 雾季\n\n北港灯塔在雾季用 LIGHTHOUSEMARK 指路。\n\n## 维护\n\n每年检修一次。",
        )
    )
    engine.db().commit()


def _parent_rows(con, count: int, embed: str | None = None) -> None:
    for seq in range(count):
        con.execute(
            """INSERT INTO chunks(source_id,seq,loc,text,role,embed_text)
               VALUES(?,?,?,?,'parent',?)""",
            (f"src-{seq}", seq, f"{seq}-{seq + 10}",
             f"标题：雾季\n第 {seq} 段来源材料，用于验证读路径没有被全表扫描拖住。",
             embed),
        )
    con.commit()


def test_connect_installs_the_partial_index(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    names = {row[1] for row in engine.db().execute("PRAGMA index_list(chunks)")}
    assert "chunks_parent_embed" in names


def test_read_section_never_scans_chunks_when_no_parent_text_is_searchable(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    _page(engine)
    _parent_rows(con, 400)  # 当前写入模型：parent 行的 embed_text 为 NULL

    statements: list[str] = []
    con.set_trace_callback(statements.append)
    try:
        result = engine.read_section("北港灯塔", "雾季")
    finally:
        con.set_trace_callback(None)

    assert result["missing"] is False
    assert "LIGHTHOUSEMARK" in result["text"]
    assert result["sections"] == ["雾季", "维护"]
    assert result["parents"] == []
    assert not any("embed_text LIKE" in sql for sql in statements), statements
    assert any("embed_text IS NOT NULL" in sql for sql in statements), statements


def test_legacy_parent_text_still_enriches_the_read(tmp_path, monkeypatch):
    """老库真的写过 parent 检索文本时，增强行为与修复前一致。"""
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    _page(engine)
    _parent_rows(con, 2, embed="标题：雾季\n来自旧版写入器的检索文本。")

    result = engine.read_section("北港灯塔", "雾季")

    assert result["missing"] is False
    assert result["parents"], "有可检索的 parent 文本时，不该把增强一并丢掉"
    assert any("标题：雾季" in piece for piece in result["parents"])


def test_missing_heading_still_returns_the_heading_inventory(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _page(engine)
    result = engine.read_section("北港灯塔", "不存在的章节")
    assert result["missing"] is True
    assert result["sections"] == ["雾季", "维护"]
