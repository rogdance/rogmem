"""WeKnora chunker invariants: overlap, protected spans, and parent chunks.

B-OV / B-PC are the QA B-gate names for overlap and parent-child.
"""

import inspect
from pathlib import Path

import pytest

from rogmem.chunker import (
    CHILD_SIZE,
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    ChunkerConfigError,
    resolve_chunk_params,
    shared_boundary,
    split_headed,
    split_parent_child,
    split_text,
    uncovered_span,
)


def test_consecutive_chunks_overlap_on_a_boundary():
    sentence = "甲" * 40 + "。\n"
    text = sentence * 12
    chunks = split_text(text, chunk_size=180, overlap=50)
    assert len(chunks) >= 2
    shared = chunks[0].content[-20:]
    assert shared
    assert shared in chunks[1].content[:80]


def test_fenced_code_is_not_cut_apart():
    code = "```\n" + ("代码" * 80) + "\n```"
    text = "前言。\n" + code + "\n后记。"
    chunks = split_text(text, chunk_size=40, overlap=8)
    holders = [chunk.content for chunk in chunks if "代码" in chunk.content]
    assert len(holders) == 1
    assert code in holders[0]


def test_headings_split_sections_and_tiny_ones_merge():
    text = "# 甲店\n" + ("周生生金店在香港开业。" * 8) + "\n# 乙厂\n短\n# 丙厂\n" + ("捷安特自行车在台湾生产。" * 8)
    parents, children = split_headed(text, parent_size=400, child_size=120)
    assert children
    assert not any("周生生" in child.content and "捷安特" in child.content for child in children)
    assert any("短" in child.content and "周生生" in child.content for child in children)


def test_heading_inside_a_fence_does_not_split_the_fence():
    code = "```\n# 不要当标题\n" + ("代码" * 40) + "\n```"
    text = "# 甲\n" + ("前言。" * 20) + "\n" + code + "\n# 乙\n" + ("后记。" * 20)
    _parents, children = split_headed(text, parent_size=200, child_size=80)
    holders = [child.content for child in children if "代码" in child.content]
    assert len(holders) == 1
    assert code in holders[0]


def test_parent_holds_children_and_children_are_smaller():
    text = ("段落" * 180 + "。\n") * 4
    parents, children = split_parent_child(text, parent_size=300, child_size=80)
    assert parents
    assert children
    linked = [child for child in children if child.parent_index is not None]
    assert linked
    for child in linked:
        parent = parents[child.parent_index]
        assert len(parent.content) > len(child.content)
        assert child.content in parent.content
        assert child.embedding_content() == child.content.strip()


def test_file_index_keeps_code_together_and_skips_parent_vectors(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    code = "```\n" + ("代码" * 80) + "\n```"
    text = "前言。\n" + code + "\n" + ("后记。" * 80)
    engine._index_text(engine.db(), "src", text, "笔记.md")
    engine.db().commit()
    rows = list(engine.db().execute("SELECT role, text, vec_json, embed_text FROM chunks WHERE source_id='src'"))
    parents = [row for row in rows if row["role"] == "parent"]
    children = [row for row in rows if row["role"] == "child"]
    assert children
    assert all(row["vec_json"] is None for row in parents)
    holders = [row["text"] for row in children if "代码" in row["text"]]
    assert len(holders) == 1
    assert code in holders[0]
    assert all("文件：" not in row["text"] for row in children)
    assert any((row["embed_text"] or "").startswith("文件：笔记.md") for row in children)


def test_b_ov_defaults_are_512_80():
    """B-OV: public defaults stay chunk=512 / overlap=80."""
    assert CHUNK_SIZE == 512
    assert CHUNK_OVERLAP == 80
    assert CHILD_SIZE == 512
    text = "这是一句用来切开的测试正文。" * 80
    chunks = split_text(text)
    assert len(chunks) >= 2
    for left, right in zip(chunks, chunks[1:]):
        shared = shared_boundary(left.content, right.content)
        assert shared
        assert len(shared) >= 1


def test_b_ov_adjacent_chunks_share_real_overlap_without_separators():
    """B-OV: overlap is a real shared span, including texts with no sentence break."""
    text = "甲" * 1800
    chunks = split_text(text)
    assert len(chunks) >= 2
    for left, right in zip(chunks, chunks[1:]):
        shared = shared_boundary(left.content, right.content)
        assert shared
        assert len(shared) >= CHUNK_OVERLAP


def test_b_ov_rejects_illegal_overlap():
    """B-OV: overlap 0, negative, or ≥ chunk size is rejected, not clamped."""
    with pytest.raises(ChunkerConfigError):
        resolve_chunk_params(512, 0)
    with pytest.raises(ChunkerConfigError):
        split_text("甲" * 80, chunk_size=512, overlap=0)
    with pytest.raises(ChunkerConfigError):
        split_text("甲" * 80, chunk_size=512, overlap=512)
    with pytest.raises(ChunkerConfigError):
        split_text("甲" * 80, chunk_size=512, overlap=-1)
    with pytest.raises(ChunkerConfigError):
        split_text("甲" * 80, chunk_size=80, overlap=80)
    with pytest.raises(ChunkerConfigError):
        split_text("甲" * 80, chunk_size=0, overlap=0)
    assert resolve_chunk_params() == (512, 80)
    assert resolve_chunk_params(512, 80) == (512, 80)


def test_b_ov_engine_ingest_uses_default_chunker(tmp_path: Path, monkeypatch):
    """B-OV: engine leftover 600/100 and max(end-100,end) paths are gone."""
    from rogmem import engine

    source = inspect.getsource(engine._chunk)
    assert "600" not in source
    assert "100" not in source
    assert "split_text(text)" in source
    chunker_src = Path("rogmem/chunker.py").read_text(encoding="utf-8")
    assert "max(end - 100, end)" not in chunker_src
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    path = tmp_path / "scan" / "long.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("这是一句用来切开的测试正文。" * 80, encoding="utf-8")
    opened = engine.ingest_file(path)
    children = list(
        engine.db().execute(
            "SELECT text FROM chunks WHERE source_id=? AND role='child' ORDER BY seq",
            (opened["id"],),
        )
    )
    assert len(children) >= 2
    for left, right in zip(children, children[1:]):
        assert shared_boundary(left["text"], right["text"])


def test_b_pc_children_cover_parent_without_holes():
    """B-PC: union of children covers the parent; parent_index is traceable."""
    text = ("段落" * 180 + "。\n") * 4
    parents, children = split_parent_child(text, parent_size=300, child_size=80)
    assert parents
    for index, parent in enumerate(parents):
        kids = [child for child in children if child.parent_index == index]
        assert kids
        assert uncovered_span(parent, kids) == ""
        for child in kids:
            assert child.content in parent.content
            assert parent.start <= child.start <= child.end <= parent.end


def test_b_pc_default_parent_child_has_no_holes():
    """B-PC: default 512/80 parent-child still covers the parent."""
    text = ("这是一句用来切开的测试正文。" * 20 + "\n") * 8
    parents, children = split_parent_child(text)
    linked = [child for child in children if child.parent_index is not None]
    if not parents:
        assert children
        return
    for index, parent in enumerate(parents):
        kids = [child for child in linked if child.parent_index == index]
        assert kids
        assert uncovered_span(parent, kids) == ""


def test_b_pc_distill_reads_the_whole_file_and_keeps_search_chunks(tmp_path: Path, monkeypatch):
    """Distill reads the stored file once. Search chunks stay, and are not re-split."""
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    path = tmp_path / "scan" / "note.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("李明喜欢在夜里骑行。" + ("补充一句。" * 200), encoding="utf-8")
    opened = engine.ingest_file(path)
    stored = list(
        engine.db().execute(
            "SELECT seq, text FROM chunks WHERE source_id=? AND role='child' ORDER BY seq",
            (opened["id"],),
        )
    )
    assert len(stored) > 1
    seen: list[str] = []
    monkeypatch.setattr(
        engine,
        "_chunk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("rechunk bypass")),
    )
    monkeypatch.setattr(
        engine,
        "_llm_extract",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("extract")),
    )
    monkeypatch.setattr(engine, "_match_integrate_parts", lambda _text: {"library_candidates": [{"id": "c-li", "name": "李明"}]})
    monkeypatch.setattr(engine, "_llm_draft", lambda text, *_a, **_k: seen.append(text) or {"crystals": []})
    source = engine.db().execute("SELECT * FROM sources WHERE id=?", (opened["id"],)).fetchone()
    qid = engine.db().execute("SELECT id FROM queue WHERE source_id=?", (opened["id"],)).fetchone()["id"]
    with pytest.raises(RuntimeError):
        engine._distill(source, qid)
    assert len(seen) == 1
    assert "李明喜欢在夜里骑行" in seen[0]
    assert "补充一句。" in seen[0]
    assert "夜里骑行" in " ".join(row["text"] for row in stored)
    hits = engine.search("夜里骑行")
    raw = hits.get("raw") or []
    assert any("夜里骑行" in (item.get("text") or "") for item in raw) or any(
        "夜里骑行" in (item.get("text") or "") for item in hits.get("crystals") or []
    )
