"""The owner page starts blank. A hand edit of the article is what later reads use."""

from pathlib import Path

from rogmem.ui import crystal_page


def _open(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def test_person_detail_does_not_invent_empty_chapters(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    detail = engine.crystal_detail("owner")
    assert "教育背景" not in detail["chapters"]
    assert "事业经历" not in detail["chapters"]
    assert "## 教育背景" not in engine.format_crystal_article(detail)


def test_owner_appellation_stays_as_written_and_does_not_lock_the_page(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    detail = engine.crystal_detail("owner")
    assert detail["summary"]["真名"] == "尚无来源"
    assert detail["summary"]["别名"] == "尚无来源"
    assert "林知远" not in engine.format_crystal_article(detail)
    assert [row["note"] for row in engine.revisions("owner")].count("预写真名与别名") == 0
    saved = engine.save_basic(
        {"sketch_char_limit": "3500", "owner_appellation": "  云岚甲、甲哥   lin  "}
    )
    assert saved["ok"] is True
    assert engine.basic_config()["owner_appellation"] == "云岚甲、甲哥   lin"
    engine.connect(tmp_path, tmp_path / "scan")
    again = engine.crystal_detail("owner")
    assert again["summary"]["真名"] == "尚无来源"
    assert again["summary"]["别名"] == "尚无来源"
    assert engine.basic_config()["owner_appellation"] == "云岚甲、甲哥   lin"
    text = engine.format_crystal_article(again).replace("真名：尚无来源", "真名：手改姓名", 1)
    assert engine.save_crystal_article("owner", text)["ok"] is True
    engine.connect(tmp_path, tmp_path / "scan")
    assert engine.crystal_detail("owner")["summary"]["真名"] == "手改姓名"
    assert engine.basic_config()["owner_appellation"] == "云岚甲、甲哥   lin"
    assert engine.save_basic({"sketch_char_limit": "3500", "owner_appellation": "另一称呼"})["ok"] is True
    assert engine.crystal_detail("owner")["summary"]["真名"] == "手改姓名"
    assert engine.crystal_detail("owner")["summary"]["别名"] == "尚无来源"
    assert "人工修订正文" in [row["note"] for row in engine.revisions("owner")]


def test_article_edit_saves_a_chapter_and_one_revision(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    detail = engine.crystal_detail("owner")
    text = engine.format_crystal_article(detail)
    text = text.replace("所在地：尚无来源", "所在地：深圳", 1)
    text = text.rstrip() + "\n## 教育背景\n在做记忆库。\n"
    assert engine.save_crystal_article("owner", text)["ok"] is True
    again = engine.crystal_detail("owner")
    assert again["chapters"]["教育背景"] == "在做记忆库。"
    assert again["summary"]["所在地"] == "深圳"
    dropped = engine.format_crystal_article(again).replace("所在地：深圳\n", "", 1)
    assert engine.save_crystal_article("owner", dropped)["ok"] is True
    cleared = engine.crystal_detail("owner")
    assert cleared["summary"]["所在地"] == "尚无来源"
    assert cleared["chapters"]["教育背景"] == "在做记忆库。"
    assert engine.save_crystal_article("owner", engine.format_crystal_article(cleared))["unchanged"] is True
    assert [row["note"] for row in engine.revisions("owner")].count("人工修订正文") == 2
    page = crystal_page(again, engine.revisions("owner"), [{"id": "owner", "name": "记忆主人", "type": "人物", "status": "active"}], "", "active")
    assert ">编辑</a>" in page
    assert "data-field=" not in page
    editing = crystal_page(
        again,
        engine.revisions("owner"),
        [{"id": "owner", "name": "记忆主人", "type": "人物", "status": "active"}],
        "",
        "active",
        editing=True,
    )
    assert "data-markdown" in editing
    assert "data-field=" not in editing
    assert ">保存</button>" in editing
    assert "在做记忆库。" in editing
    saved = engine.save_crystal_fields("owner", {"别名": "手写别名"}, {"个人生活": "周末画画。"})
    assert saved["ok"] is True
    edited = engine.crystal_detail("owner")
    assert edited["summary"]["别名"] == "手写别名"
    assert edited["chapters"]["个人生活"] == "周末画画。"
    assert edited["chapters"]["教育背景"] == "在做记忆库。"
