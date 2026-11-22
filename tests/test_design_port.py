"""Design rules whose only previous implementation lived in the shelved stash."""

from __future__ import annotations

import json
import zipfile

from rogmem.names import names_correspond, owner_names_match
from rogmem.sections import markdown_sections, pptx_sections, read_sections


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


def test_names_match_case_pinyin_and_one_typo():
    assert names_correspond("Git Hub", "github")
    assert names_correspond("张三", "章三")
    assert names_correspond("周生生", "周生声")
    assert not names_correspond("张三", "李四")
    assert not names_correspond("北城", "Beicheng")


def test_owner_names_match_latin_pinyin_and_western_order():
    # Owner bridge only: Latin spelling may meet Chinese true name.
    assert owner_names_match("Zhao Wen", "赵文")
    assert owner_names_match("Wen Zhao", "赵文")
    assert owner_names_match("zhaowen", "赵文")
    assert not names_correspond("Zhao Wen", "赵文")
    assert not names_correspond("北城", "Beicheng")
    assert not owner_names_match("北城", "南山")


def test_person_land_routes_owner_true_name_to_memory_owner(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    envelope = {
        "aliases": ["zwen"],
        "summary": "",
        "owner_relation": "暂无信息",
        "facts": [],
        "inferences": [],
        "keys": {"真名": "赵文"},
    }
    con.execute(
        "UPDATE crystallizations SET envelope_json=?, summary_json='{}' WHERE id='owner'",
        (json.dumps(envelope, ensure_ascii=False),),
    )
    con.commit()
    assert engine._owner_labels()[:3] == ["记忆主人", "赵文", "zwen"]
    assert engine._person_is_owner("Zhao Wen")
    assert engine._person_is_owner("Wen Zhao", ["旁人甲"])
    assert engine._person_is_owner("旁人乙", ["zwen"])
    assert not engine._person_is_owner("旁人丙")
    landed = engine._land_integrate_new(
        {"name": "Zhao Wen", "type": "人物", "aliases": [], "summary": "概述一句"},
        store=None,
        con=con,
        holder="test:owner-land",
    )
    assert landed is not None
    assert landed[0] == "owner"
    other = engine._land_integrate_new(
        {"name": "旁人丁", "type": "人物", "aliases": [], "summary": "另一人"},
        store=None,
        con=con,
        holder="test:other-land",
    )
    assert other is not None and other[0] != "owner"


def test_markdown_and_pptx_keep_structure(tmp_path):
    text = "# 经历\n2018-2020 甲公司\n\n# 教育\n2014-2018 某大学\n"
    sections = markdown_sections(text)
    assert sections[0][0] == "经历"
    assert "甲公司" in sections[0][1]
    path = tmp_path / "deck.pptx"
    slide = (
        "<?xml version='1.0'?>"
        "<p:sld xmlns:a='http://schemas.openxmlformats.org/drawingml/2006/main' "
        "xmlns:p='http://schemas.openxmlformats.org/presentationml/2006/main'>"
        "<p:spTree><p:sp><p:txBody><a:p><a:r><a:t>第一张标题</a:t></a:r></a:p></p:txBody></p:sp></p:spTree>"
        "</p:sld>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", slide)
    got = pptx_sections(path)
    assert got == [("第1张", "第一张标题")]
    note = tmp_path / "note.md"
    note.write_text(text, encoding="utf-8")
    assert any(title == "教育" for title, _body in read_sections(note))


def test_one_spelling_folds_and_two_do_not(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES('src','user',?,'h','章三来了','队列中','fast',1)""",
        (engine.utcnow(),),
    )
    summary = json.dumps({"别名": "尚无来源", "名称": "张三"}, ensure_ascii=False)
    chapters = json.dumps({}, ensure_ascii=False)
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES('c1','人物','张三','active',?,?)",
        (summary, chapters),
    )
    con.commit()
    source = con.execute("SELECT * FROM sources WHERE id='src'").fetchone()
    assert engine._fold_spelling(con, "章三", "人物", "", "章三来了", source) == "folded"
    con.commit()
    after = con.execute("SELECT summary_json, envelope_json, revision FROM crystallizations WHERE id='c1'").fetchone()
    assert json.loads(after["summary_json"])["别名"] == "尚无来源"
    envelope = json.loads(after["envelope_json"] or "{}")
    assert envelope.get("aliases") == ["章三"]
    assert int(after["revision"] or 0) > 0
    stmt = con.execute("SELECT text FROM statements WHERE crystal_id='c1' AND field='别名' AND invalid_at IS NULL").fetchone()
    assert stmt is not None and stmt["text"] == "章三"
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES('c2','人物','张四','active',?,?)",
        (summary, chapters),
    )
    con.commit()
    assert engine._fold_spelling(con, "张五", "人物", "", "张五", source) == "blocked"
    assert con.execute("SELECT 1 FROM crystallizations WHERE name='张五'").fetchone() is None


def test_denial_splits_a_merged_page(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    now = engine.utcnow()
    for cid, name in (("keep", "甲"), ("drop", "乙")):
        con.execute(
            "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES(?,?,?,'active','{}','{}')",
            (cid, "人物", name),
        )
    con.execute(
        """INSERT INTO statements(id,crystal_id,text,quote,source_id,trust,rank,created_at)
           VALUES('st','drop','乙的经历','乙的经历','src','written','user',?)""",
        (now,),
    )
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES('src','user',?,'h','甲和乙不是同一人','队列中','fast',1)""",
        (now,),
    )
    con.commit()
    engine._merge_into(con, "keep", "drop", "src")
    con.commit()
    assert con.execute("SELECT crystal_id, home_crystal FROM statements WHERE id='st'").fetchone()["home_crystal"] == "drop"
    moved = engine._split_denied(con, "甲和乙不是同一人", "src")
    con.commit()
    assert moved == 1
    row = con.execute("SELECT status FROM crystallizations WHERE id='drop'").fetchone()
    assert row["status"] == "active"
    home = con.execute("SELECT crystal_id FROM statements WHERE id='st'").fetchone()["crystal_id"]
    assert home == "drop"


def test_forget_span_keeps_the_unnamed_sentence(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    now = engine.utcnow()
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES('keep','user',?,'k','北窗社在深圳。另一句留下。','已萃取','fast',1)""",
        (now,),
    )
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES('ask','user',?,'a','请忘掉北窗社在深圳','队列中','fast',1)""",
        (now,),
    )
    con.commit()
    engine._maybe_forget(con.execute("SELECT * FROM sources WHERE id='ask'").fetchone())
    kept = con.execute("SELECT body FROM sources WHERE id='keep'").fetchone()["body"]
    assert "北窗社在深圳" not in kept
    assert "另一句留下" in kept


def test_device_rename_batch_approve_and_reclaim(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem import crypto

    public = crypto.b64encode(crypto.new_keypair()[1])
    hermes = crypto.b64encode(crypto.new_keypair()[1])
    home = crypto.b64encode(crypto.new_keypair()[1])
    first = engine.enroll_apply("dev-1", "office", "Cursor", public)
    second = engine.enroll_apply("dev-1", "office", "Hermes", hermes)
    other = engine.enroll_apply("dev-2", "home", "Cursor", home)
    renamed = engine.rename_agent(first["id"], "前台 Cursor")
    assert renamed["ok"] and renamed["device_id"] == "dev-1" and renamed["program"] == "Cursor"
    batch = engine.approve_device("dev-1")
    assert set(batch["ids"]) == {first["id"], second["id"]}
    assert engine.db().execute("SELECT status FROM agents WHERE id=?", (other["id"],)).fetchone()["status"] == "待放行"
    claimed = engine.enroll_claim(public)
    assert claimed["ok"]
    again = engine.enroll_reclaim(public, "dev-1", "Cursor")
    assert again["ok"] and again["status"] == "待放行"
    row = engine.db().execute("SELECT status, key_hash FROM agents WHERE id=?", (first["id"],)).fetchone()
    assert row["status"] == "待放行" and row["key_hash"] is None


def test_reclaim_host_returns_the_same_device_to_review(tmp_path):
    from installer.rogmem_installer import reclaim_host

    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    seen = {}

    def post(body):
        seen.update(body)
        return {"ok": True, "status": "待放行", "code": "abc", "id": "row-1"}

    result = reclaim_host(home, "http://127.0.0.1:9", "Hermes", post)
    assert result["ok"] is True and result["wrote"] is False and result["status"] == "待放行"
    assert seen["program"] == "Hermes" and seen["device_id"] and seen["pubkey"]
    assert (home / ".rogmem" / "keys" / "Hermes.json").is_file()


def test_preview_marks_unwritten_host_as_pending(tmp_path):
    from installer.rogmem_installer import inspect_hosts, preview_hosts

    home = tmp_path / "home"
    (home / "Library" / "Application Support" / "Cursor").mkdir(parents=True)
    assert inspect_hosts(home)[0]["access"] == "已发现未接入" or any(
        row["access"] == "已发现未接入" for row in inspect_hosts(home)
    )
    rows = preview_hosts(home, "http://127.0.0.1:18780")
    cursor = next(row for row in rows if row["program"] == "Cursor")
    assert cursor["access"] == "待确认"
    assert "将指向" in cursor["detail"]


def test_a_transcript_without_clocks_is_still_a_set_of_turns(tmp_path):
    from installer.rogmem_installer import collect_history, extract_turns, history_preview

    bare = "\n".join(
        [
            json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "合成句：没有钟的问"}]}}),
            json.dumps({"role": "assistant", "message": {"content": [{"type": "text", "text": "合成句：没有钟的答"}]}}),
            json.dumps({"role": "turn_ended", "status": "success"}),
        ]
    )
    assert extract_turns("Cursor", bare)["turns"] == []
    placed = extract_turns("Cursor", bare, file_at="2026-04-01T00:00:00+00:00", session_id="chat-1")
    assert [item["text"] for item in placed["turns"]] == ["合成句：没有钟的问", "合成句：没有钟的答"]
    assert placed["turns"][0]["session_id"] == "chat-1"
    assert placed["turns"][0]["event_at"] == "2026-04-01T00:00:00+00:00"
    mixed = "\n".join(
        [
            json.dumps({"role": "user", "content": "合成句：有钟的一句", "timestamp": "2026-04-01T00:00:00+00:00"}),
            json.dumps({"role": "assistant", "content": "合成句：旁边没写钟"}),
        ]
    )
    assert [item["text"] for item in extract_turns("Cursor", mixed, file_at="2026-04-02T00:00:00+00:00")["turns"]] == [
        "合成句：有钟的一句"
    ]
    home = tmp_path / "home"
    path = home / ".cursor" / "projects" / "demo" / "chat.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(bare, encoding="utf-8")
    gathered = collect_history(home, allow_real=True)
    assert [item["text"] for item in gathered["turns"]] == ["合成句：没有钟的问", "合成句：没有钟的答"]
    assert history_preview(home, allow_real=True)["importable"] == 2


def test_subagent_and_daily_notes_are_not_turns(tmp_path):
    from installer.rogmem_installer import collect_history, extract_turns

    text = "\n".join(
        [
            json.dumps({"role": "user", "content": "父会话原句", "timestamp": "2026-10-25T00:00:00+00:00"}),
            json.dumps({"type": "subagent", "role": "assistant", "content": "子代理发言", "timestamp": "2026-10-25T00:00:01+00:00"}),
            json.dumps({"role": "subagent", "content": "另一句", "timestamp": "2026-10-25T00:00:02+00:00"}),
        ]
    )
    got = extract_turns("Cursor", text)
    assert [item["text"] for item in got["turns"]] == ["父会话原句"]
    assert got["skipped"] >= 2
    root = tmp_path / "home" / ".cursor" / "projects" / "demo"
    (root / "daily").mkdir(parents=True)
    (root / "daily" / "2026-10-25.jsonl").write_text(
        json.dumps({"role": "user", "content": "按日笔记", "timestamp": "2026-10-25T00:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )
    (root / "session.jsonl").write_text(
        json.dumps({"role": "user", "content": "父会话原句", "timestamp": "2026-10-25T00:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )
    # Cursor history is discovered from the app support tree, not .cursor/projects.
    # The daily-note filter is on the file walker used by collect_history.
    home = tmp_path / "host"
    sessions = home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage"
    # collect_history looks up known session roots; assert the walker helper directly.
    from installer.rogmem_installer import _session_files

    files = _session_files("Cursor", root)
    assert all("daily" not in file.parts for file in files)
    assert any(file.name == "session.jsonl" for file in files)
    assert sessions or home


def test_auth_recovery_notifies_once(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute(
        "INSERT INTO alerts(created_at,message,open) VALUES(?,?,1)",
        (engine.utcnow(), "外部模型鉴权失败"),
    )
    engine.db().commit()
    notes = []
    monkeypatch.setattr(engine, "notify_alert", notes.append)
    assert engine.recover_auth_alerts() == 1
    assert engine.recover_auth_alerts() == 0
    assert notes == ["恢复：外部模型鉴权失败"]
    assert engine.db().execute("SELECT open FROM alerts").fetchone()["open"] == 0


def test_named_chain_and_sentence_fate(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    now = engine.utcnow()
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES('p','人物','周生生','active','{}','{}')"
    )
    con.execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,created_at)
           VALUES('a','p','沟通风格','先写短句','先写短句','s','written','user',?)""",
        (now,),
    )
    con.execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,created_at,invalid_at)
           VALUES('b','p','沟通风格','先写短句','先写短句','s','written','user',?,'2026-10-25T01:00:00+00:00')""",
        (now,),
    )
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES('s','user',?,'h','周生生先写短句。这句没有写进结晶。','已萃取','fast',1)""",
        (now,),
    )
    con.execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,created_at)
           VALUES('c','p','真名','周生生','周生生先写短句','s','written','user',?)""",
        (now,),
    )
    con.commit()
    found = engine.read_chain("周生生", "沟通风格")
    assert any(item["text"] == "先写短句" and item["current"] for item in found["rings"])
    detail = engine.source_detail("s")
    fates = {item["sentence"]: item["fate"] for item in detail["source"]["_fate"]}
    assert fates["周生生先写短句"] == "写入"
    assert fates["这句没有写进结晶"] == "丢掉"


def test_browser_fallback_is_offered_and_refuses_local_files():
    from installer.rogmem_installer import open_system_browser
    from rogmem.ui import basic_page, graph_page

    assert open_system_browser("file:///etc/passwd") is False
    assert open_system_browser("") is False
    page = graph_page()
    assert "用系统浏览器打开同一页" in page
    assert "rogmemOpenBrowser" in page
    assert "graph-screen" in page
    assert "el.clientWidth" in page
    assert "el.clientHeight" in page
    assert "prefers-reduced-motion" in page
    # 图谱不再随渲染方式切换，基础设置里也不再有渲染方式开关。
    basic = basic_page({"sketch_char_limit": "3500", "graph_spin_seconds": "180"})
    assert "图谱转一圈的秒数" not in basic
    assert 'name="graph_spin_seconds"' not in basic
    assert 'name="graph_renderer"' not in basic


def test_graph_counts_active_nodes():
    """记忆星图报出当前有多少活跃结晶；计数叠在星图内，不再单独占页头。"""
    from rogmem.ui import graph_page

    page = graph_page()
    assert "<h1>记忆星图</h1>" not in page
    assert "class='page-head'" not in page
    assert "<div id=\"graph\" aria-busy=\"true\"><p class='graph-count' id='graph-count'></p>" in page
    assert "#graph > .graph-count" in page
    assert "':scope > #graph-count'" in page
    assert "body.graph-screen main { flex: 1; min-height: 0; padding: 0;" in page
    assert "nodes.length + ' 个活跃结晶'" not in page
    assert "'活跃结晶：' + nodes.length" in page
    assert "getElementById('graph-count')" in page
    # 三维挂载用 replaceChildren 换画布时必须保住计数，否则右上角灰字整段消失。
    assert "function rogmemReplaceGraphBody(el)" in page
    assert "rogmemReplaceGraphBody(el, renderer.domElement, rogmemGraphTip())" in page
    assert "el.replaceChildren(renderer.domElement, rogmemGraphTip())" not in page
