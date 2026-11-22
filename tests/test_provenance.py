"""Each paragraph points back to the material it came from. The program writes that, not the model."""

from __future__ import annotations

import asyncio
import json

from rogmem.pages_md import carry_source_marks, render_markdown, source_marks, strip_source_marks

S1 = "a1b2c3d4e5f60718"
S2 = "b2c3d4e5f6071829"
S3 = "c3d4e5f60718293a"
S4 = "d4e5f60718293a4b"


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    monkeypatch.setattr(engine, "llm_target", lambda role: None)
    return engine


def _source(engine, sid: str, body: str, *, kind: str = "file"):
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,filename,event_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (sid, kind, "2026-11-06T12:00:00+00:00", "h-" + sid, body, "萃取中", "fast", "笔记.md", "2026-11-06T12:00:00+00:00"),
    )
    engine.db().commit()
    return engine.db().execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()


def _land(engine, source, body: str, name: str = "柏舟乙"):
    engine._land_markdown_pages(source, [{"name": name, "type": "人物", "body": body}])
    return engine.db().execute(
        "SELECT id, body_md FROM crystallizations WHERE name=? AND status='active'", (name,)
    ).fetchone()


def _paragraph(body: str, words: str) -> str:
    return next(part for part in body.split("\n\n") if words in part)


def test_a_new_paragraph_names_its_source_and_alias_lines_and_headings_do_not(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    source = _source(engine, S1, "柏舟乙在溪口镇开了一家修表铺。他每周三去码头收旧零件。")
    body = "别名：老柏\n\n## 生计\n\n柏舟乙在溪口镇开了一家修表铺。\n\n他每周三去码头收旧零件。"
    row = _land(engine, source, body)
    stored = row["body_md"]
    assert "别名：老柏\n" in stored and "别名：老柏 〔" not in stored
    assert "## 生计\n" in stored and "## 生计 〔" not in stored
    assert "柏舟乙在溪口镇开了一家修表铺。 〔源 a1b2c3d4〕" in stored
    assert "他每周三去码头收旧零件。 〔源 a1b2c3d4〕" in stored
    assert engine._alias_names(stored) == ["老柏"]


def test_unchanged_paragraph_keeps_its_mark_and_a_changed_one_adds_the_new_source(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    first = _source(engine, S1, "柏舟乙在溪口镇开了一家修表铺。他每周三去码头收旧零件。")
    _land(engine, first, "柏舟乙在溪口镇开了一家修表铺。\n\n他每周三去码头收旧零件。")
    second = _source(engine, S2, "他现在改成每周五去码头收旧零件。")
    # The writer returns the page without any marks; the program puts the old ones back.
    row = _land(engine, second, "柏舟乙在溪口镇开了一家修表铺。\n\n他现在改成每周五去码头收旧零件。")
    stored = row["body_md"]
    assert _paragraph(stored, "修表铺").endswith("〔源 a1b2c3d4〕")
    assert _paragraph(stored, "每周五").endswith("〔源 a1b2c3d4 b2c3d4e5〕")
    third = _source(engine, S3, "柏舟乙收养了一只叫铜铃的猫。")
    # This time the writer copied the marks itself. They are not doubled.
    row = _land(engine, third, stored + "\n\n柏舟乙收养了一只叫铜铃的猫。")
    again = row["body_md"]
    assert again.count("〔源") == 3
    assert _paragraph(again, "修表铺").endswith("〔源 a1b2c3d4〕")
    assert _paragraph(again, "每周五").endswith("〔源 a1b2c3d4 b2c3d4e5〕")
    assert _paragraph(again, "铜铃").endswith("〔源 c3d4e5f6〕")


def test_a_paragraph_keeps_only_the_three_most_recent_sources(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    wording = [
        "柏舟乙每周三去码头收旧零件。",
        "柏舟乙每周三去码头收旧零件，也收旧发条。",
        "柏舟乙每周三去码头收旧零件，也收旧发条和表壳。",
        "柏舟乙每周三去码头收旧零件，也收旧发条、表壳和齿轮。",
    ]
    row = None
    for sid, text in zip((S1, S2, S3, S4), wording):
        row = _land(engine, _source(engine, sid, text), text)
    assert source_marks(row["body_md"]) == ["b2c3d4e5", "c3d4e5f6", "d4e5f607"]


def test_a_dialogue_paragraph_names_the_rows_it_came_from(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    lead = _source(engine, S1, "我打算把修表铺搬到河西街。", kind="user")
    _source(engine, S2, "好的，记下了：铜铃这只猫怕打雷。", kind="agent")
    _source(engine, S3, "另外表壳都从北岸的老周那里进货。", kind="user")
    members = tuple({"qid": index + 1, "source_id": sid} for index, sid in enumerate((S1, S2, S3)))
    token = engine._EXCHANGE.set(members)
    try:
        row = _land(
            engine,
            lead,
            "柏舟乙打算把修表铺搬到河西街。\n\n柏舟乙的表壳从北岸的老周那里进货。\n\n柏舟乙喜欢下棋。",
        )
    finally:
        engine._EXCHANGE.reset(token)
    stored = row["body_md"]
    assert _paragraph(stored, "河西街").endswith("〔源 a1b2c3d4〕")
    assert _paragraph(stored, "老周").endswith("〔源 c3d4e5f6〕")
    # Words found in no row still point at the unit's lead row.
    assert _paragraph(stored, "下棋").endswith("〔源 a1b2c3d4〕")


def test_short_marks_resolve_for_read_source_and_read_section(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    source = _source(engine, S1, "柏舟乙每周三去码头收旧零件。")
    _land(engine, source, "## 生计\n\n柏舟乙每周三去码头收旧零件。")
    assert engine.resolve_source_mark("a1b2c3d4") == S1
    assert engine.resolve_source_mark("ffffffff") == ""
    read = engine.read_section("柏舟乙", "生计")
    assert "〔源 a1b2c3d4〕" in read["text"]
    assert read["source_marks"] == [{"marker": "a1b2c3d4", "source_id": S1}]
    from rogmem import app

    tool = app.read_source
    fn = getattr(tool, "fn", tool)
    payload = json.loads(asyncio.run(fn("a1b2c3d4")))
    assert payload["id"] == S1 and "码头" in payload["body"]
    assert engine.source_detail("a1b2c3d4")["source"]["id"] == S1


def test_a_short_mark_grows_when_another_source_shares_its_prefix(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _source(engine, "a1b2c3d4ffff0000", "别的材料。")
    source = _source(engine, S1, "柏舟乙每周三去码头收旧零件。")
    row = _land(engine, source, "柏舟乙每周三去码头收旧零件。")
    assert source_marks(row["body_md"]) == ["a1b2c3d4e"]
    assert engine.resolve_source_mark("a1b2c3d4e") == S1


def test_the_page_renders_the_mark_as_a_link_to_the_full_source(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    source = _source(engine, S1, "柏舟乙每周三去码头收旧零件。")
    row = _land(engine, source, "柏舟乙每周三去码头收旧零件。")
    detail = engine.crystal_detail(row["id"])
    assert detail["source_marks"] == {"a1b2c3d4": S1}
    html = render_markdown(detail["body_md"], {}, detail["source_marks"])
    assert f"<sup class='cite src'>源 <a href='/admin/sources/{S1}'" in html
    assert "〔源" not in html
    from rogmem.ui import crystal_page

    page = crystal_page(detail, [], [], "", "active")
    assert f"/admin/sources/{S1}" in page


def test_setting_off_adds_no_marks_but_keeps_the_old_ones(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _land(engine, _source(engine, S1, "柏舟乙每周三去码头收旧零件。"), "柏舟乙每周三去码头收旧零件。")
    engine.set_setting("provenance_marks", "0")
    engine.db().commit()
    row = _land(
        engine,
        _source(engine, S2, "柏舟乙收养了一只叫铜铃的猫。"),
        "柏舟乙每周三去码头收旧零件。\n\n柏舟乙收养了一只叫铜铃的猫。",
    )
    stored = row["body_md"]
    assert _paragraph(stored, "码头").endswith("〔源 a1b2c3d4〕")
    assert "〔" not in _paragraph(stored, "铜铃")


def test_marks_do_not_enter_footnotes_code_or_the_search_words():
    before = ""
    after = (
        "他在溪口镇修表。[^1]\n\n```\n配置 = 一\n\n配置 = 二\n```\n\n| 列 | 值 |\n\n[^1]: 实 「他在溪口镇修表。」"
    )
    marked = carry_source_marks(before, after, lambda _text: ["a1b2c3d4"])
    assert "他在溪口镇修表。[^1] 〔源 a1b2c3d4〕" in marked
    assert "配置 = 一\n\n配置 = 二" in marked
    assert marked.count("〔源") == 1
    assert strip_source_marks(marked) == after
    from rogmem.crystal_index import readable_text

    assert "a1b2c3d4" not in readable_text("柏舟乙", marked, "")
    # A second pass with nothing new changes nothing.
    assert carry_source_marks(marked, strip_source_marks(marked), lambda _text: ["ffffffff"]) == marked


def test_a_mark_the_old_page_never_carried_is_not_trusted():
    marked = carry_source_marks("", "柏舟乙喜欢下棋。 〔源 deadbeef〕", lambda _text: ["a1b2c3d4"])
    assert source_marks(marked) == ["a1b2c3d4"]


def test_a_write_from_several_units_marks_paragraphs_with_the_rows_they_came_from(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _source(engine, S1, "柏舟乙在溪口镇开了一家修表铺。")
    _source(engine, S2, "柏舟乙收养了一只叫铜铃的猫。")
    before = "柏舟乙在溪口镇开了一家修表铺。 〔源 a1b2c3d4〕"
    body = "柏舟乙在溪口镇开了一家修表铺。\n\n柏舟乙收养了一只叫铜铃的猫。\n\n他周末喜欢下棋。"
    marked = engine.carry_provenance_from(engine.db(), before, body, [S1, S2])
    assert _paragraph(marked, "修表铺").endswith("〔源 a1b2c3d4〕")
    assert _paragraph(marked, "铜铃").endswith("〔源 b2c3d4e5〕")
    # Words found in none of the rows point at the newest one.
    assert _paragraph(marked, "下棋").endswith("〔源 b2c3d4e5〕")
