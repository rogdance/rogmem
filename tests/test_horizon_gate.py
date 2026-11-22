"""A page body is mid/long-term memory. A run log section leaves it at delivery.

Every sentence here is synthetic.
"""

from __future__ import annotations

from rogmem.horizon_gate import LOG_HEADINGS, drop_log_sections, log_heading

PAGE = (
    "别名：雾灯\n\n"
    "## 偏好\n\n"
    "他喜欢先听结论，再看细节。[^1]\n\n"
    "## 修订记录\n\n"
    "- 第九夜 灯塔复核只读更新[^2]\n"
    "- 第十夜 又把铜铃挪了一格\n\n"
    "### 细项\n\n"
    "仍属于上面那份流水。\n\n"
    "## 约定\n\n"
    "动手前先问一句。[^3]\n\n"
    "[^1]: 实 「先听结论」\n"
    "[^2]: 实 「复核只读更新」\n"
    "[^3]: 实 「先问一句」"
)


def test_a_log_heading_is_recognised_by_its_words_not_its_dressing():
    for title in ("修订记录", "五、更新日志", "**Changelog**", "变更记录（第九夜起）", "修订记录：本轮", "Revision History"):
        assert log_heading(title), title
    for title in ("偏好", "记录", "沿革", "当前关注", "版本", "雾港的修订记录员"):
        assert not log_heading(title), title
    assert "修订记录" in LOG_HEADINGS and "changelog" in LOG_HEADINGS


def test_the_log_section_and_its_subsections_leave_the_rest_stays_verbatim():
    out, dropped = drop_log_sections(PAGE)
    assert "修订记录" not in out
    assert "铜铃挪了一格" not in out and "仍属于上面那份流水" not in out
    assert "## 偏好\n\n他喜欢先听结论，再看细节。[^1]\n\n## 约定\n\n动手前先问一句。[^3]" in out
    assert "[^1]: 实 「先听结论」" in out and "[^3]: 实 「先问一句」" in out
    # 只在流水里用到的注一起走。
    assert "[^2]" not in out
    assert dropped == len(PAGE) - len(out)


def test_a_page_without_a_log_comes_back_unchanged():
    body = "## 偏好\n\n他喜欢短句。\n\n\n## 约定\n\n先问。"
    assert drop_log_sections(body) == (body, 0)
    assert drop_log_sections("") == ("", 0)


def test_a_log_at_the_end_stops_at_the_notes():
    body = "## 偏好\n\n他喜欢短句。[^1]\n\n## 更新记录\n\n- 第三夜 改了一次\n\n[^1]: 实 「喜欢短句」"
    out, _ = drop_log_sections(body)
    assert out == "## 偏好\n\n他喜欢短句。[^1]\n\n[^1]: 实 「喜欢短句」"


def test_a_note_still_used_outside_the_log_stays():
    body = "## 偏好\n\n他喜欢短句。[^1]\n\n## 变更日志\n\n- 又确认了一次[^1]\n\n[^1]: 实 「喜欢短句」"
    out, _ = drop_log_sections(body)
    assert "[^1]: 实 「喜欢短句」" in out


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _source(engine):
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,filename,event_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            "src-fog",
            "file",
            "2026-11-06T12:00:00+00:00",
            "h-fog",
            "雾港守灯人喜欢先听结论。动手前先问一句。",
            "萃取中",
            "fast",
            "雾港.md",
            "2026-11-06T12:00:00+00:00",
        ),
    )
    engine.db().commit()
    return engine.db().execute("SELECT * FROM sources WHERE id='src-fog'").fetchone()


def _events(engine, cid):
    from rogmem.crystal_store import CrystalStore

    return [e for e in CrystalStore(engine.db()).events(cid) if e["kind"] == "page_log_dropped"]


def test_a_merged_page_is_stored_without_its_run_log(tmp_path, monkeypatch):
    import json

    engine = _open(tmp_path, monkeypatch)
    engine._insert_crystal_page(engine.db(), "keeper", "人物", "雾港守灯人", holder="test")
    engine.db().execute("UPDATE crystallizations SET body_md=? WHERE id='keeper'", ("他喜欢先听结论。",))
    engine.db().commit()
    source = _source(engine)
    merged = "他喜欢先听结论。\n\n## 约定\n\n动手前先问一句。\n\n## 修订记录\n\n- 第九夜 只读复核更新\n- 第十夜 又复核一次"

    def fake_complete(_base, _key, payload, **_kwargs):
        if "并页写作者" in payload["messages"][0]["content"]:
            return json.dumps({"same": "是", "page": merged, "conflicts": [], "gist": "补了约定。"}, ensure_ascii=False), "stop"
        return payload["messages"][1]["content"], "stop"

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m"))
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    engine._land_markdown_pages(source, [{"name": "雾港守灯人", "type": "人物", "body": "动手前先问一句。"}])
    body = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='keeper'").fetchone()["body_md"]
    assert "动手前先问一句。" in body
    assert "修订记录" not in body and "只读复核更新" not in body
    events = _events(engine, "keeper")
    assert len(events) == 1 and events[0]["payload"]["dropped_chars"] > 0


def test_the_gate_can_be_switched_off(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.set_setting("horizon_gate", "0")
    engine.db().commit()
    monkeypatch.setattr(engine, "llm_target", lambda role: None)
    source = _source(engine)
    engine._land_markdown_pages(
        source,
        [{"name": "雾港守灯人", "type": "人物", "body": "他喜欢先听结论。\n\n## 修订记录\n\n- 第九夜 只读复核更新"}],
    )
    row = engine.db().execute("SELECT id, body_md FROM crystallizations WHERE name='雾港守灯人'").fetchone()
    assert "只读复核更新" in row["body_md"]
    assert _events(engine, row["id"]) == []


def test_a_new_page_is_stored_without_its_run_log(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "llm_target", lambda role: None)
    source = _source(engine)
    engine._land_markdown_pages(
        source,
        [{"name": "雾港守灯人", "type": "人物", "body": "他喜欢先听结论。\n\n## 更新日志\n\n- 第九夜 只读复核更新"}],
    )
    row = engine.db().execute("SELECT id, body_md FROM crystallizations WHERE name='雾港守灯人'").fetchone()
    from rogmem.pages_md import strip_source_marks

    # 程序在交付时给新段落挂来源标记；日志节被删掉，只留正文那一段。
    assert strip_source_marks(row["body_md"]).strip() == "他喜欢先听结论。"
    assert "更新日志" not in row["body_md"]
    assert len(_events(engine, row["id"])) == 1
