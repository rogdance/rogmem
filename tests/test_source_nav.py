"""Top nav shows session and file import directly. Detail is the text and the crystals that cite it."""

from rogmem.ui import source_detail, sources_page


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def test_a_failed_stage_is_not_painted_green_by_the_next_one():
    from rogmem.engine import _begin_attempt, _compose_pipeline

    assert _compose_pipeline("并页:fail", "速写", "run") == "并页|解析:ok,草稿:ok,并页:fail"
    assert _compose_pipeline("并页|解析:ok,草稿:ok,并页:fail", "完成", "ok") == "并页|解析:ok,草稿:ok,并页:fail"
    retry = _begin_attempt("解析:ok,草稿:ok,并页:fail")
    assert retry == "并页|解析:ok,草稿:run"
    again = _compose_pipeline(retry, "并页", "run")
    assert again.endswith("并页:retry")
    assert "润色" not in again.split("|", 1)[-1]


def test_a_source_row_shows_the_stage_lamps():
    from rogmem.ui import flow_states

    assert [state for _name, state in flow_states("并页:run", "萃取中")] == ["ok", "ok", "run", "wait"]
    assert [state for _name, state in flow_states("润色:fail", "萃取失败")] == ["ok", "ok", "ok", "fail"]
    assert [state for _name, state in flow_states("", "萃取已结晶")] == ["ok", "ok", "ok", "ok"]
    assert [state for _name, state in flow_states("解析:ok,草稿:ok,并页:ok,润色:fail", "萃取失败")] == [
        "ok",
        "ok",
        "ok",
        "fail",
    ]
    html = sources_page(
        [
            {
                "id": "s",
                "kind": "file",
                "preview": "",
                "filename": "简历.md",
                "device_name": "",
                "agent_name": "",
                "status": "萃取中",
                "pipeline": "并页:run",
                "ingested_at": "",
                "project_ref": "",
                "note": "",
            }
        ],
        "file",
        {},
        {},
    )
    assert "lamp ok" in html
    assert "lamp run" in html
    assert "lamp wait" in html
    assert "aria-label='并页：进行中'" in html
    assert "lamp-pop" in html
    from rogmem.ui import flow_html

    branched = flow_html("解析:ok,草稿:run~向量:ok", "萃取中", "")
    assert "class='branch'" in branched
    assert "aria-label='向量化：通过'" in branched
    assert "aria-label='草稿：进行中'" in branched
    waiting = flow_html("解析:ok~向量:run", "已解析待向量化", "")
    assert "aria-label='解析：通过'" in waiting
    assert "aria-label='向量化：进行中'" in waiting
    assert "aria-label='草稿：未到'" in waiting


def test_an_old_stage_name_is_rewritten_on_open(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pipeline)
           VALUES('old','file','2026-01-01T00:00:00+00:00','h','正文','萃取失败','fast','整合:fail')"""
    )
    engine.db().commit()
    engine.migrate_source_statuses(engine.db())
    engine.db().commit()
    stored = engine.db().execute("SELECT pipeline FROM sources WHERE id='old'").fetchone()["pipeline"]
    assert stored == "并页:fail"
    kept = engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pipeline)
           VALUES('kept','file','2026-01-01T00:00:00+00:00','h2','正文','萃取失败','fast','整合|解析:ok,整合:fail~向量:ok')"""
    )
    del kept
    engine.db().commit()
    engine.migrate_source_statuses(engine.db())
    engine.db().commit()
    compound = engine.db().execute("SELECT pipeline FROM sources WHERE id='kept'").fetchone()["pipeline"]
    assert compound == "并页|解析:ok,并页:fail~向量:ok"


def _row(**extra) -> dict:
    row = {
        "id": "s",
        "kind": "user",
        "preview": "一句原话",
        "filename": "简历.md",
        "device_name": "独有设备甲",
        "agent_name": "独有设备甲 · Cursor",
        "status": "完成",
        "pipeline": "",
        "ingested_at": "",
        "project_ref": "https://example.com/a-very-long-project-name",
        "note": "",
    }
    row.update(extra)
    return row


def test_session_list_hides_the_device_column_and_keeps_the_filter():
    facets = {
        "devices": ["独有设备甲"],
        "agents": ["独有设备甲 · Cursor"],
        "projects": ["https://example.com/a-very-long-project-name"],
    }
    session = sources_page([_row()], "session", {}, facets)
    head = session.split("<table class='sources'>", 1)[1].split("</tr>", 1)[0]
    assert head.count("<th") == 11
    assert "设备名称" not in head
    assert "智能体" in head
    assert "Token" in head
    assert "金额" in head
    device = session.split("<select name='device'>", 1)[1].split("</select>", 1)[0]
    assert "独有设备甲" in device
    style = " ".join(session.split("</style>", 1)[0].split())
    assert "form.source-filters { --filter-box: 8.5em; --filter-search: 12em; }" in style
    assert "form.source-filters select { field-sizing: fixed; width: var(--filter-box);" in style
    assert "form.source-filters .pick { position: relative; display: block; width: var(--filter-box); flex: none; }" in style
    assert "form.source-filters .pick-menu { position: absolute; z-index: 40; top: calc(100% + 4px); left: 0; width: max-content; min-width: 100%;" in style
    assert 'pick.className = "pick"' in session
    assert 'menu.style.width = "max-content"' in session
    files = sources_page([_row(kind="file")], "file", {}, facets)
    file_head = files.split("<table class='sources'>", 1)[1].split("</tr>", 1)[0]
    assert "设备名称" in file_head
    assert file_head.count("<th") == 12
    assert "Token" in file_head
    assert "金额" in file_head
    detail = source_detail(_row(), [], None, "session")
    assert "设备名称" in detail
    assert "独有设备甲" in detail


def test_session_and_file_are_top_level_and_drop_the_old_name():
    session = sources_page([], "session", {}, {})
    files = sources_page([], "file", {}, {})
    assert "<h1>情景会话</h1>" not in session
    assert "<h1>知识文件</h1>" not in files
    assert "知识文件" in files.split("<nav>", 1)[1].split("</nav>", 1)[0]
    assert "原始记忆" not in session
    assert "原始记忆" not in files
    assert "href='/admin/sources?tab=session'" in session
    assert "href='/admin/sources?tab=file'" in session
    assert "class='tabs'" not in session
    assert "class='tabs'" not in files
    assert "这一栏还没有情景会话。" in session
    assert "这一栏还没有文件。" in files
    assert "class='on'" in session
    assert "情景会话" in session.split("class='on'", 1)[1][:40]
    assert "关联工程" in session
    assert "关联项目" not in session
    assert "关联工程" in files


def test_file_detail_is_the_text_and_the_crystals_that_cite_it():
    row = {
        "id": "s1",
        "kind": "file",
        "filename": "note.md",
        "body": "# 标题\n第一句带格式。\n第二句还在。",
        "status": "萃取已结晶",
        "coverage": "partial",
        "_part": "text",
        "_cited": [
            {"id": "c1", "name": "南港书院", "passages": ["他在南港写编辑器。"]},
        ],
    }
    original = source_detail(row, [], None, "file")
    assert "原文解析" in original
    assert "关联结晶" in original
    assert "清洗文本" not in original
    assert "写入内容" not in original
    assert "产出结晶" not in original
    assert "# 标题" in original
    assert "<h1>知识文件</h1>" not in original
    assert "class='page-head'><a href='/admin/sources?tab=file'>返回列表</a><button" in original
    cited = source_detail({**row, "_part": "crystals"}, [], None, "file")
    assert "南港书院" in cited
    assert "他在南港写编辑器。" in cited
    assert "href='/admin/crystals/c1'" in cited
    session = source_detail({**row, "kind": "user", "_part": "text"}, [], None, "session")
    assert "关联工程" in session
    assert "关联项目" not in session
    assert "原文解析" in session
    assert "关联结晶" in session
    assert "清洗文本" not in session
    assert "<h1>情景会话</h1>" not in session


def test_a_material_lists_every_crystal_that_used_it(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,filename)
           VALUES('src-1','file','2026-11-06T00:00:00+00:00','h','他在南港写编辑器。','萃取已结晶','fast','note.md')"""
    )
    engine._insert_crystal_page(engine.db(), "old", "组织", "南港书院", holder="test")
    engine._insert_crystal_page(engine.db(), "made", "人物", "云岚甲", holder="test")
    engine._insert_crystal_page(engine.db(), "other", "产品", "旁的产品", holder="test")
    engine.db().execute(
        "UPDATE crystallizations SET body_md=? WHERE id='made'",
        ("他在南港写编辑器。[^1]\n\n[^1]: 实 /admin/sources/src-1 「他在南港写编辑器。」\n",),
    )
    engine.db().execute(
        """INSERT INTO revisions(crystal_id,created_at,note,source_id)
           VALUES('old','2026-11-06T00:00:00+00:00','正文','src-1')"""
    )
    engine.db().execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,created_at)
           VALUES('st-1','made','经历','他在南港写编辑器。','他在南港写编辑器。','src-1','written','file','2026-11-06T00:00:00+00:00')"""
    )
    engine.db().commit()
    cited = {item["name"]: item for item in engine.cited_crystals("src-1")}
    assert set(cited) == {"南港书院", "云岚甲"}
    assert cited["南港书院"]["passages"] == []
    assert "他在南港写编辑器。" in cited["云岚甲"]["passages"]
    page = source_detail(engine.source_detail("src-1")["source"], [], None, "file")
    assert "关联结晶" in page
    crystals = source_detail({**engine.source_detail("src-1")["source"], "_part": "crystals"}, [], None, "file")
    assert "南港书院" in crystals
    assert "云岚甲" in crystals
    assert "旁的产品" not in crystals


def test_the_file_list_does_not_wait_on_the_write_lock(tmp_path, monkeypatch):
    import threading
    import time

    engine = _open(tmp_path, monkeypatch)
    started = threading.Event()
    release = threading.Event()

    def hold():
        engine.LOCK.acquire()
        started.set()
        release.wait(5)
        engine.LOCK.release()

    thread = threading.Thread(target=hold)
    thread.start()
    assert started.wait(2)
    began = time.time()
    listed = engine.list_sources("file")
    waited = time.time() - began
    release.set()
    thread.join()
    assert waited < 0.5
    assert listed["rows"] == []


def test_embedding_a_file_does_not_hold_the_write_lock(tmp_path, monkeypatch):
    import threading

    engine = _open(tmp_path, monkeypatch)
    seen = []

    def embed(_text):
        def probe():
            free = engine.LOCK.acquire(blocking=False)
            if free:
                engine.LOCK.release()
            seen.append(free)

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        return None

    monkeypatch.setattr(engine, "_embed_one", embed)
    path = tmp_path / "scan" / "南港.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("他在南港写编辑器。这句话单独成段，用来切出可以向量化的一块。", encoding="utf-8")
    opened = engine.ingest_file(path)
    assert opened["ok"] is True
    assert seen and all(seen)


def test_a_failed_merge_is_retried_without_drafting_again(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,body_md)
           VALUES('page-nan','人物','南港','active','{}','{}','旧的说法。')"""
    )
    engine.db().commit()
    calls = {"draft": 0, "merge": 0}

    def produce(_text, _kind="", **_kwargs):
        calls["draft"] += 1
        return {"payload_kind": "markdown", "pages": [{"name": "南港", "type": "人物", "body": "他在南港写编辑器。"}]}

    def merge(_old, new, _material, *_args, **_kwargs):
        calls["merge"] += 1
        if calls["merge"] == 1:
            raise engine.ModelCallFailed("The read operation timed out")
        return new, [], "", "", False, "", ""

    monkeypatch.setattr(engine, "_produce_distill_payload", produce)
    monkeypatch.setattr(engine, "_merge_existing_page", merge)
    monkeypatch.setattr(engine, "_ask_sketch", lambda *_args, **_kwargs: "")
    path = tmp_path / "scan" / "材料.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("他在南港写编辑器。", encoding="utf-8")
    opened = engine.ingest_file(path)
    assert engine.pump_one() == opened["id"]
    assert calls["draft"] == 1
    assert calls["merge"] == 1
    row = engine.db().execute("SELECT pipeline, stage_json FROM sources WHERE id=?", (opened["id"],)).fetchone()
    assert "并页" in (row["pipeline"] or "")
    assert "润色" not in (row["pipeline"] or "")
    assert "draft" in (row["stage_json"] or "")
    engine.db().execute("UPDATE queue SET state='queued', next_at=NULL WHERE source_id=?", (opened["id"],))
    engine.db().commit()
    assert engine.pump_one() == opened["id"]
    assert calls["draft"] == 1
    assert calls["merge"] == 2
    stored = engine.db().execute("SELECT body_md FROM crystallizations WHERE name='南港'").fetchone()
    assert stored is not None and "他在南港写编辑器。" in stored["body_md"]
    done = engine.db().execute("SELECT pipeline, stage_json FROM sources WHERE id=?", (opened["id"],)).fetchone()
    assert (done["pipeline"] or "").startswith("完成:ok")
    assert "润色" not in (done["pipeline"] or "")
    assert not done["stage_json"]


def _cell(html: str, kind: str) -> str:
    return html.split(f"<td class='{kind}'>", 1)[1].split("</td>", 1)[0]


def test_list_cells_show_thirty_characters_and_keep_the_rest_for_hover():
    long_body = "前十个字就换行\n" + ("后" * 40)
    long_note = "错" * 20 + "<script>alert(1)</script>" + ("误" * 20)
    session = sources_page(
        [
            _row(preview=long_body, note=long_note),
            _row(id="short", preview="一句原话", note="覆盖未齐"),
        ],
        "session",
        {},
        {},
    )
    name = _cell(session, "name")
    note = _cell(session, "note")
    assert name.startswith("<a class='clip' ")
    assert "前十个字就换行 " + ("后" * 22) + "…" in name
    assert "<span class='clip-full' hidden>" + long_body + "</span>" in name
    assert "clip-shown" in note and "…" in note
    assert "&lt;script&gt;" in note and "<script>" not in note
    assert long_note not in note
    short_name = session.split("<td class='name'>", 2)[2]
    assert short_name.startswith("<a href=")
    assert ">一句原话</a>" in short_name
    assert "class='clip'" not in short_name.split("</td>", 1)[0]
    assert ">覆盖未齐</td>" in session
    assert "clip-pop" in session and "placeClip" in session
    filename = "文" * 40 + ".md"
    files = sources_page([_row(kind="file", filename=filename, note=long_note)], "file", {}, {})
    file_name = _cell(files, "name")
    file_note = _cell(files, "note")
    assert filename in file_name and "clip" not in file_name
    assert file_note.startswith("<span class='clip'")
    assert "错" * 20 + "&lt;script&gt;" in file_note


def test_a_session_list_keeps_the_whole_body_and_a_file_list_does_not(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    body = "句" * 120
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES('talk','user','2026-01-01T00:00:00+00:00','h',?,'已萃取','fast',1)""",
        (body,),
    )
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,filename)
           VALUES('doc','file','2026-01-02T00:00:00+00:00','h2',?,'已萃取','fast',1,'笔记.md')""",
        ("字" * 120,),
    )
    engine.db().commit()
    assert engine.list_sources("session")["rows"][0]["preview"] == body
    assert engine.list_sources("file")["rows"][0]["preview"] == "字" * 80
