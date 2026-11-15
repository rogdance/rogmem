"""A revision says who changed which words, and which material they used."""

from rogmem.ui import crystal_page


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _source(engine, filename: str):
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,filename,event_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            "src-resume",
            "file",
            "2026-11-06T12:00:00+00:00",
            "h",
            "他在南港写编辑器。后来去了北城。",
            "蒸馏中",
            "fast",
            filename,
            "2026-11-06T12:00:00+00:00",
        ),
    )
    engine.db().commit()
    return engine.db().execute("SELECT * FROM sources WHERE id='src-resume'").fetchone()


def test_a_page_revision_records_the_words_the_writer_and_the_material(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "llm_target", lambda role: None)
    source = _source(engine, "南港简历.md")
    engine._land_markdown_pages(
        source,
        [
            {
                "name": "云岚甲",
                "type": "人物",
                "body": "他在南港写编辑器。[^1]\n\n[^1]: 实 「他在南港写编辑器。」\n",
            }
        ],
    )
    row = engine.db().execute(
        "SELECT id, body_md FROM crystallizations WHERE name='云岚甲' AND status='active'"
    ).fetchone()
    first = engine.revisions(row["id"])
    assert len(first) == 1
    assert first[0]["actor"] == "蒸馏"
    assert "他在南港写编辑器" in first[0]["change"]
    assert "南港简历.md" in first[0]["basis"]
    assert "他在南港写编辑器。" in first[0]["basis"]
    page = crystal_page(
        {"id": row["id"], "name": "云岚甲", "type": "人物", "status": "active", "status_label": "活跃", "summary": {}, "chapters": {}},
        first,
        [],
        "",
        "active",
        "log",
    )
    assert "修订人" in page and "rev-who'>蒸馏" in page
    assert "修订范围" in page and "正文" in page
    assert "撤销修订" in page
    assert "确定撤销「云岚甲」" in page
    assert "确认撤销" in page
    assert 'onclick=\'post("/admin/api/crystals/' not in page
    assert "class='rev'" in page
    assert "南港简历.md" in page
    assert "打开出处" in page
    assert "这一条没有记下依据" not in page

    engine._land_markdown_pages(
        source,
        [{"name": "云岚甲", "type": "人物", "body": "他在南港写编辑器。[^1]\n\n[^1]: 实 「他在南港写编辑器。」\n"}],
    )
    assert len(engine.revisions(row["id"])) == 1

    engine._land_markdown_pages(
        source,
        [{"name": "云岚甲", "type": "人物", "body": "他后来去了北城。"}],
    )
    second = engine.revisions(row["id"])
    assert len(second) == 2
    assert "正文" in second[-1]["scope"]
    assert "速写" not in second[-1]["scope"].split("、")
    assert "删去" in second[-1]["change"]
    assert "北城" in second[-1]["change"]
    engine.rollback(row["id"], first[0]["id"])
    restored = engine.db().execute("SELECT body_md FROM crystallizations WHERE id=?", (row["id"],)).fetchone()
    assert "南港写编辑器" in restored["body_md"]
    assert "北城" not in restored["body_md"]


def test_merging_an_existing_page_records_the_conflict(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine._insert_crystal_page(engine.db(), "org", "组织", "南港书院", holder="test")
    engine.db().execute(
        "UPDATE crystallizations SET body_md=? WHERE id='org'",
        ("他在南港。[^1]\n\n[^1]: 实 「他在南港。」\n",),
    )
    engine.db().commit()
    source = _source(engine, "南港简历.md")

    def fake_complete(_base, _key, payload, **_kwargs):
        system = payload["messages"][0]["content"]
        if "并页写作者" in system:
            return (
                '{"page":"他在南港。[^1]\\n\\n他后来去了北城。\\n\\n[^1]: 实 「他在南港。」\\n",'
                '"conflicts":[{"matter":"人在哪里","verdict":"更新","basis":"新材料写的是北城。"}],'
                '"gist":"因更新，所在地改成了北城。"}',
                "stop",
            )
        return payload["messages"][1]["content"], "stop"

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m"))
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    engine._land_markdown_pages(
        source,
        [{"name": "南港书院", "type": "组织", "body": "他后来去了北城。[^1]\n\n[^1]: 实 「他在南港。」\n"}],
    )
    body = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='org'").fetchone()["body_md"]
    assert "北城" in body
    assert "[^1]" in body
    changes = [item["change"] for item in engine.revisions("org")]
    conflict = next(item for item in changes if "冲突裁决" in item)
    assert "更新：人在哪里" in conflict
    assert "新材料写的是北城" in conflict
    logged = next(item for item in engine.revisions("org") if "冲突裁决" in (item.get("change") or ""))
    assert logged["gist"] == "因更新，所在地改成了北城。"


def test_a_merge_without_a_model_sentence_keeps_the_judgment(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine._insert_crystal_page(engine.db(), "org", "组织", "南港书院", holder="test")
    engine.db().execute(
        "UPDATE crystallizations SET body_md=? WHERE id='org'",
        ("他在南港。[^1]\n\n[^1]: 实 「他在南港。」\n",),
    )
    engine.db().commit()
    source = _source(engine, "南港简历.md")

    def fake_complete(_base, _key, payload, **_kwargs):
        system = payload["messages"][0]["content"]
        if "并页写作者" in system:
            return (
                '{"page":"他在南港。[^1]\\n\\n他后来去了北城。\\n\\n[^1]: 实 「他在南港。」\\n",'
                '"conflicts":[{"matter":"人在哪里","verdict":"更新","basis":"新材料写的是北城。"}]}',
                "stop",
            )
        return payload["messages"][1]["content"], "stop"

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m"))
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    engine._land_markdown_pages(
        source,
        [{"name": "南港书院", "type": "组织", "body": "他后来去了北城。[^1]\n\n[^1]: 实 「他在南港。」\n"}],
    )
    logged = next(item for item in engine.revisions("org") if "冲突裁决" in (item.get("change") or ""))
    assert logged["gist"].startswith("更新：人在哪里")
    assert "新材料写的是北城" in logged["gist"]
    assert not logged["gist"].startswith("删去")


def test_an_agent_draft_queues_merge_and_leaves_the_live_page(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    before = engine.db().execute("SELECT body_md, sketch_md FROM crystallizations WHERE id='owner'").fetchone()
    refused = engine.submit_draft("他现在住在南港。", "智能体")
    assert refused["ok"] is False
    page = (
        "# 记忆主人\n类型：人物\n\n"
        "他现在住在南港。不是同一人这件事不要再记住，忘掉旧地址。[^1]\n\n"
        "[^1]: 实 「我现在住在南港。」\n"
    )
    saved = engine.submit_draft(page, "智能体「云岚」")
    assert saved["ok"] is True and saved["queued"] == "并页" and saved["duplicate"] is False
    after = engine.db().execute("SELECT body_md, sketch_md FROM crystallizations WHERE id='owner'").fetchone()
    assert after["body_md"] == before["body_md"]
    assert after["sketch_md"] == before["sketch_md"]
    row = engine.db().execute(
        "SELECT kind, lane, stage_json, agent_name FROM sources WHERE id=?",
        (saved["id"],),
    ).fetchone()
    assert row["kind"] == "draft" and row["lane"] == "jump" and row["agent_name"] == "智能体「云岚」"
    assert "记忆主人" in row["stage_json"]
    queued = engine.db().execute("SELECT lane, state FROM queue WHERE source_id=?", (saved["id"],)).fetchone()
    assert queued["lane"] == "jump" and queued["state"] == "queued"
    seen = []

    def land(source, pages, start="并页"):
        del source
        seen.append((start, [item["name"] for item in pages]))
        return None

    monkeypatch.setattr(engine, "_land_markdown_pages", land)
    monkeypatch.setattr(engine, "_produce_distill_payload", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("草稿不重跑首蒸")))
    engine.db().execute(
        "UPDATE sources SET pipeline=? WHERE id=?",
        (engine._begin_attempt(""), saved["id"]),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id=?", (saved["id"],)).fetchone()
    assert engine._distill(source, 0) is True
    assert seen == [("并页", ["记忆主人"])]
    failed = engine.db().execute("UPDATE queue SET state='failed' WHERE source_id=?", (saved["id"],))
    del failed
    engine.db().commit()
    again = engine.submit_draft(page, "智能体「云岚」")
    assert again["duplicate"] is True and again["id"] == saved["id"] and again["queued"] == "并页"
    assert engine.db().execute("SELECT state, lane FROM queue WHERE source_id=?", (saved["id"],)).fetchone()["state"] == "queued"
    still = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='owner'").fetchone()
    assert still["body_md"] == before["body_md"]


def test_a_bound_engineering_page_does_not_need_the_address_repeated(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    repo = "https://github.com/example/memory"
    engine._insert_crystal_page(engine.db(), "eng", "工程", "云岚核忆", holder="test")
    engine.db().execute("UPDATE crystallizations SET body_md=? WHERE id='eng'", (f"仓库 {repo}\n",))
    engine.db().commit()
    row, found = engine._row_for_named_page(
        engine.db(),
        None,
        {"id": "eng", "name": "云岚核忆", "type": "工程", "body": "下次不要改这个端口。"},
        None,
        [],
    )
    assert row["id"] == "eng"
    assert repo in found


def test_the_log_puts_the_newest_revision_first():
    from rogmem.ui import crystal_page

    page = crystal_page(
        {
            "id": "c-order",
            "name": "旧页",
            "type": "人物",
            "status": "active",
            "status_label": "活跃",
            "summary": {},
            "chapters": {},
        },
        [
            {"id": 1, "note": "开篇", "change": "最早的一笔", "actor": "蒸馏", "basis": "第一份"},
            {"id": 2, "note": "正文", "change": "最新的一笔", "actor": "蒸馏", "basis": "第二份"},
        ],
        [],
        "",
        "active",
        "log",
    )
    assert page.index("最新的一笔") < page.index("最早的一笔")


def test_graph_spin_seconds_can_be_saved(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    assert engine.graph_spin_seconds() == 5
    assert engine.save_basic({"sketch_char_limit": "3500", "graph_spin_seconds": "5"})["ok"] is True
    assert engine.graph_spin_seconds() == 5
    assert engine.save_basic({"sketch_char_limit": "3500", "graph_spin_seconds": "0"})["ok"] is True
    assert engine.graph_spin_seconds() == 0
    refused = engine.save_basic({"sketch_char_limit": "3500", "graph_spin_seconds": "3601"})
    assert refused["ok"] is False
    assert engine.graph_spin_seconds() == 0


def test_an_old_body_line_still_names_the_file(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _source(engine, "旧简历.md")
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json)
           VALUES('c-old','人物','旧页','active','{}','{}')"""
    )
    engine.db().execute(
        """INSERT INTO revisions(crystal_id,created_at,note,source_id)
           VALUES('c-old','2026-11-06T12:00:00+00:00','正文','src-resume')"""
    )
    engine.db().commit()
    item = engine.revisions("c-old")[-1]
    assert item["actor"] == "蒸馏"
    assert "旧简历.md" in item["basis"]
    assert "没有留下改了哪些字" in item["change"]
    assert item["scope"] == ""
    page = crystal_page(
        {"id": "c-old", "name": "旧页", "type": "人物", "status": "active", "status_label": "活跃", "summary": {}, "chapters": {}},
        [item],
        [],
        "",
        "active",
        "log",
    )
    assert "未记下范围" in page


def test_a_sketch_change_is_only_a_sketch_and_rollback_restores_it(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    source = _source(engine, "南港简历.md")
    engine._land_markdown_pages(source, [{"name": "云岚甲", "type": "人物", "body": "他在南港。"}])
    row = engine.db().execute(
        "SELECT id, body_md FROM crystallizations WHERE name='云岚甲' AND status='active'"
    ).fetchone()
    assert engine.save_crystal_markdown(row["id"], row["body_md"], "速写甲。")["ok"] is True
    sketched = engine.revisions(row["id"])[-1]
    assert sketched["scope"] == "速写"
    assert engine.save_crystal_markdown(row["id"], row["body_md"], "速写乙。")["ok"] is True
    engine.rollback(row["id"], sketched["id"])
    restored = engine.crystal_detail(row["id"])
    assert restored["sketch_md"] == "速写甲。"
    assert "他在南港。" in (restored["body_md"] or "")
    again = engine.rollback(row["id"], engine.revisions(row["id"])[-1]["id"])
    assert again["unchanged"] is True


def test_scope_stays_unknown_when_the_previous_revision_has_no_snapshot(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,body_md)
           VALUES('c1','人物','云岚','active','{}','{}','他在南港。')"""
    )
    engine.db().execute(
        """INSERT INTO revisions(crystal_id,created_at,note)
           VALUES('c1','2026-11-06T12:00:00+00:00','正文')"""
    )
    engine.db().commit()
    engine._record_revision(engine.db(), "c1", "正文", change="又写了一句。")
    engine.db().commit()
    assert engine.revisions("c1")[-1]["scope"] == ""


def test_missing_scopes_are_filled_from_snapshots_once(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json)
           VALUES('c1','人物','云岚','active','{}','{}')"""
    )
    engine.db().execute(
        """INSERT INTO revisions(crystal_id,created_at,note,page_json)
           VALUES('c1','2026-11-06T12:00:00+00:00','开篇',?)""",
        ('{"name":"云岚","body":"","sketch":""}',),
    )
    engine.db().execute(
        """INSERT INTO revisions(crystal_id,created_at,note,page_json)
           VALUES('c1','2026-11-06T12:01:00+00:00','正文',?)""",
        ('{"name":"云岚","body":"他在。","sketch":""}',),
    )
    engine.db().commit()
    engine.backfill_revision_scopes(engine.db())
    engine.db().commit()
    scopes = [item["scope"] for item in engine.revisions("c1")]
    assert scopes[0] == "标题"
    assert scopes[1] == "正文"


def test_a_revision_keeps_one_sentence_and_clips_a_long_one(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json)
           VALUES('c1','人物','云岚','active','{}','{}')"""
    )
    engine.db().commit()
    engine._record_revision(
        engine.db(),
        "c1",
        "正文",
        change="删去：\n" + ("旧说法。" * 40),
        gist="因更新，所在地改成了北城。",
    )
    kept = engine.revisions("c1")[-1]
    assert kept["gist"] == "因更新，所在地改成了北城。"
    engine._record_revision(engine.db(), "c1", "正文", change="甲" * 90)
    engine.db().commit()
    clipped = engine.revisions("c1")[-1]["gist"]
    assert len(clipped) <= 80
    assert clipped == "甲" * 80


def test_a_gist_keeps_the_explanation_that_still_fits():
    from rogmem.engine import clip_revision_gist

    text = (
        "删去了北城的旧说法。因为后来的材料把当前所在地改成了南港，旧职称不再是当前。"
        + "后面整段正文不必出现在定位条上。" * 6
    )
    gist = clip_revision_gist(text)
    assert len(gist) <= 80
    assert gist.startswith("删去了北城的旧说法。")
    assert "因为后来的材料" in gist


def test_an_old_revision_still_points_at_a_short_sentence(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json)
           VALUES('c-old','人物','旧页','active','{}','{}')"""
    )
    engine.db().execute(
        """INSERT INTO revisions(crystal_id,created_at,note,change)
           VALUES('c-old','2026-11-06T12:00:00+00:00','正文','删去了北城的旧说法，因为后来的材料把当前所在地改成了南港，并且把已经结束的职称从当前职责里拿掉。后面还有很长的差异不必出现在定位条上，定位条只留前面这一句，其余留在修改内容里。')"""
    )
    engine.db().commit()
    item = engine.revisions("c-old")[-1]
    assert len(item["gist"]) <= 80
    assert item["gist"].startswith("删去了北城的旧说法")
    page = crystal_page(
        {"id": "c-old", "name": "旧页", "type": "人物", "status": "active", "status_label": "活跃", "summary": {}, "chapters": {}},
        [item],
        [],
        "",
        "active",
        "log",
    )
    assert "rev-rail" in page
    assert "rev-tick" in page
    assert f"id='rev-{item['id']}'" in page
    assert item["gist"] in page
    assert "摘要" in page


def test_a_revision_card_previews_the_words_and_the_detail_keeps_them():
    from rogmem.ui import _revision_item, revision_detail

    change = "改" * 260
    basis = "据" * 2500
    item = {
        "id": 7,
        "change": change,
        "basis": basis,
        "gist": "因更新，所在地改成了南港。",
        "created_at": "2026-11-08T01:02:03+00:00",
        "event_at": "2026-11-08T01:00:00+00:00",
        "actor": "蒸馏",
        "scope": "正文",
        "source_id": "src-1",
        "source_href": "/admin/sources/src-1",
    }
    html = _revision_item("page-1", item, name="云岚甲")
    assert "class='quiet rev-open'" in html
    assert html.index("详情") < html.index("撤销修订")
    assert "data-crystal='page-1'" in html and "data-rev='7'" in html
    assert "修订时间" in html and "修订人" in html and "修订范围" in html
    assert "摘要" in html and "因更新，所在地改成了南港。" in html
    assert "修订内容" in html and "修订依据" in html
    assert ("改" * 200) + "…" in html
    assert "改" * 201 not in html
    assert ("据" * 100) + "…" in html
    assert "据" * 101 not in html
    assert "template" not in html
    assert "打开出处" in html
    assert "确定撤销「云岚甲」" in html
    assert "onclick='post(" not in html
    detail = revision_detail(item)
    assert change in detail
    assert basis in detail
    assert ("改" * 200) + "…" not in detail
    assert ("据" * 100) + "…" not in detail

    short = _revision_item(
        "page-1",
        {
            "id": 8,
            "change": "删去了北城。",
            "basis": "材料写了这一处。",
            "created_at": "2026-11-08T01:02:03+00:00",
            "actor": "蒸馏",
            "scope": "正文",
        },
        name="云岚甲",
    )
    short_list = short.split("<template", 1)[0]
    assert "删去了北城。" in short_list
    assert "删去了北城。…" not in short_list
    assert "材料写了这一处。" in short_list
    assert "材料写了这一处。…" not in short_list


def test_opening_a_revision_returns_the_reading_the_page_only_starts(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from rogmem.app import mcp

    engine = _open(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json)
           VALUES('c-read','人物','云岚甲','active','{}','{}')"""
    )
    change = "改" * 260
    basis = "据" * 2500
    engine.db().execute(
        """INSERT INTO revisions(crystal_id,created_at,note,actor,change,basis,scope)
           VALUES('c-read','2026-11-06T12:00:00+00:00','正文','蒸馏',?,?,'正文')""",
        (change, basis),
    )
    engine.db().commit()
    rev = engine.revisions("c-read")[-1]
    page = crystal_page(
        {"id": "c-read", "name": "云岚甲", "type": "人物", "status": "active", "status_label": "活跃", "summary": {}, "chapters": {}},
        [rev],
        [],
        "",
        "active",
        "log",
    )
    assert ("改" * 200) + "…" in page and "改" * 201 not in page
    assert ("据" * 100) + "…" in page and "据" * 101 not in page
    client = TestClient(mcp.streamable_http_app())
    opened = client.get(f"/admin/api/crystals/c-read/revisions/{rev['id']}")
    assert opened.status_code == 200
    assert change in opened.text and basis in opened.text
    assert client.get("/admin/api/crystals/c-read/revisions/99999").status_code == 404


def test_the_revision_log_opens_one_detail_dialog():
    item = {
        "id": 3,
        "created_at": "2026-11-06T12:00:00+00:00",
        "actor": "蒸馏",
        "scope": "正文",
        "note": "正文",
        "change": "写入了一句。",
        "basis": "文件「南港简历.md」。",
        "gist": "写入了一句。",
        "source_id": "src-resume",
    }
    page = crystal_page(
        {"id": "c-detail", "name": "云岚甲", "type": "人物", "status": "active", "status_label": "活跃", "summary": {}, "chapters": {}},
        [item],
        [],
        "",
        "active",
        "log",
    )
    assert "id='rev-detail'" in page
    assert "修订详情" in page
    assert "id='rev-detail-close'" in page
    assert "template.rev-detail" not in page
    assert "formatSourceClocks(detailBody)" in page
    assert "/admin/api/crystals/" in page and "/revisions/" in page
    assert page.count("class='quiet rev-open'") == 1


def test_each_revision_has_a_tick_and_the_hover_wave_falls_off_by_the_fourth():
    revisions = [
        {
            "id": index + 1,
            "created_at": "2026-11-06T12:00:00+00:00",
            "actor": "蒸馏",
            "scope": "正文",
            "note": "正文",
            "change": f"改了第{index}处。",
            "basis": "材料写了这一处。",
        }
        for index in range(8)
    ]
    page = crystal_page(
        {"id": "c-wave", "name": "云岚甲", "type": "人物", "status": "active", "status_label": "活跃", "summary": {}, "chapters": {}},
        revisions,
        [],
        "",
        "active",
        "log",
    )
    assert page.count("class='rev-tick") == 8
    for index in range(8):
        assert f"data-target='rev-{index + 1}'" in page
        assert f"id='rev-{index + 1}'" in page
    assert ".rev-tick i" in page and "width: 4px" in page
    assert ".rev-tick.hot i" in page and "width: 12px" in page
    assert ".rev-tick.wave-1 i" in page and "width: 10px" in page
    assert ".rev-tick.wave-2 i" in page and "width: 8px" in page
    assert ".rev-tick.wave-3 i" in page and "width: 6px" in page
    assert 'tick.classList.toggle("wave-3", distance === 3)' in page


def test_the_integrate_prompt_asks_for_the_same_one_sentence():
    from rogmem.distill_resim import approved_integrate_prompts

    system, _user = approved_integrate_prompts()
    assert "gist" in system
    assert "不超过八十字" in system
    assert "删除了什么" not in system


def test_a_later_write_records_the_new_passage_not_the_pages_evidence_index(tmp_path, monkeypatch):
    """A revision points at the material for this write. It does not transcribe footnotes the page already carried."""
    engine = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "llm_target", lambda role: None)
    source = _source(engine, "南港简历.md")
    old_notes = "".join(
        f"[^old{i}]: 实 「旧引{i}只属于原来那一页。」\n" for i in range(40)
    )
    first_body = "他在南港写编辑器。[^1]\n\n[^1]: 实 「他在南港写编辑器。」\n" + old_notes
    engine._land_markdown_pages(
        source,
        [{"name": "云岚甲", "type": "人物", "body": first_body}],
    )
    row = engine.db().execute(
        "SELECT id FROM crystallizations WHERE name='云岚甲' AND status='active'"
    ).fetchone()
    later_body = first_body + "他后来去了北城。[^new]\n\n[^new]: 实 「他后来去了北城。」\n"
    engine._land_markdown_pages(
        source,
        [{"name": "云岚甲", "type": "人物", "body": later_body}],
    )
    latest = engine.revisions(row["id"])[-1]
    assert "南港简历.md" in latest["basis"]
    assert "他后来去了北城" in latest["basis"]
    assert "旧引0只属于原来那一页" not in latest["basis"]
    assert len(latest["basis"]) <= 80


def test_a_log_page_projects_the_claim_and_leaves_the_stored_corpus(tmp_path, monkeypatch):
    """Already-stored evidence indexes stay in the library. The log page shows one claim and a link."""
    engine = _open(tmp_path, monkeypatch)
    corpus = "文件「南港简历.md」。" + ("「旧引文不应整段重印。」" * 400)
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json)
           VALUES('c-log','人物','云岚甲','active','{}','{}')"""
    )
    engine.db().execute(
        """INSERT INTO revisions(crystal_id,created_at,note,actor,change,basis,source_id,scope)
           VALUES('c-log','2026-11-06T12:00:00+00:00','正文','蒸馏','写入了一句。',?,'src-resume','正文')""",
        (corpus,),
    )
    engine.db().commit()
    detail = {
        "id": "c-log",
        "name": "云岚甲",
        "type": "人物",
        "status": "active",
        "status_label": "活跃",
        "summary": {},
        "chapters": {},
    }
    page = crystal_page(
        detail,
        engine.revisions("c-log"),
        [],
        "",
        "active",
        "log",
    )
    engine.backfill_revision_basis(engine.db())
    engine.db().commit()
    stored = engine.db().execute("SELECT basis FROM revisions WHERE crystal_id='c-log'").fetchone()["basis"]
    assert "南港简历.md" in stored
    assert "旧引文不应整段重印。" * 10 not in stored
    assert len(stored) <= 80
    assert "南港简历.md" in page
    assert "打开出处" in page
    assert "旧引文不应整段重印。" * 10 not in page
    short = crystal_page(
        detail,
        [
            {
                "id": 1,
                "created_at": "2026-11-06T12:00:00+00:00",
                "note": "正文",
                "actor": "蒸馏",
                "change": "写入了一句。",
                "basis": "文件「南港简历.md」。",
                "source_id": "src-resume",
                "scope": "正文",
            }
        ],
        [],
        "",
        "active",
        "log",
    )
    assert len(page) - len(short) < 200
