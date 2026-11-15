"""Wiki links and markdown pages are what the reader sees."""

from __future__ import annotations


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    return engine


def test_saving_brackets_links_both_sides_and_drops_them_when_the_text_changes(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem.crystal_store import CrystalStore, ThinCrystal
    from rogmem.ui import crystal_page

    store = CrystalStore(engine.db())
    store.put(
        ThinCrystal(id="org", type="组织", name="南港书院", status="active", revision=0, aliases=["南港"])
    )
    store.put(
        ThinCrystal(
            id="person",
            type="人物",
            name="云岚甲",
            status="active",
            revision=0,
            body_md="## 事业经历\n\n后来到 [[南港]] 任教。[[没有这篇]] 不算。[[云岚甲]] 不连自己。",
        )
    )
    live = engine.db().execute(
        "SELECT dst FROM edges WHERE src='person' AND kind='提及' AND ended=0"
    ).fetchall()
    assert [row["dst"] for row in live] == ["org"]
    assert any(item["src"] == "person" and item["dst"] == "org" and item["kind"] == "提及" for item in engine.graph()["edges"])

    person = engine.crystal_detail("person")
    org = engine.crystal_detail("org")
    assert person["out_links"] == [{"id": "org", "name": "南港书院"}]
    assert org["in_links"] == [{"id": "person", "name": "云岚甲"}]
    page = crystal_page(person, [], [], "", "active")
    assert "href='/admin/crystals/org'" in page
    assert "南港" in page
    assert "反向链接" in crystal_page(org, [], [], "", "active")
    assert "云岚甲" in crystal_page(org, [], [], "", "active")

    edited = store.get("person")
    edited.body_md = "## 事业经历\n\n不再提到书院。"
    store.put(edited)
    assert engine.db().execute(
        "SELECT 1 FROM edges WHERE src='person' AND kind='提及' AND ended=0"
    ).fetchone() is None


def test_a_title_shared_by_two_pages_is_not_guessed(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem.crystal_store import CrystalStore, ThinCrystal

    store = CrystalStore(engine.db())
    store.put(ThinCrystal(id="a", type="产品", name="同名", status="active", revision=0))
    store.put(ThinCrystal(id="b", type="产品", name="同名", status="active", revision=0))
    store.put(
        ThinCrystal(id="c", type="人物", name="丙", status="active", revision=0, body_md="见 [[同名]]。")
    )
    assert engine.db().execute(
        "SELECT 1 FROM edges WHERE src='c' AND kind='提及' AND ended=0"
    ).fetchone() is None


def test_markdown_body_is_shown_whole_and_sketch_is_the_summary(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem.crystal_store import CrystalStore, ThinCrystal
    from rogmem.pages_md import parse_pages
    from rogmem.ui import crystal_page

    pages = parse_pages(
        "# 云岚甲\n类型：人物\n\n## 身份速写\n\n他在 [[南港书院]] 做事。\n\n## 当前关注与计划\n\n接着写编辑器。\n"
    )
    typed = parse_pages("# 星河工坊\n# 类型：项目\n\n## 项目定位\n\n从零做。\n")
    assert typed[0]["name"] == "星河工坊"
    assert typed[0]["type"] == "工程"
    assert "项目定位" in typed[0]["body"]
    assert pages == [
        {
            "name": "云岚甲",
            "type": "人物",
            "body": "## 身份速写\n\n他在 [[南港书院]] 做事。\n\n## 当前关注与计划\n\n接着写编辑器。",
        }
    ]
    store = CrystalStore(engine.db())
    store.put(ThinCrystal(id="org", type="组织", name="南港书院", status="active", revision=0))
    store.put(
        ThinCrystal(
            id="person",
            type="人物",
            name="云岚甲",
            status="active",
            revision=0,
            body_md=pages[0]["body"],
            sketch_md="云岚甲在南港书院做事。",
        )
    )
    detail = engine.crystal_detail("person")
    html = crystal_page(detail, [], [], "", "active")
    assert "身份速写" in html
    assert "当前关注与计划" in html
    assert "结晶速写" in html
    assert "云岚甲在南港书院做事。" in html
    assert "href='/admin/crystals/org'" in html
    section = engine.read_section("云岚甲", "当前关注与计划")
    assert section["missing"] is False
    assert "编辑器" in section["text"]
    saved = engine.save_crystal_markdown("person", detail["body_md"] + "\n\n## 另起一节\n\n留下。", detail["sketch_md"])
    assert saved["ok"] is True
    assert "另起一节" in (engine.crystal_detail("person")["body_md"] or "")
    engine.db().execute("UPDATE crystallizations SET sketch_md=? WHERE id='owner'", ("主人这一页的速写。",))
    engine.db().commit()
    assert engine.core_memory()["loaded"] == [{"name": "记忆主人", "text": "主人这一页的速写。"}]


def test_a_page_write_counts_as_crystallized_even_without_a_statement(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane)
           VALUES('src-page','file','2026-11-05T00:00:00+00:00','h','正文','蒸馏无结晶','fast')"""
    )
    engine.db().execute(
        """INSERT INTO revisions(crystal_id,created_at,note,source_id)
           VALUES('owner','2026-11-05T00:00:00+00:00','正文','src-page')"""
    )
    engine.db().commit()
    engine.settle_file_statuses()
    row = engine.db().execute("SELECT status, coverage FROM sources WHERE id='src-page'").fetchone()
    assert row["status"] == "蒸馏已结晶"
    assert row["coverage"] == "pages"


def test_a_citation_mark_hides_the_quote_until_the_mark_is_opened():
    from rogmem.pages_md import render_markdown

    html = render_markdown("他在腾讯任职。[^1]\n\n[^1]: 实 /admin/sources/abc 「2019年加入腾讯」\n")
    assert "他在腾讯任职" in html
    assert "<sup class='cite'" in html
    assert "打开出处" in html
    assert html.count("2019年加入腾讯") == 1
    assert "<q>" in html


def test_a_mark_without_a_passage_does_not_open_an_empty_card():
    from rogmem.pages_md import render_markdown

    html = render_markdown("他在南港写编辑器。[^1]\n")
    assert "他在南港写编辑器" in html
    assert "cite-card" not in html
    assert "<sup" not in html


def _product(engine, cid: str, name: str, body: str):
    engine._insert_crystal_page(engine.db(), cid, "产品", name, holder="test")
    engine.db().execute("UPDATE crystallizations SET body_md=? WHERE id=?", (body, cid))
    engine.db().commit()


def _file_source(engine, sid: str, body: str):
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,filename)
           VALUES(?, 'file', '2026-11-06T00:00:00+00:00', ?, ?, '蒸馏中', 'fast', 'note.md')""",
        (sid, sid, body),
    )
    engine.db().commit()
    return engine.db().execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()


def test_a_draft_title_does_not_rename_a_page_the_merge_did_not_propose(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "llm_target", lambda role: None)
    _product(engine, "play", "yunlan Play", "A small game.\n")
    source = _file_source(engine, "src-zh", "云岚游。")
    engine._land_markdown_pages(
        source,
        [{"name": "云岚游", "type": "产品", "body": "云岚游是一款小游戏。\n别名：yunlan Play\n"}],
    )
    rows = engine.db().execute(
        "SELECT id, name FROM crystallizations WHERE type='产品' AND status='active'"
    ).fetchall()
    assert [(row["id"], row["name"]) for row in rows] == [("play", "yunlan Play")]
    notes = [item["note"] for item in engine.revisions("play")]
    assert "篇名" not in notes


def test_a_title_proposal_renames_the_bound_page_and_keeps_the_old_name(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "llm_target", lambda role: None)
    _product(engine, "play", "yunlan Play", "A small game.\n")
    source = _file_source(engine, "src-rename", "云岚游是现在的名字。")
    engine._land_markdown_pages(
        source,
        [
            {
                "id": "play",
                "name": "yunlan Play",
                "type": "产品",
                "title": "云岚游",
                "body": "云岚游是一款小游戏。\n",
            }
        ],
        start="落地",
    )
    from rogmem.crystal_store import hydrate

    rows = engine.db().execute(
        "SELECT id, name FROM crystallizations WHERE type='产品' AND status='active'"
    ).fetchall()
    assert [(row["id"], row["name"]) for row in rows] == [("play", "云岚游")]
    crystal = hydrate(engine.db().execute("SELECT * FROM crystallizations WHERE id='play'").fetchone())
    assert "yunlan Play" in crystal.aliases
    assert "别名：yunlan Play" in crystal.body_md
    from rogmem.wikilink import title_index

    index = title_index(engine.db())
    assert index.get("云岚游") == "play"
    assert index.get("yunlan Play") == "play"
    revision = next(item for item in engine.revisions("play") if item["note"] == "篇名")
    assert "篇名改为云岚游" in revision["change"]
    assert "yunlan Play" in revision["change"]


def test_a_title_proposal_does_not_take_a_name_another_page_owns(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "llm_target", lambda role: None)
    _product(engine, "old", "旧灯塔", "旧灯塔还在。\n")
    _product(engine, "other", "北窗灯塔", "北窗灯塔是另一件。\n")
    source = _file_source(engine, "src-clash", "名字撞上了。")
    engine._land_markdown_pages(
        source,
        [
            {
                "id": "old",
                "name": "旧灯塔",
                "type": "产品",
                "title": "北窗灯塔",
                "body": "旧灯塔还在，不能占用别人的名字。\n别名：北窗灯塔\n",
            }
        ],
        start="落地",
    )
    names = {
        row["id"]: row["name"]
        for row in engine.db().execute(
            "SELECT id, name FROM crystallizations WHERE type='产品' AND status='active'"
        )
    }
    assert names == {"old": "旧灯塔", "other": "北窗灯塔"}
    from rogmem.crystal_store import hydrate

    old = hydrate(engine.db().execute("SELECT * FROM crystallizations WHERE id='old'").fetchone())
    assert "北窗灯塔" not in old.aliases
    assert "别名：北窗灯塔" not in (old.body_md or "")
    revision = next(item for item in engine.revisions("old") if "篇名仍是旧灯塔" in (item.get("change") or ""))
    assert "篇名改为" not in revision["change"]
    assert "北窗灯塔" in (revision.get("gist") or "")


def test_a_near_spelling_can_still_become_the_title(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "llm_target", lambda role: None)
    _product(engine, "old", "旧灯塔", "旧灯塔还在。\n")
    _product(engine, "neighbor", "云岚游", "云岚游是另一件。\n")
    source = _file_source(engine, "src-near", "现在叫云岚油。")
    engine._land_markdown_pages(
        source,
        [
            {
                "id": "old",
                "name": "旧灯塔",
                "type": "产品",
                "title": "云岚油",
                "body": "云岚油是现在的名字。\n",
            }
        ],
        start="落地",
    )
    names = {
        row["id"]: row["name"]
        for row in engine.db().execute(
            "SELECT id, name FROM crystallizations WHERE type='产品' AND status='active'"
        )
    }
    assert names == {"old": "云岚油", "neighbor": "云岚游"}


def test_the_owner_page_does_not_accept_a_title_proposal(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "llm_target", lambda role: None)
    source = _file_source(engine, "src-owner", "他有一个真名。")
    engine._land_markdown_pages(
        source,
        [
            {
                "id": "owner",
                "name": "记忆主人",
                "type": "人物",
                "title": "云岚甲",
                "body": "记忆主人仍用这个篇名。\n",
            }
        ],
        start="落地",
    )
    name = engine.db().execute("SELECT name FROM crystallizations WHERE id='owner'").fetchone()["name"]
    assert name == "记忆主人"
    assert all(item["note"] != "篇名" for item in engine.revisions("owner"))


def test_merge_applies_a_new_title_without_opening_a_second_page(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _product(engine, "play", "旧灯塔", "旧灯塔是现在的名字。\n")
    source = _file_source(engine, "src-merge", "现在叫北窗灯塔。")

    def fake_complete(_base, _key, payload, **_kwargs):
        system = payload["messages"][0]["content"]
        if "并页写作者" in system:
            return (
                '{"same":"是","name":"北窗灯塔","page":"北窗灯塔是现在的名字。\\n别名：旧灯塔\\n","conflicts":[],"gist":"篇名由旧灯塔改为北窗灯塔。"}',
                "stop",
            )
        return "一段速写。", "stop"

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m") if role == "distill" else None)
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    engine._land_markdown_pages(
        source,
        [{"name": "旧灯塔", "type": "产品", "body": "北窗灯塔是现在的名字。\n"}],
    )
    from rogmem.crystal_store import hydrate

    rows = engine.db().execute(
        "SELECT id, name FROM crystallizations WHERE type='产品' AND status='active'"
    ).fetchall()
    assert [(row["id"], row["name"]) for row in rows] == [("play", "北窗灯塔")]
    crystal = hydrate(engine.db().execute("SELECT * FROM crystallizations WHERE id='play'").fetchone())
    assert "旧灯塔" in crystal.aliases
    assert any("篇名改为北窗灯塔" in item["change"] for item in engine.revisions("play"))


def test_one_git_address_stays_one_engineering_page(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    repo = "https://github.com/yunlan/rogmem"
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,project_ref,event_at)
           VALUES('src-eng','user','2026-11-06T00:00:00+00:00','h','今天修检索。','蒸馏中','fast',?,
                  '2026-11-06T00:00:00+00:00')""",
        (repo,),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-eng'").fetchone()
    engine._land_markdown_pages(
        source,
        [
            {"name": "云岚核忆", "type": "工程", "body": f"仓库 {repo}\n\n第一次定了超时。"},
            {"name": "云岚甲", "type": "人物", "body": "他在写编辑器。"},
            {"name": "另一篇工程", "type": "工程", "body": f"仓库 {repo}\n\n第二次把超时改成 30 秒。"},
        ],
    )
    rows = engine.db().execute(
        "SELECT id, name, body_md FROM crystallizations WHERE type='工程' AND status='active'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "云岚核忆"
    assert "30 秒" in rows[0]["body_md"]
    person = engine.db().execute(
        "SELECT type FROM crystallizations WHERE name='云岚甲' AND status='active'"
    ).fetchone()
    assert person["type"] == "人物"


def test_engineering_session_sends_the_page_and_owner_sketch_file_does_not(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    repo = "https://github.com/yunlan/rogmem"
    engine.db().execute(
        "UPDATE crystallizations SET sketch_md=? WHERE id='owner'",
        ("主人喜欢先看结论。",),
    )
    engine._insert_crystal_page(engine.db(), "eng-1", "工程", "云岚核忆", holder="distill:seed")
    row = engine.db().execute("SELECT * FROM crystallizations WHERE id='eng-1'").fetchone()
    from rogmem.crystal_store import CrystalStore, hydrate

    crystal = hydrate(row)
    crystal.body_md = f"仓库 {repo}\n\n超时曾经定成 15 秒。"
    crystal.keys["仓库地址"] = repo
    CrystalStore(engine.db()).put(crystal, holder="distill:seed")
    seen = {}

    def fake_complete(_base, _key, payload, **_kwargs):
        seen["user"] = payload["messages"][1]["content"]
        return "# 云岚甲\n类型：人物\n\n他在。\n", "stop"

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m") if role == "distill" else None)
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    engine._llm_draft("今天继续改超时。", "user", project_ref=repo, source_id="src-user")
    assert "已有工程结晶：" in seen["user"]
    assert "超时曾经定成 15 秒" in seen["user"]
    assert "记忆主人速写：" in seen["user"]
    assert "主人喜欢先看结论" in seen["user"]
    engine._llm_draft("简历里写了这个仓库。", "file", project_ref=repo, source_id="src-file")
    assert "已有工程结晶：" not in seen["user"]
    assert "记忆主人速写：" not in seen["user"]
    engine.db().execute("UPDATE crystallizations SET body_md=? WHERE id='owner'", ("他现在住在南港。",))
    engine.db().commit()
    engine._llm_draft("简历里写了住址。", "file", source_id="src-file")
    assert "已有记忆主人结晶：" in seen["user"]
    assert "他现在住在南港。" in seen["user"]


def test_core_memory_is_the_owner_sketch_and_the_current_project_sketch(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    assert engine.sketch_char_limit() == 3500
    assert engine.save_basic({"sketch_char_limit": "800"})["ok"] is True
    assert engine.sketch_char_limit() == 800
    from rogmem.prompts import sketch_system

    told = sketch_system("人物", owner=True, limit=engine.sketch_char_limit())
    assert "800字以内" in told
    assert "3500字以内" not in told
    fitted = engine._fit_sketch("甲。\n\n" + ("乙" * 900), 800)
    assert len(fitted) <= 800
    assert fitted.startswith("甲。")
    engine.db().execute("UPDATE crystallizations SET sketch_md=? WHERE id='owner'", ("甲" * 900,))
    engine.db().commit()
    kept = engine.save_crystal_markdown("owner", "正文。", "甲" * 900)
    assert kept["ok"] is True
    assert len(engine.crystal_detail("owner")["sketch_md"]) == 900
    engine.save_crystal_markdown("owner", "正文。", "乙" * 900)
    assert len(engine.crystal_detail("owner")["sketch_md"]) <= 800
    engine.db().execute("UPDATE crystallizations SET sketch_md=? WHERE id='owner'", ("主人速写。",))
    engine._insert_crystal_page(engine.db(), "eng", "工程", "云岚核忆", holder="test")
    row = engine.db().execute("SELECT * FROM crystallizations WHERE id='eng'").fetchone()
    from rogmem.crystal_store import CrystalStore, hydrate

    crystal = hydrate(row)
    crystal.body_md = "仓库 https://github.com/yunlan/rogmem\n"
    crystal.sketch_md = "工程速写。"
    crystal.keys["仓库地址"] = "https://github.com/yunlan/rogmem"
    CrystalStore(engine.db()).put(crystal, holder="test")
    engine.db().commit()
    loaded = engine.core_memory(project="https://github.com/yunlan/rogmem")["loaded"]
    assert [item["name"] for item in loaded] == ["记忆主人", "云岚核忆"]
    assert loaded[1]["text"] == "工程速写。"
