"""Historical sessions carry two clocks. Current state follows when the words were said."""

from __future__ import annotations

import json

from rogmem.prompts import (
    INTEGRATE_PAGE_SYSTEM,
    clock_lines,
    distill_header,
    worker_prompts,
)


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    return engine


def test_a_message_clock_is_when_it_was_said_and_a_file_clock_is_not():
    from installer.rogmem_installer import extract_turns

    spoken = "\n".join(
        [
            json.dumps(
                {
                    "role": "user",
                    "message": {
                        "createdAt": "2024-05-01T08:00:00+00:00",
                        "content": [{"type": "text", "text": "合成句：北窗的灯还亮着。"}],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "session_meta",
                    "createdAt": "2026-11-08T00:00:00+00:00",
                    "role": "user",
                    "content": "合成句：这行不是发言。",
                }
            ),
        ]
    )
    turns = extract_turns("Cursor", spoken, file_at="2026-11-08T00:00:00+00:00")["turns"]
    assert [item["text"] for item in turns] == ["合成句：北窗的灯还亮着。"]
    assert turns[0]["event_at"] == "2024-05-01T08:00:00+00:00"
    assert turns[0]["event_basis"] == "utterance"

    bare = "\n".join(
        [
            json.dumps({"type": "session_meta", "createdAt": "2026-11-08T00:00:00+00:00"}),
            json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "合成句：没有钟的问"}]}}),
            json.dumps({"role": "assistant", "message": {"content": [{"type": "text", "text": "合成句：没有钟的答"}]}}),
        ]
    )
    placed = extract_turns("Cursor", bare, file_at="2026-04-01T00:00:00+00:00", session_id="chat-1")
    assert [item["text"] for item in placed["turns"]] == ["合成句：没有钟的问", "合成句：没有钟的答"]
    assert {item["event_basis"] for item in placed["turns"]} == {"file"}
    assert placed["turns"][0]["event_at"] == "2026-04-01T00:00:00+00:00"


def test_ingress_keeps_the_basis_apart_from_the_two_clocks():
    from rogmem.ingress import envelopes_from_turns, turn_from_envelope

    envelopes = envelopes_from_turns(
        [
            {
                "text": "合成句：旧项目通宵那一晚。",
                "role": "user",
                "event_at": "2024-05-01T08:00:00+00:00",
                "event_basis": "utterance",
                "session_id": "s",
                "seq": 1,
            }
        ]
    )
    turn = turn_from_envelope(envelopes[0])
    assert turn["event_at"] == "2024-05-01T08:00:00+00:00"
    assert turn["event_basis"] == "utterance"
    assert "ingested_at" not in turn


def test_history_stores_speech_time_apart_from_when_it_arrived(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO agents(id,device_id,device_name,program,display_name,status,pubkey,created_at)
           VALUES('ag','dev','机','Cursor','Cursor','启用','k',?)""",
        (now,),
    )
    engine.db().commit()
    agent = engine.db().execute("SELECT * FROM agents WHERE id='ag'").fetchone()
    spoken = "2024-05-01T08:00:00+00:00"
    engine.ingest_history(
        [
            {
                "role": "user",
                "text": "合成句：旧项目通宵那一晚。",
                "event_at": spoken,
                "event_basis": "utterance",
                "session_id": "s-old",
                "seq": 1,
            }
        ]
    )
    row = engine.db().execute("SELECT event_at, ingested_at, event_basis FROM sources WHERE session_id='s-old'").fetchone()
    assert row["event_at"] == spoken
    assert row["ingested_at"] != spoken
    assert row["event_basis"] == "utterance"

    live = engine.submit_text(
        kind="user",
        text="合成句：这一句没有带发言时间。",
        session_id="s-live",
        turn_id="t-live",
        seq=1,
        agent=agent,
        event_at=None,
        project=None,
    )
    stored = engine.db().execute(
        "SELECT event_at, ingested_at FROM sources WHERE id=?",
        (live["id"],),
    ).fetchone()
    assert not (stored["event_at"] or "").strip()
    assert stored["ingested_at"]


def test_header_names_both_clocks_and_says_what_each_one_is():
    spoken = clock_lines(event_at="2024-05-01T08:00:00+00:00", ingested_at="2026-11-08T00:00:00+00:00", event_basis="utterance")
    assert any(line.startswith("事件时间：2024-05-01T08:00:00+00:00") and "不要用提交时间" in line for line in spoken)
    assert any(line.startswith("提交时间：2026-11-08T00:00:00+00:00") and "进入记忆库" in line for line in spoken)
    coarse = clock_lines(event_at="2026-11-08T00:00:00+00:00", ingested_at="2026-11-08T00:00:01+00:00", event_basis="file")
    assert any("文件最后写入时间" in line and "不能单独证明" in line for line in coarse)
    missing = clock_lines(event_at="", ingested_at="2026-11-08T00:00:00+00:00", event_basis="")
    assert any(line.startswith("事件时间：未记下") and "不要用提交时间填上" in line for line in missing)
    header = distill_header(
        material_kind="会话",
        speaker="记忆主人",
        owner_name="云岚甲",
        owner_aliases="",
        source_id="src",
        filename="",
        event_at="2024-05-01T08:00:00+00:00",
        ingested_at="2026-11-08T00:00:00+00:00",
        event_basis="utterance",
    )
    assert "事件时间：2024-05-01T08:00:00+00:00" in header
    assert "提交时间：2026-11-08T00:00:00+00:00" in header


def test_draft_and_merge_see_speech_time_against_the_page(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute("UPDATE crystallizations SET body_md=? WHERE id='owner'", ("他现在关注南港书院。",))
    engine.db().execute(
        "INSERT INTO revisions(crystal_id,created_at,note,event_at) VALUES('owner',?,?,?)",
        ("2026-11-01T00:00:00+00:00", "正文", "2026-11-01T00:00:00+00:00"),
    )
    engine.db().execute(
        """INSERT INTO sources(id,kind,session_id,event_at,ingested_at,event_basis,content_hash,body,status,lane,pair_ready)
           VALUES('src-old','user','s-old','2024-05-01T08:00:00+00:00','2026-11-08T00:00:00+00:00','utterance','h',
                  '合成句：那时候喜欢通宵改旧项目。','蒸馏中','p1',1)"""
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-old'").fetchone()
    seen: list[str] = []

    def fake_complete(_base, _key, payload, **_kwargs):
        user = payload["messages"][1]["content"]
        seen.append(user)
        if "已有这一页" in user:
            return json.dumps(
                {"page": "他现在关注南港书院。更早的时候他通宵改过旧项目。", "conflicts": [], "gist": "补了一句早年的事。"},
                ensure_ascii=False,
            ), "stop"
        if user.startswith("材料类型") or user.startswith("事件时间") or "材料类型：" in user:
            return "# 记忆主人\n类型：人物\n\n更早的时候他通宵改过旧项目。\n", "stop"
        return user, "stop"

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m"))
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    engine._distill(source, 1)
    draft = next(item for item in seen if "材料类型：" in item)
    merge = next(item for item in seen if "已有这一页" in item)
    assert "事件时间：2024-05-01T08:00:00+00:00" in draft
    assert "提交时间：2026-11-08T00:00:00+00:00" in draft
    assert "最晚事件时间：2026-11-01T00:00:00+00:00" in draft
    assert "事件时间：2024-05-01T08:00:00+00:00" in merge
    assert "提交时间：2026-11-08T00:00:00+00:00" in merge
    assert "最晚事件时间：2026-11-01T00:00:00+00:00" in merge
    assert "2026-11-08T00:00:00+00:00" not in draft.split("事件时间：", 1)[1].split("\n", 1)[0]
    assert "只因为名字或别名接近" in merge


def test_a_different_subject_is_stored_as_its_own_page(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = "他现在关注南港书院。[^1]\n\n[^1]: 实 「他现在关注南港书院。」"
    new = "北城灯塔是另一家。[^2]\n\n[^2]: 实 「北城灯塔是另一家。」"

    def fake_complete(_base, _key, _payload, **_kwargs):
        return (
            json.dumps(
                {
                    "same": "否",
                    "name": "北城灯塔",
                    "type": "组织",
                    "page": new,
                    "conflicts": [],
                    "gist": "名字接近，但是另一家。",
                },
                ensure_ascii=False,
            ),
            "stop",
        )

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m") if role == "distill" else None)
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    page, conflicts, note, _gist, separate, name, ctype = engine._merge_existing_page(
        old,
        new,
        "合成句。",
        {
            "event_at": "2024-05-01T08:00:00+00:00",
            "ingested_at": "2026-11-08T00:00:00+00:00",
            "event_basis": "utterance",
        },
        "2026-11-01T00:00:00+00:00",
    )
    assert separate is True
    assert name == "北城灯塔"
    assert ctype == "组织"
    assert page.strip() == new.strip()
    assert "南港书院" not in page
    assert note == ""
    assert conflicts == []

    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,body_md)
           VALUES('page-old','组织','南港书院','active','{}','{}',?)""",
        (old,),
    )
    engine.db().execute(
        """INSERT INTO sources(id,kind,event_at,ingested_at,event_basis,content_hash,body,status,lane,pair_ready)
           VALUES('src-sep','file','2026-11-09T00:00:00+00:00','2026-11-09T00:00:00+00:00','file','h',
                  '合成句。','蒸馏中','slow',1)"""
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-sep'").fetchone()
    engine._land_markdown_pages(source, [{"name": "南港书院", "type": "组织", "body": new}])
    kept = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='page-old'").fetchone()
    opened = engine.db().execute("SELECT body_md, type FROM crystallizations WHERE name='北城灯塔'").fetchone()
    assert kept["body_md"].strip() == old.strip()
    assert opened is not None
    assert opened["type"] == "组织"
    assert "北城灯塔是另一家。" in opened["body_md"]
    assert "南港书院" not in opened["body_md"]


def test_prompts_keep_current_state_on_event_time():
    prompts = worker_prompts(sketch_limit=3500)
    principle = "当前态只跟着事件时间走"
    assert principle in prompts["distill-user"]
    assert principle in INTEGRATE_PAGE_SYSTEM
    assert "后到的旧材料不能把已经由更晚日期说定的当前态重新打开" not in prompts["distill-user"]
    assert "更新，是事件时间更晚的说法把当前态说定了" in prompts["distill-user"]
    assert "事件时间" in prompts["distill-user"] and "提交时间" in prompts["distill-user"]
    assert "prose" not in prompts
    assert "只留还在生效的后一种" in prompts["sketch-主人"]
    assert "冲淡" not in prompts["sketch-主人"]


def test_an_older_utterance_does_not_replace_a_later_focus(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)

    def write(sid: str, text: str, when: str, basis: str) -> None:
        engine.db().execute(
            """INSERT INTO sources(id,kind,event_at,event_basis,ingested_at,content_hash,body,status,lane,pair_ready)
               VALUES(?,?,?,?,?,?,?,'蒸馏中','p1',1)""",
            (sid, "user", when, basis, "2026-11-08T00:00:00+00:00", sid, text),
        )
        engine.db().commit()
        source = engine.db().execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        engine._write_field(
            source,
            {"field": "当前关注", "value": text.rstrip("。"), "quote": text, "crystal": "记忆主人", "trust": "written"},
            "user",
            text,
        )

    write("later", "改做南港书院讲义了。", "2026-11-01T00:00:00+00:00", "utterance")
    write("earlier", "那时候在改旧灯塔。", "2024-05-01T08:00:00+00:00", "utterance")
    write("copied", "上传时才看见的旧会话。", "2026-11-09T00:00:00+00:00", "file")
    current = engine.db().execute(
        "SELECT text FROM statements WHERE field='当前关注' AND invalid_at IS NULL"
    ).fetchall()
    assert [row["text"] for row in current] == ["改做南港书院讲义了"]


def test_source_page_explains_the_two_clocks():
    from rogmem.ui import source_detail

    html = source_detail(
        {
            "id": "src",
            "kind": "user",
            "event_at": "2024-05-01T08:00:00+00:00",
            "ingested_at": "2026-11-08T00:00:00+00:00",
            "event_basis": "file",
            "status": "已入晶",
            "body": "合成句。",
        },
        [],
        None,
        "session",
    )
    assert "事件时间" in html and "提交时间" in html
    assert "文件最后写入" in html
    assert "不是提交时间" in html


def _quiet_model(monkeypatch, engine, reply):
    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m") if role == "distill" else None)

    def fake(_base, _key, payload, **_kwargs):
        user = payload["messages"][1]["content"]
        if "已有这一页" in user:
            return reply(user), "stop"
        return "一段速写。", "stop"

    monkeypatch.setattr(engine, "_complete_chat", fake)


def test_an_older_utterance_may_still_revise_the_stored_page(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = "他现在关注南港书院。[^1]\n\n[^1]: 实 「他现在关注南港书院。」"
    engine.db().execute("UPDATE crystallizations SET body_md=? WHERE id='owner'", (old,))
    engine.db().execute(
        "INSERT INTO revisions(crystal_id,created_at,note,event_at) VALUES('owner',?,?,?)",
        ("2026-11-01T00:00:00+00:00", "正文", "2026-11-01T00:00:00+00:00"),
    )
    spoken = "更早他在改旧灯塔。"
    engine.db().execute(
        """INSERT INTO sources(id,kind,event_at,ingested_at,event_basis,content_hash,body,status,lane,pair_ready)
           VALUES('src-old','user','2024-05-01T08:00:00+00:00','2026-11-08T00:00:00+00:00','utterance','h',?,'蒸馏中','p1',1)""",
        (spoken,),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-old'").fetchone()
    rewritten = (
        "他现在关注南港书院。[^1]\n\n"
        "他现在改做旧灯塔。[^2]\n\n"
        "[^1]: 实 「他现在关注南港书院。」\n"
        "[^2]: 实 「更早他在改旧灯塔。」\n"
    )

    def reply(_user):
        return json.dumps(
            {
                "same": "是",
                "page": rewritten,
                "conflicts": [{"matter": "当前关注", "verdict": "更新", "basis": "沿用更早的说法"}],
                "gist": "改了当前",
            },
            ensure_ascii=False,
        )

    _quiet_model(monkeypatch, engine, reply)
    engine._land_markdown_pages(source, [{"name": "记忆主人", "type": "人物", "body": "更早他在改旧灯塔。[^2]\n\n[^2]: 实 「更早他在改旧灯塔。」"}])
    body = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='owner'").fetchone()["body_md"]
    prose = "\n".join(
        line for line in body.splitlines() if not (line.strip().startswith("[^") and "]:" in line)
    )
    assert "他现在改做旧灯塔" in prose
    for line in body.splitlines():
        if line.strip().startswith("[^") and "他现在关注南港书院" in line:
            assert "/admin/sources/" not in line
    assert any("/admin/sources/src-old" in line and "更早他在改旧灯塔" in line for line in body.splitlines())


def test_a_later_utterance_can_revise_the_stored_page(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute("UPDATE crystallizations SET body_md=? WHERE id='owner'", ("他现在关注南港书院。",))
    engine.db().execute(
        "INSERT INTO revisions(crystal_id,created_at,note,event_at) VALUES('owner',?,?,?)",
        ("2024-05-01T08:00:00+00:00", "正文", "2024-05-01T08:00:00+00:00"),
    )
    engine.db().execute(
        """INSERT INTO sources(id,kind,event_at,ingested_at,event_basis,content_hash,body,status,lane,pair_ready)
           VALUES('src-new','user','2026-11-01T00:00:00+00:00','2026-11-08T00:00:00+00:00','utterance','h',
                  '他现在改做北窗灯塔。','蒸馏中','p1',1)"""
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-new'").fetchone()

    def reply(_user):
        return json.dumps(
            {
                "same": "是",
                "page": "他现在改做北窗灯塔。\n\n他现在关注南港书院。",
                "conflicts": [{"matter": "当前关注", "verdict": "更新", "basis": "他现在关注南港书院。更晚的说法"}],
                "gist": "改了当前",
            },
            ensure_ascii=False,
        )

    _quiet_model(monkeypatch, engine, reply)
    engine._land_markdown_pages(source, [{"name": "记忆主人", "type": "人物", "body": "他现在改做北窗灯塔。"}])
    body = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='owner'").fetchone()["body_md"]
    assert "他现在改做北窗灯塔" in body
    assert "他现在关注南港书院" in body


def test_page_clock_uses_speech_and_survives_mixed_zones(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,body_md)
           VALUES('org','组织','南港书院','active','{}','{}','旧正文')"""
    )
    engine.db().execute(
        """INSERT INTO sources(id,kind,event_at,ingested_at,event_basis,content_hash,body,status,lane,pair_ready)
           VALUES('src-said','user','2024-05-01T08:00:00+00:00','2026-11-09T00:00:00+00:00','utterance','a','早年','蒸馏中','p1',1),
                 ('src-file','file','2026-11-09T01:00:00+00:00','2026-11-09T02:00:00+00:00','file','b','文件','蒸馏中','slow',1)"""
    )
    engine.db().execute(
        """INSERT INTO statements(id,crystal_id,source_id,field,text,quote,trust,rank,event_at,created_at)
           VALUES('st1','org','src-said','当前关注','早年的说法','早年的说法','written','user','2024-05-01T08:00:00+00:00','2026-11-09T00:00:00+00:00')"""
    )
    engine.db().execute(
        "INSERT INTO revisions(crystal_id,source_id,created_at,note,event_at) VALUES('org','src-file',?,?,?)",
        ("2026-11-09T01:00:00+00:00", "正文", "2026-11-09T01:00:00+00:00"),
    )
    engine.db().execute(
        "INSERT INTO revisions(crystal_id,created_at,note,event_at) VALUES('org',?,?,?)",
        ("2026-11-09T03:00:00+00:00", "正文", "2026-11-02T00:00:00"),
    )
    engine.db().commit()
    assert engine._latest_event_at("org") == "2026-11-02T00:00:00"


def test_a_separate_engineering_page_joins_the_one_repo_page(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "llm_target", lambda role: None)
    repo = "https://github.com/example/north-lamp"
    old = f"仓库地址\n{repo}\n旧约束：保留卷。"
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,body_md)
           VALUES('eng','工程','北窗工程','active','{}','{}',?)""",
        (old,),
    )
    engine.db().execute(
        """INSERT INTO sources(id,kind,event_at,ingested_at,event_basis,content_hash,body,status,lane,pair_ready)
           VALUES('src-eng','user','2026-11-09T03:00:00+00:00','2026-11-09T04:00:00+00:00','utterance','h',
                  '新约束：端口保持不动。','蒸馏中','p1',1)"""
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-eng'").fetchone()
    engine._land_markdown_pages(
        source,
        [{"name": "北窗工程", "type": "工程", "body": f"新约束：端口保持不动。\n{repo}\n", "separate": True}],
        start="落地",
    )
    rows = engine.db().execute("SELECT id, body_md FROM crystallizations WHERE type='工程'").fetchall()
    assert [row["id"] for row in rows] == ["eng"]
    assert "旧约束：保留卷" in rows[0]["body_md"]
    assert "新约束：端口保持不动" in rows[0]["body_md"]


def _dock(engine):
    repo = "https://example.test/north-dock"
    old = f"青渚船坞还停在北栈。\n{repo}\n"
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,body_md)
           VALUES('dock','工程','青渚船坞','active','{}','{}',?)""",
        (old,),
    )
    engine.db().execute(
        """INSERT INTO sources(id,kind,event_at,ingested_at,event_basis,content_hash,body,status,lane,pair_ready)
           VALUES('src-dock','user','2026-11-09T03:00:00+00:00','2026-11-09T04:00:00+00:00','utterance','h',
                  '另一处链接不是这艘船的仓库。','蒸馏中','p1',1)"""
    )
    engine.db().commit()
    return engine.db().execute("SELECT * FROM sources WHERE id='src-dock'").fetchone()


def test_a_different_link_lands_on_the_same_engineering_name(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    source = _dock(engine)
    other = "https://example.test/other-dock"
    engine._land_markdown_pages(
        source,
        [{"name": "青渚船坞", "type": "工程", "body": f"另一处链接不是这艘船的仓库。\n{other}\n"}],
        start="落地",
    )
    rows = engine.db().execute(
        "SELECT id FROM crystallizations WHERE name='青渚船坞' AND status='active'"
    ).fetchall()
    assert [row["id"] for row in rows] == ["dock"]


def test_a_separate_engineering_page_does_not_fork_the_same_name(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    source = _dock(engine)
    other = "https://example.test/other-dock"
    engine._land_markdown_pages(
        source,
        [{"name": "青渚船坞", "type": "工程", "body": f"另一处链接不是这艘船的仓库。\n{other}\n", "separate": True}],
        start="落地",
    )
    rows = engine.db().execute("SELECT id, status FROM crystallizations WHERE name='青渚船坞'").fetchall()
    assert [row["id"] for row in rows] == ["dock"]
    assert rows[0]["status"] == "active"


def test_a_separate_page_does_not_fork_an_existing_name(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = "南港书院还在原来的地方。"
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,body_md)
           VALUES('org','组织','南港书院','active','{}','{}',?)""",
        (old,),
    )
    engine.db().execute(
        """INSERT INTO sources(id,kind,event_at,ingested_at,event_basis,content_hash,body,status,lane,pair_ready)
           VALUES('src-dup','user','2026-11-09T03:00:00+00:00','2026-11-09T04:00:00+00:00','utterance','h',
                  '另一套说法。','蒸馏中','p1',1)"""
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-dup'").fetchone()

    def reply(_user):
        return json.dumps(
            {"same": "否", "name": "", "type": "组织", "page": "另一套说法。", "conflicts": [], "gist": "没给出另一个篇名"},
            ensure_ascii=False,
        )

    _quiet_model(monkeypatch, engine, reply)
    try:
        engine._land_markdown_pages(source, [{"name": "南港书院", "type": "组织", "body": "另一套说法。"}])
    except RuntimeError as exc:
        assert "已有同名的一页" in str(exc)
    else:
        raise AssertionError("同名的另一页不该被当成已经落地")
    rows = engine.db().execute("SELECT id, body_md FROM crystallizations WHERE name='南港书院'").fetchall()
    assert [row["id"] for row in rows] == ["org"]
    assert rows[0]["body_md"].strip() == old


def _owner_with_citation(engine, body: str) -> None:
    engine.db().execute("UPDATE crystallizations SET body_md=? WHERE id='owner'", (body,))
    engine.db().execute(
        """INSERT INTO sources(id,kind,event_at,ingested_at,event_basis,content_hash,body,status,lane,pair_ready)
           VALUES('src-cite','user','2026-11-01T00:00:00+00:00','2026-11-08T00:00:00+00:00','utterance','h',
                  '后来这句不成立。','蒸馏中','p1',1)"""
    )
    engine.db().commit()


def test_a_citation_can_be_revoked_when_the_revision_says_why(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = "他现在关注南港书院。[^1]\n\n[^1]: 实 「他现在关注南港书院。」"
    _owner_with_citation(engine, old)
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-cite'").fetchone()
    revised = "他后来不再关注南港书院。"
    reason = "「他现在关注南港书院。」已经不成立，撤掉这句引文。"

    def reply(_user):
        return json.dumps(
            {"same": "是", "page": revised, "conflicts": [], "gist": reason},
            ensure_ascii=False,
        )

    _quiet_model(monkeypatch, engine, reply)
    engine._land_markdown_pages(source, [{"name": "记忆主人", "type": "人物", "body": "后来这句不成立。"}])
    body = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='owner'").fetchone()["body_md"]
    gist = engine.db().execute(
        "SELECT gist FROM revisions WHERE crystal_id='owner' AND note='正文' ORDER BY id DESC LIMIT 1"
    ).fetchone()["gist"]
    assert revised in body
    assert "[^1]" not in body
    assert reason in gist


def test_a_crystal_link_can_be_removed_when_the_revision_says_why(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = "他现在在[[南港书院]]任教。[^1]\n\n[^1]: 实 「他现在在南港书院任教。」"
    _owner_with_citation(engine, old)
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-cite'").fetchone()
    revised = "他后来离开了那家书院。"
    reason = "「他现在在南港书院任教。」已经结束，撤掉这处链接。"

    def reply(_user):
        return json.dumps(
            {"same": "是", "page": revised, "conflicts": [], "gist": reason},
            ensure_ascii=False,
        )

    _quiet_model(monkeypatch, engine, reply)
    engine._land_markdown_pages(source, [{"name": "记忆主人", "type": "人物", "body": "后来这句不成立。"}])
    body = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='owner'").fetchone()["body_md"]
    gist = engine.db().execute(
        "SELECT gist FROM revisions WHERE crystal_id='owner' AND note='正文' ORDER BY id DESC LIMIT 1"
    ).fetchone()["gist"]
    assert revised in body
    assert "[[南港书院]]" not in body
    assert reason in gist


def test_a_crystal_link_stays_when_the_revision_gives_no_reason(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = "他现在在[[南港书院]]任教。[^1]\n\n[^1]: 实 「他现在在南港书院任教。」"
    _owner_with_citation(engine, old)
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-cite'").fetchone()

    def reply(_user):
        return json.dumps(
            {"same": "是", "page": "他后来离开了那家书院。", "conflicts": [], "gist": ""},
            ensure_ascii=False,
        )

    _quiet_model(monkeypatch, engine, reply)
    engine._land_markdown_pages(source, [{"name": "记忆主人", "type": "人物", "body": "后来这句不成立。"}])
    body = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='owner'").fetchone()["body_md"]
    assert "他现在在[[南港书院]]任教。" in body
    assert "[[南港书院]]" in body
    assert "他后来离开了那家书院。" not in body
    assert not any(line.strip() == "[[南港书院]]" for line in body.splitlines())


def test_an_update_keeps_the_named_sentence_beside_the_new_one(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = "他现在在[[南港书院]]任教。"
    _owner_with_citation(engine, old)
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-cite'").fetchone()
    revised = "他后来离开了那家书院。"
    reason = "他现在在[[南港书院]]任教。这处旧链接指的事已经结束。"

    def reply(_user):
        return json.dumps(
            {
                "same": "是",
                "page": revised,
                "conflicts": [{"matter": "南港书院", "verdict": "更新", "basis": reason}],
                "gist": "",
            },
            ensure_ascii=False,
        )

    _quiet_model(monkeypatch, engine, reply)
    engine._land_markdown_pages(source, [{"name": "记忆主人", "type": "人物", "body": "后来这句不成立。"}])
    body = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='owner'").fetchone()["body_md"]
    assert revised in body
    assert "他现在在[[南港书院]]任教。" in body
    assert not any(line.strip() == "[[南港书院]]" for line in body.splitlines())


def test_a_summary_does_not_withdraw_unnamed_links(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = "他在[[南港书院]]任教，也用[[北窗灯塔]]。"
    _owner_with_citation(engine, old)
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-cite'").fetchone()
    revised = "他后来离开了这两处。"

    def reply(_user):
        return json.dumps(
            {"same": "是", "page": revised, "conflicts": [], "gist": "撤掉[[南港书院]]，说法已经结束。"},
            ensure_ascii=False,
        )

    _quiet_model(monkeypatch, engine, reply)
    engine._land_markdown_pages(source, [{"name": "记忆主人", "type": "人物", "body": "后来这句不成立。"}])
    body = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='owner'").fetchone()["body_md"]
    assert "他在任教，也用[[北窗灯塔]]。" in body
    assert "[[南港书院]]" not in body
    assert "[[北窗灯塔]]" in body
    assert revised not in body
    assert not any(line.strip() in {"[[南港书院]]", "[[北窗灯塔]]"} for line in body.splitlines())


def test_naming_a_longer_link_does_not_withdraw_the_shorter_one(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = "他在[[南港]]长大，后来在[[南港书院]]任教。"
    _owner_with_citation(engine, old)
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-cite'").fetchone()
    revised = "他在[[南港]]长大，后来离开了书院。"

    def reply(_user):
        return json.dumps(
            {
                "same": "是",
                "page": revised,
                "conflicts": [],
                "gist": "他在[[南港]]长大，后来在[[南港书院]]任教。书院这段已经结束。",
            },
            ensure_ascii=False,
        )

    _quiet_model(monkeypatch, engine, reply)
    engine._land_markdown_pages(source, [{"name": "记忆主人", "type": "人物", "body": "后来这句不成立。"}])
    body = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='owner'").fetchone()["body_md"]
    assert "[[南港书院]]" not in body
    assert "[[南港]]" in body


def test_a_conflict_subject_alone_does_not_withdraw_a_link(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = "他现在在[[南港书院]]任教。"
    _owner_with_citation(engine, old)
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-cite'").fetchone()

    def reply(_user):
        return json.dumps(
            {
                "same": "是",
                "page": "他后来仍在那里任教。",
                "conflicts": [{"matter": "南港书院", "verdict": "并存", "basis": "两边可以同时成立。"}],
                "gist": "",
            },
            ensure_ascii=False,
        )

    _quiet_model(monkeypatch, engine, reply)
    engine._land_markdown_pages(source, [{"name": "记忆主人", "type": "人物", "body": "后来这句不成立。"}])
    body = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='owner'").fetchone()["body_md"]
    assert "[[南港书院]]" in body


def test_restoring_a_citation_does_not_replace_the_mark_the_revision_kept(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = "甲地的事。[^1]\n\n[^1]: 实 「甲地的事。」"
    _owner_with_citation(engine, old)
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-cite'").fetchone()
    revised = "甲地的事。[^1]\n\n乙地的事。[^2]\n\n[^1]: 实 「甲地的事。」\n[^2]: 实 「乙地的事。」"

    def reply(_user):
        return json.dumps(
            {"same": "是", "page": revised, "conflicts": [], "gist": ""},
            ensure_ascii=False,
        )

    _quiet_model(monkeypatch, engine, reply)
    engine._land_markdown_pages(source, [{"name": "记忆主人", "type": "人物", "body": "后来这句不成立。"}])
    body = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='owner'").fetchone()["body_md"]
    from rogmem.pages_md import citation_passage, cite_notes

    kept = {citation_passage(note) for note in cite_notes(body).values()}
    assert "甲地的事。" in kept
    assert "乙地的事。" in kept


def test_a_citation_whose_words_remain_keeps_its_note(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = "他现在关注南港书院。[^1]\n\n[^1]: 实 「他现在关注南港书院。」"
    _owner_with_citation(engine, old)
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-cite'").fetchone()
    revised = "他现在关注南港书院。[^1]\n\n近来如此。\n\n[^1]: 实 「他现在关注南港书院。」"

    def reply(_user):
        return json.dumps(
            {"same": "是", "page": revised, "conflicts": [], "gist": "补了近况。"},
            ensure_ascii=False,
        )

    _quiet_model(monkeypatch, engine, reply)
    engine._land_markdown_pages(source, [{"name": "记忆主人", "type": "人物", "body": "后来这句不成立。"}])
    body = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='owner'").fetchone()["body_md"]
    from rogmem.pages_md import citation_passage, cite_notes

    assert "他现在关注南港书院。" in {citation_passage(note) for note in cite_notes(body).values()}
    assert "[^1]" in body.split("近来如此。", 1)[0]


def test_a_citation_stays_when_the_revision_gives_no_reason(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    old = "他现在关注南港书院。[^1]\n\n[^1]: 实 「他现在关注南港书院。」"
    _owner_with_citation(engine, old)
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-cite'").fetchone()

    def reply(_user):
        return json.dumps(
            {"same": "是", "page": "他后来不再关注南港书院。", "conflicts": [], "gist": ""},
            ensure_ascii=False,
        )

    _quiet_model(monkeypatch, engine, reply)
    engine._land_markdown_pages(source, [{"name": "记忆主人", "type": "人物", "body": "后来这句不成立。"}])
    body = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='owner'").fetchone()["body_md"]
    assert "[^1]" in body
    assert "他现在关注南港书院" in body


def test_a_scanned_file_records_its_write_time(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    path = tmp_path / "scan" / "resume.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("2025年12月起负责一项新工作。", encoding="utf-8")
    result = engine.ingest_file(path)
    row = engine.db().execute(
        "SELECT event_at, event_basis FROM sources WHERE id=?",
        (result["id"],),
    ).fetchone()
    assert row["event_basis"] == "file"
    assert row["event_at"]


def test_an_ingest_stamp_is_not_shown_as_speech(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    stamped = "2026-11-08T12:00:00+00:00"
    engine.db().execute(
        """INSERT INTO sources(id,kind,event_at,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES('src-legacy','user',?,?, 'h','旧材料','蒸馏中','p1',1)""",
        (stamped, stamped),
    )
    engine.db().commit()
    row = engine.db().execute("SELECT * FROM sources WHERE id='src-legacy'").fetchone()
    shown = engine._prompt_injection(row)
    assert shown["event_at"] == ""
    assert shown["event_basis"] == ""
    spoken = engine._prompt_injection(
        {
            "kind": "user",
            "event_at": "2024-05-01T08:00:00+00:00",
            "ingested_at": stamped,
            "event_basis": "utterance",
        }
    )
    assert spoken["event_at"] == "2024-05-01T08:00:00+00:00"
    assert spoken["event_basis"] == "utterance"
