"""Host list shows every device, marks only the viewing computer, and can authorize."""

from __future__ import annotations

import ast
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _apply(engine, device_id: str, name: str, program: str) -> tuple[str, str]:
    from rogmem import crypto

    public = crypto.b64encode(crypto.new_keypair()[1])
    applied = engine.enroll_apply(device_id, name, program, public)
    assert applied["ok"]
    return applied["id"], public


def _claim(engine, device_id: str, name: str, program: str) -> tuple[str, str]:
    agent_id, public = _apply(engine, device_id, name, program)
    assert engine.enroll_approve(agent_id)["ok"]
    claimed = engine.enroll_claim(public)
    assert claimed["ok"]
    return agent_id, public


def _source(engine, sid: str, kind: str, agent_id: str | None, device_name: str, agent_name: str) -> None:
    engine.db().execute(
        """INSERT INTO sources(id,kind,agent_id,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?,?,?,?,?,?,?,?,?,?,1)""",
        (sid, kind, agent_id, device_name, agent_name, engine.utcnow(), sid, "正文", "已蒸馏", "fast"),
    )
    engine.db().commit()


def test_fleet_lists_every_device_and_marks_only_the_viewer(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    aaa, _public_a = _claim(engine, "dev-a", "AAAA", "Cursor")
    bbb, _public_b = _claim(engine, "dev-b", "BBB", "Hermes")
    pending, _public_c = _apply(engine, "dev-c", "CCCC", "Codex")
    stopped, _public_d = _claim(engine, "dev-d", "DDDD", "WorkBuddy")
    engine.disable_agent(stopped)
    rejected, _public_r = _apply(engine, "dev-r", "拒绝机", "Cursor")
    engine.enroll_approve(rejected)
    engine.db().execute("UPDATE agents SET status='已拒绝' WHERE id=?", (rejected,))
    engine.db().execute(
        """INSERT INTO agents(id,device_id,device_name,program,display_name,status,pubkey,key_hash,created_at)
           VALUES('old','dev-old','旧电脑','Codex','旧电脑 · Codex','启用','cHVi','abc','2020-01-01T00:00:00+00:00')"""
    )
    _source(engine, "u1", "user", aaa, "AAAA", "AAAA · Cursor")
    _source(engine, "a1", "agent", aaa, "AAAA", "AAAA · Cursor")
    _source(engine, "f1", "file", aaa, "AAAA", "AAAA · Cursor")
    _source(engine, "s1", "summary", aaa, "AAAA", "AAAA · Cursor")
    _source(engine, "b1", "user", bbb, "BBB", "BBB · Hermes")
    _source(engine, "h1", "user", None, "AAAA", "Cursor")
    _source(engine, "h2", "agent", None, "AAAA", "Cursor")
    engine.db().commit()
    engine.note_memory_call(aaa)
    engine.note_memory_call(aaa)
    engine.note_memory_call(stopped)
    _source(engine, "live", "user", aaa, "AAAA", "AAAA · Cursor")

    calls = engine.db().execute("SELECT call_count FROM agents WHERE id=?", (aaa,)).fetchone()["call_count"]
    assert calls == 2
    assert engine.db().execute("SELECT call_count FROM agents WHERE id=?", (stopped,)).fetchone()["call_count"] == 0
    memory_reads, writes = _memory_tools()
    assert memory_reads == {"core", "search", "read_section", "read_chain", "read_source", "read_revisions"}
    assert "submit_turn" in writes and "guide" in writes and "submit_draft" in writes
    names = {
        item["device_name"]
        for item in engine.db().execute("SELECT device_name FROM agents WHERE device_id IN ('dev-a','dev-b')")
    }
    assert names == {"AAAA", "BBB"}
    assert "本机" not in names

    viewed = engine.list_connected_hosts(viewer_device_id="dev-a")
    by_id = {item["device_id"]: item for item in viewed["rows"]}
    assert set(by_id) == {"dev-a", "dev-b", "dev-c", "dev-d", "dev-old"}
    assert by_id["dev-a"]["device_label"] == "AAAA（本机）"
    assert by_id["dev-a"]["local"] is True
    assert by_id["dev-b"]["device_label"] == "BBB"
    assert by_id["dev-b"]["local"] is False
    assert by_id["dev-a"]["session_count"] == 5
    assert by_id["dev-b"]["session_count"] == 1
    assert by_id["dev-a"]["call_count"] == 2
    assert by_id["dev-a"]["connected_estimated"] is False
    assert by_id["dev-a"]["connected_at"]
    assert by_id["dev-c"]["status"] == "待放行"
    assert by_id["dev-c"]["connected_at"] == ""
    assert by_id["dev-d"]["status"] == "停用"
    assert by_id["dev-old"]["connected_estimated"] is True
    assert by_id["dev-old"]["connected_at"] == "2020-01-01T00:00:00+00:00"
    assert viewed["viewer_known"] is True

    other = engine.list_connected_hosts(viewer_device_id="dev-b")
    labels = {item["device_id"]: item["device_label"] for item in other["rows"]}
    assert labels["dev-b"] == "BBB（本机）"
    assert labels["dev-a"] == "AAAA"
    plain = engine.list_connected_hosts()
    assert all(item["local"] is False for item in plain["rows"])
    assert plain["viewer_known"] is False
    named = engine.list_connected_hosts(viewer_device_name="CCCC")
    assert named["viewer_known"] is True
    assert next(item["device_label"] for item in named["rows"] if item["device_id"] == "dev-c") == "CCCC（本机）"
    assert next(item["local"] for item in named["rows"] if item["device_id"] == "dev-a") is False
    stale = engine.list_connected_hosts(viewer_device_id="not-a-device", viewer_device_name="CCCC")
    assert stale["viewer_known"] is True
    assert next(item["local"] for item in stale["rows"] if item["device_id"] == "dev-c") is True
    assert next(item["local"] for item in stale["rows"] if item["device_id"] == "dev-a") is False
    missing = engine.list_connected_hosts(viewer_device_id="not-a-device")
    assert missing["viewer_known"] is False
    assert all(item["local"] is False for item in missing["rows"])

    same = engine.list_connected_hosts(viewer_device_name="同名")
    assert same["viewer_known"] is False
    engine.db().execute("UPDATE agents SET device_name='同名' WHERE device_id IN ('dev-a','dev-b')")
    engine.db().commit()
    ambiguous = engine.list_connected_hosts(viewer_device_name="同名")
    assert ambiguous["viewer_known"] is False
    assert all(item["local"] is False for item in ambiguous["rows"])
    engine.db().execute("UPDATE agents SET device_name='AAAA' WHERE id=?", (aaa,))
    engine.db().execute("UPDATE agents SET device_name='BBB' WHERE id=?", (bbb,))
    engine.db().commit()

    only_b = engine.list_connected_hosts(viewer_device_id="dev-a", device="dev-b")
    assert [item["device_id"] for item in only_b["rows"]] == ["dev-b"]
    assert any(item["id"] == "dev-a" for item in only_b["facets"]["devices"])

    first_connected = engine.db().execute("SELECT connected_at FROM agents WHERE id=?", (aaa,)).fetchone()["connected_at"]
    engine.enroll_reclaim(_public_a, "dev-a", "Cursor")
    engine.enroll_approve(aaa)
    assert engine.enroll_claim(_public_a)["ok"]
    second = engine.db().execute("SELECT connected_at FROM agents WHERE id=?", (aaa,)).fetchone()["connected_at"]
    assert second == first_connected
    assert engine.enable_agent(pending)["ok"] is False

    from rogmem.ui import hosts_page

    page = hosts_page(engine.list_hosts(), engine.list_connected_hosts(viewer_device_id="dev-a"), {})
    assert "添加宿主" in page
    assert "复制提示" in page
    assert "___ROGMEM_GRANT___" in page
    assert "fillGrant" in page
    assert page.index("fillGrant") < page.index("/admin/api/enroll-grants")
    assert "AAAA（本机）" in page
    assert "BBB（本机）" not in page
    assert "data-device-id='dev-b'" in page
    fleet = page.split('id="host-fleet"', 1)[1].split('id="add-host-modal"', 1)[0]
    modal = page.split('id="add-host-modal"', 1)[1].split("<script>", 1)[0]
    assert "通过授权" in fleet
    assert "停止授权" in fleet
    assert "/admin/api/agents/" not in modal
    assert "停止授权" not in modal
    assert "待导入会话数" in fleet
    assert "setInterval(refreshPending, 20000)" in page
    assert "/admin/api/hosts/progress" in page
    assert "贴给本机任意一个已安装的宿主" in modal
    assert "现在可以适配这些宿主：Cursor、Trae、Hermes、Codex、WorkBuddy、CodeBuddy、DSH。" in modal
    assert "累计调用次数" in page
    assert "核心记忆" in page
    assert "<h2>接入确认</h2>" not in page
    assert "<p class='note'>这里列出各台电脑" not in page
    head = page.split('id="host-fleet"', 1)[1].split("</tr>", 1)[0]
    assert "设备名" not in head
    assert "name='device'" in page.split('id="host-fleet"', 1)[0]
    assert "class='device-name'" not in page
    local_row = page.split("data-device-id='dev-a'", 1)[1].split("</tr>", 1)[0]
    assert "class='agent-name'" in local_row and "class='local-mark'>（本机）</span>" in local_row
    assert "data-device-name='AAAA'" in local_row
    assert "targetAddressSpace" in page and "loopback-network" in page and ".agent-name" in page
    assert head.index("智能体名") < head.index("接入状态") < head.index("接入时间") < head.index("最近活跃")
    assert "连接状态" not in head
    bbb_row = page.split("data-device-id='dev-b'", 1)[1].split("</tr>", 1)[0]
    assert "（本机）" not in bbb_row
    assert f"/admin/api/agents/{pending}/approve" in page
    assert f"/admin/api/agents/{stopped}/enable" in page
    assert "拒绝机" not in page
    assert engine.enable_agent(stopped)["ok"]
    assert engine.db().execute("SELECT status FROM agents WHERE id=?", (stopped,)).fetchone()["status"] == "启用"


def test_a_core_read_is_counted_through_the_tool(tmp_path, monkeypatch):
    """core 走 run_in_executor，那条线程没有请求上下文；计数必须仍在工具这一层发出，
    否则宿主列表里每次开场都要读的核心记忆一次都不会被记上。"""
    import asyncio

    engine = _open(tmp_path, monkeypatch)
    import rogmem.app as app

    agent_id, _public = _claim(engine, "dev-trae", "TraeMac", "Trae")
    assert engine.db().execute("SELECT call_count FROM agents WHERE id=?", (agent_id,)).fetchone()["call_count"] == 0

    async def drive():
        token = app.CURRENT.set({"id": agent_id})
        try:
            return await app.core(budget=0)
        finally:
            app.CURRENT.reset(token)

    assert json.loads(asyncio.run(drive()))
    assert engine.db().execute("SELECT call_count FROM agents WHERE id=?", (agent_id,)).fetchone()["call_count"] == 1


def test_access_status_joins_the_host_list():
    from rogmem.ui import hosts_page

    def host(device, program, status, local=False):
        return {
            "id": f"{device}-{program}",
            "device_id": f"{device}-{program}",
            "device_base": device,
            "local": local,
            "agent_name": program,
            "program": program,
            "status": status,
            "code": "AB12" if status == "待放行" else "",
            "connected_at": "",
            "connected_estimated": False,
            "last_used": "",
            "session_count": 0,
            "call_count": 0,
        }

    fleet = {
        "rows": [
            host("dev-a", "Cursor", "启用", local=True),
            host("dev-b", "Cursor", "启用"),
            host("dev-a", "Hermes", "待放行", local=True),
            host("dev-b", "Codex", "启用"),
            host("dev-c", "Codex", "停用"),
        ],
        "facets": {"devices": [], "agents": []},
        "viewer_known": True,
    }
    reports = [
        {"program": "Cursor", "access": "已接入", "health": "正常", "detail": "最近一次调用成功。", "reported_at": "t", "importable": 4, "imported": 1},
        {"program": "Hermes", "access": "待授权", "health": "通道未开", "detail": "核对码：AB12", "reported_at": "t"},
        {
            "program": "WorkBuddy",
            "access": "已发现未接入",
            "health": "通道未开",
            "detail": "可导入 1 条，跳过 0 条。原文会送到记忆中枢。确认之前不会导入。",
            "reported_at": "t",
        },
        {"program": "Codex", "access": "未安装", "health": "通道未开", "detail": "", "reported_at": "t"},
    ]
    page = hosts_page(reports, fleet, {})
    assert "<h2>接入确认</h2>" not in page
    assert "<p class='note'>" not in page.split('id="add-host-modal"', 1)[0]
    local = page.split("data-device-id='dev-a-Cursor'", 1)[1].split("</tr>", 1)[0]
    remote = page.split("data-device-id='dev-b-Cursor'", 1)[1].split("</tr>", 1)[0]
    assert "已接入" in local and "最近一次调用成功" in local and "class='scan-pending' title='本机扫到但还没入库的条数'>3<" in local
    assert "连接状态" not in page
    assert "已接入" not in remote and "最近一次调用成功" not in remote and "class='scan-pending' title='本机扫到但还没入库的条数'>3<" not in remote
    hermes = page.split("data-device-id='dev-a-Hermes'", 1)[1].split("</tr>", 1)[0]
    assert "待授权" in hermes and "申请授权" in page and "没领到密钥不算连上" in page
    assert "确认接入" not in hermes
    assert page.count("确认接入") == 1
    # 评审 PR #116 §1：legacy 「确认导入」按钮已移除；改由每行的「导入历史」单独开始。
    assert "确认导入" not in page
    assert "class='quiet host-import-job'" in local
    assert ">导入历史</button>" in local
    assert "host-actions" in local
    assert "下达历史导入作业" not in page
    assert "作业 pending" not in page
    assert "可导入 1 条" in page and "原文会送到记忆中枢" in page
    assert "未安装" in page
    filtered = hosts_page(reports, fleet, {"device": "dev-b", "agent": ""})
    body = filtered.split('id="host-fleet"', 1)[1].split("</tr>", 1)[1].split('id="add-host-modal"', 1)[0]
    assert "已发现未接入" not in body
    assert "未安装" not in body


def test_history_progress_is_local_stock_still_waiting(tmp_path, monkeypatch):
    from rogmem.ui import hosts_page

    engine = _open(tmp_path, monkeypatch)
    engine.report_hosts(
        [{"program": "Hermes", "found": True, "access": "已接入", "health": "正常", "detail": "", "importable": 4}]
    )
    engine.ingest_history(
        [
            {"role": "user", "text": "甲", "event_at": "2026-01-01T00:00:00+00:00", "session_id": "s", "seq": 1},
            {"role": "assistant", "text": "乙", "event_at": "2026-01-01T00:00:01+00:00", "session_id": "s", "seq": 2},
        ],
        "Hermes",
        device_name="AAAA",
    )
    engine.ingest_history(
        [{"role": "user", "text": "甲", "event_at": "2026-01-01T00:00:00+00:00", "session_id": "s", "seq": 1}],
        "Hermes",
        device_name="AAAA",
    )
    listed = {row["program"]: row for row in engine.list_hosts()}
    assert listed["Hermes"]["importable"] == 4
    assert listed["Hermes"]["imported"] == 2
    assert listed["Cursor"]["importable"] is None
    page = hosts_page(
        engine.list_hosts(),
        {"rows": [], "facets": {"devices": [], "agents": []}, "viewer_known": False},
        {},
    )
    assert "class='scan-pending' title='本机扫到但还没入库的条数'>2<" in page
    assert "2/4" not in page
    assert "0/0" not in page.split("Hermes", 1)[0]


def test_project_ref_matches_one_repository_across_spellings(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    assert engine._project_ref_hits(
        "git@git.example.test:org/demo.git",
        "https://git.example.test/org/demo",
    )
    assert engine._project_ref_hits(
        "https://user:secret@git.example.test/org/demo.git",
        "仓库地址 https://git.example.test/org/demo 。",
    )
    assert not engine._project_ref_hits(
        "https://git.example.test/org/demo",
        "https://git.example.test/org/demo-extra",
    )


def test_a_renamed_owner_updates_the_same_repository(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.ingest_history(
        [
            {
                "role": "user",
                "text": "合成句：北岸仓库还在。",
                "event_at": "2026-04-02T00:00:00+00:00",
                "session_id": "s-repo",
                "seq": 1,
                "project": "https://github.com/old-name/demo",
            }
        ],
        "Hermes",
    )
    moved = engine.bind_session_projects(
        [{"session_id": "s-repo", "project": "https://github.com/new-name/demo"}]
    )
    assert moved["updated"] == 1
    kept = engine.db().execute("SELECT project_ref FROM sources").fetchone()["project_ref"]
    assert kept == "https://github.com/new-name/demo"
    engine.bind_session_projects(
        [{"session_id": "s-repo", "project": "https://github.com/new-name/other"}]
    )
    still = engine.db().execute("SELECT project_ref FROM sources").fetchone()["project_ref"]
    assert still == "https://github.com/new-name/demo"


def test_embed_threads_leave_a_quarter_of_the_cores(monkeypatch):
    from rogmem import engine

    monkeypatch.setattr(engine.os, "cpu_count", lambda: 8)
    monkeypatch.delenv("ROGMEM_BUDGET", raising=False)
    assert engine.embed_threads() == 6
    monkeypatch.setenv("ROGMEM_BUDGET", "0.5")
    assert engine.embed_threads() == 4
    monkeypatch.setenv("ROGMEM_BUDGET", "nope")
    assert engine.embed_threads() == 6


def test_history_keeps_the_repository_the_host_already_recorded(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    first = engine.ingest_history(
        [
            {
                "role": "user",
                "text": "合成句：北岸仓库还在。",
                "event_at": "2026-04-01T00:00:00+00:00",
                "session_id": "s-repo",
                "seq": 1,
            }
        ],
        "Hermes",
    )
    assert first["ok"]
    assert (engine.db().execute("SELECT project_ref FROM sources").fetchone()["project_ref"] or "") == ""
    bound = engine.bind_session_projects(
        [{"session_id": "s-repo", "project": "https://git.example.test/org/demo.git"}]
    )
    assert bound["updated"] == 1
    again = engine.ingest_history(
        [
            {
                "role": "user",
                "text": "合成句：北岸仓库还在。",
                "event_at": "2026-04-01T00:00:00+00:00",
                "session_id": "s-repo",
                "seq": 1,
                "project": "https://git.example.test/other.git",
            }
        ],
        "Hermes",
    )
    assert again["duplicates"] == 1
    kept = engine.db().execute("SELECT project_ref FROM sources WHERE session_id='s-repo'").fetchone()["project_ref"]
    assert kept == "https://git.example.test/org/demo.git"
    fresh = engine.ingest_history(
        [
            {
                "role": "assistant",
                "text": "合成句：灯塔记在这个仓库。",
                "event_at": "2026-04-01T00:00:01+00:00",
                "session_id": "s-new",
                "seq": 2,
                "project": "https://git.example.test/new.git",
            }
        ],
        "Hermes",
    )
    assert fresh["imported"][0]["duplicate"] is False
    stored = engine.db().execute("SELECT project_ref FROM sources WHERE session_id='s-new'").fetchone()["project_ref"]
    assert stored == "https://git.example.test/new.git"


def test_imported_turns_stay_visible_without_a_scan(tmp_path, monkeypatch):
    from rogmem.ui import hosts_page

    engine = _open(tmp_path, monkeypatch)
    hermes, _public = _claim(engine, "dev-h", "书房", "Hermes")
    engine.ingest_history(
        [
            {"role": "user", "text": "甲", "event_at": "2026-02-01T00:00:00+00:00", "session_id": "s", "seq": 1},
            {"role": "assistant", "text": "乙", "event_at": "2026-02-01T00:00:01+00:00", "session_id": "s", "seq": 2},
        ],
        "Hermes",
        device_name="极空间",
    )
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES('old','user','极空间','Hermes',?,'old','旧的一条','已解析','p1',1)""",
        (engine.utcnow(),),
    )
    engine.db().commit()
    engine.ingest_history(
        [{"role": "user", "text": "别的程序", "event_at": "2026-02-01T00:00:02+00:00", "session_id": "c", "seq": 1}],
        "Codex",
        device_name="极空间",
    )
    viewed = {item["id"]: item for item in engine.list_connected_hosts()["rows"]}
    assert viewed[hermes]["session_count"] == 3
    assert viewed[hermes]["history_count"] == 3
    page = hosts_page(engine.list_hosts(), engine.list_connected_hosts(), {})
    row = page.split("data-device-id='dev-h'", 1)[1].split("</tr>", 1)[0]
    assert "<td>3</td>" in row
    assert "class='scan-pending' title='本机扫到但还没入库的条数'>—<" in row

    engine.report_hosts(
        [{"program": "Hermes", "found": True, "access": "已接入", "health": "正常", "detail": "", "importable": 10}]
    )
    scanned = hosts_page(engine.list_hosts(), engine.list_connected_hosts(viewer_device_id="dev-h"), {})
    owned = scanned.split("data-device-id='dev-h'", 1)[1].split("</tr>", 1)[0]
    assert "class='scan-pending' title='本机扫到但还没入库的条数'>7<" in owned


def test_one_host_is_one_agent_in_the_session_filter(tmp_path, monkeypatch):
    """A trailing tab and a draft's quoted name are the same host, not two agents."""
    engine = _open(tmp_path, monkeypatch)
    agent_id, _public = _claim(engine, "dev-lin", "LINS-MacBook-Pro-408", "Cursor\t")
    host = engine.db().execute(
        "SELECT program, display_name FROM agents WHERE id=?",
        (agent_id,),
    ).fetchone()
    assert host["program"] == "Cursor"
    assert host["display_name"] == "LINS-MacBook-Pro-408 · Cursor"
    dirty = "LINS-MacBook-Pro-408 · Cursor\t"
    engine.db().execute("UPDATE agents SET display_name=? WHERE id=?", (dirty, agent_id))
    engine.db().commit()
    _source(engine, "turn", "user", agent_id, "LINS-MacBook-Pro-408", dirty)
    _source(engine, "quoted", "draft", None, "", f"智能体「{dirty}」")
    _source(engine, "mention", "user", None, "", "上次在 LINS-MacBook-Pro-408 · Cursor 上试过")
    fresh = engine.db().execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    page = (
        "# 记忆主人\n类型：人物\n\n"
        "他现在住在南港。[^1]\n\n"
        "[^1]: 实 「我现在住在南港。」\n"
    )
    saved = engine.submit_draft(page, fresh)
    assert saved["ok"] is True
    filed = engine.db().execute(
        "SELECT agent_id, agent_name FROM sources WHERE id=?",
        (saved["id"],),
    ).fetchone()
    assert filed["agent_id"] == agent_id
    assert filed["agent_name"] == "LINS-MacBook-Pro-408 · Cursor"
    engine._attribute_loose_turns()
    agents = engine.source_facets("session")["agents"]
    assert "LINS-MacBook-Pro-408 · Cursor" in agents
    assert not any("智能体「" in name or "\t" in name for name in agents)
    stored = {
        row["id"]: row
        for row in engine.db().execute("SELECT id, agent_id, agent_name FROM sources").fetchall()
    }
    assert stored["quoted"]["agent_id"] == agent_id
    assert stored["quoted"]["agent_name"] == "LINS-MacBook-Pro-408 · Cursor"
    assert stored["turn"]["agent_name"] == "LINS-MacBook-Pro-408 · Cursor"
    assert not stored["mention"]["agent_id"]
    assert stored["mention"]["agent_name"] == "上次在 LINS-MacBook-Pro-408 · Cursor 上试过"
    listed = engine.list_sources(tab="session", agent="LINS-MacBook-Pro-408 · Cursor")
    assert {row["id"] for row in listed["rows"]} == {"turn", "quoted", saved["id"]}


def test_a_quoted_short_name_is_that_host(tmp_path, monkeypatch):
    from rogmem.device_identity import normalize_device_name

    engine = _open(tmp_path, monkeypatch)
    agent_id, _public = _claim(engine, "dev-dsh", "", "DSH")
    _source(engine, "quoted", "draft", None, "", "智能体「DSH」")
    engine._attribute_loose_turns()
    row = engine.db().execute("SELECT agent_id, agent_name FROM sources WHERE id='quoted'").fetchone()
    assert row["agent_id"] == agent_id
    assert row["agent_name"] == f"{normalize_device_name(None)} · DSH"


def test_a_phrase_naming_two_hosts_stays_unassigned(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _claim(engine, "dev-study", "书房", "Hermes")
    _claim(engine, "dev-living", "客厅", "Cursor")
    phrase = "智能体「书房 · Hermes」和「客厅 · Cursor」"
    _source(engine, "both", "draft", None, "", phrase)
    engine._attribute_loose_turns()
    row = engine.db().execute("SELECT agent_id, agent_name FROM sources WHERE id='both'").fetchone()
    assert not row["agent_id"]
    assert row["agent_name"] == phrase


def test_session_names_follow_the_host(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _claim(engine, "dev-h", "书房", "Hermes")
    engine.ingest_history(
        [{"role": "user", "text": "从书房来的", "event_at": "2026-04-01T00:00:00+00:00", "session_id": "s", "seq": 1}],
        "Hermes",
        device_name="极空间",
    )
    stored = engine.db().execute("SELECT device_name, agent_name FROM sources").fetchone()
    assert stored["device_name"] == "书房"
    assert stored["agent_name"] == "书房 · Hermes"
    engine.db().execute("UPDATE sources SET device_name='极空间', agent_name='Hermes'")
    engine.db().commit()
    listed = engine.list_sources(tab="session")
    assert listed["rows"][0]["device_name"] == "书房"
    assert listed["rows"][0]["agent_name"] == "书房 · Hermes"
    facets = engine.source_facets("session")
    assert facets["devices"] == ["书房"]
    assert facets["agents"] == ["书房 · Hermes"]
    detail = engine.source_detail(listed["rows"][0]["id"])["source"]
    assert detail["device_name"] == "书房"
    assert detail["agent_name"] == "书房 · Hermes"


def test_history_with_two_hosts_stays_with_the_named_one(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    study, _left = _claim(engine, "dev-study", "书房", "Hermes")
    living, _right = _claim(engine, "dev-living", "客厅", "Hermes")
    engine.ingest_history(
        [{"role": "user", "text": "不知是哪台", "event_at": "2026-03-01T00:00:00+00:00", "session_id": "s", "seq": 1}],
        "Hermes",
        device_name="极空间",
    )
    engine.ingest_history(
        [{"role": "user", "text": "书房里的", "event_at": "2026-03-01T00:00:01+00:00", "session_id": "s", "seq": 2}],
        "Hermes",
        device_name="书房",
    )
    viewed = {item["id"]: item for item in engine.list_connected_hosts()["rows"]}
    assert viewed[study]["history_count"] == 1
    assert viewed[living]["history_count"] == 0


def test_connect_prompt_and_local_scan(tmp_path):
    from installer.rogmem_installer import (
        connect_prompt,
        forward,
        inspect_hosts,
        local_host_scan,
        viewer_identity,
    )

    from installer.rogmem_installer import onboard_prompt

    guide = onboard_prompt("rg_test")
    assert "rg_test" in guide
    assert "Cursor" in guide and "Hermes" in guide and "Codex" in guide and "WorkBuddy" in guide
    assert "redeem_grant" in guide and "待导入会话数" in guide
    assert "不要向用户再要 token" in guide
    assert "点「通过授权」" not in guide
    assert "只处理 Cursor" not in guide
    assert connect_prompt("Nope") == ""
    prompt = connect_prompt("Cursor")
    assert "Cursor" in prompt
    assert "allow_real=True" in prompt
    assert "18080" in prompt
    assert "18780" in prompt
    assert "不要写成「本机」" in prompt
    assert "宿主列表" in prompt
    assert "通过授权" in prompt
    assert inspect_hosts(Path.home()) == []

    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".cursor").mkdir(parents=True)
    for rel in (
        Path("Library") / "Application Support" / "Cursor",
        Path("AppData") / "Roaming" / "Cursor",
        Path(".config") / "Cursor",
    ):
        (home / rel).mkdir(parents=True, exist_ok=True)
    (home / ".rogmem").mkdir()
    (home / ".rogmem" / "device_id").write_text("dev-local\n", encoding="utf-8")
    (home / ".rogmem" / "device_name").write_text("AAAA\n", encoding="utf-8")
    (home / ".cursor" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "rogmem": {
                        "url": "http://127.0.0.1:18780/mcp",
                        "headers": {"Authorization": "Bearer rm_already"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    scanned = local_host_scan(home)
    by_program = {item["program"]: item for item in scanned["hosts"]}
    assert scanned["device_id"] == "dev-local"
    assert scanned["device_name"] == "AAAA"
    assert by_program["Cursor"]["status"] == "已接入"
    assert by_program["Cursor"]["prompt"] == ""
    assert by_program["Hermes"]["status"] == "未接入"
    assert "Hermes" in by_program["Hermes"]["prompt"]
    assert by_program["Codex"]["status"] == "未安装"
    assert by_program["Codex"]["prompt"] == ""
    assert by_program["WorkBuddy"]["status"] == "未安装"

    empty = tmp_path / "empty"
    empty.mkdir()
    assert viewer_identity(empty) == {"device_id": "", "device_name": ""}
    assert not (empty / ".rogmem").exists()

    seen = {}

    class Echo(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen["device"] = self.headers.get("X-Rogmem-Device", "")
            seen["name"] = self.headers.get("X-Rogmem-Device-Name", "")
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    target = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=target.serve_forever, daemon=True).start()
    listen = _free_port()
    proxy = forward(listen, target.server_address[1], home=home)
    try:
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{listen}/client/hosts", timeout=3) as resp:
            payload = json.loads(resp.read().decode())
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"
        assert payload["hosts"][0]["program"] == "Cursor"
        with urllib.request.urlopen(f"http://127.0.0.1:{listen}/client/whoami", timeout=3) as resp:
            who = json.loads(resp.read().decode())
        assert who["device_id"] == "dev-local"
        with urllib.request.urlopen(f"http://127.0.0.1:{listen}/admin/hosts", timeout=3) as resp:
            assert resp.read() == b"ok"
        assert seen["device"] == "dev-local"
        assert seen["name"] == "AAAA"
    finally:
        proxy.shutdown()
        target.shutdown()
    assert (home / ".rogmem" / "device_id").read_text(encoding="utf-8").strip() == "dev-local"


def test_forward_relays_the_hub_response_unchanged():
    """18780 is a pipe. The hub's status line and headers are what the client sees."""
    import socket

    from installer.rogmem_installer import forward

    payload = b'{"jsonrpc":"2.0","id":1,"result":{}}'
    upstream_response = (
        b"HTTP/1.1 200 OK\r\n"
        b"content-type: application/json\r\n"
        b"mcp-session-id: sess-1\r\n"
        b"x-rogmem-mark: hub\r\n"
        b"content-length: " + str(len(payload)).encode() + b"\r\n"
        b"\r\n" + payload
    )
    seen = {}

    def serve(conn: socket.socket) -> None:
        buf = b""
        while b"\r\n\r\n" not in buf:
            piece = conn.recv(4096)
            if not piece:
                break
            buf += piece
        seen["request"] = buf
        conn.sendall(upstream_response)
        conn.close()

    gate = threading.Event()

    def accept() -> None:
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        seen["port"] = server.getsockname()[1]
        gate.set()
        conn, _addr = server.accept()
        try:
            serve(conn)
        finally:
            server.close()

    threading.Thread(target=accept, daemon=True).start()
    assert gate.wait(2)
    listen = _free_port()
    proxy = forward(listen, seen["port"])
    try:
        client = socket.create_connection(("127.0.0.1", listen), timeout=3)
        client.sendall(
            b"POST /mcp HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Accept: application/json, text/event-stream\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"{}"
        )
        raw = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            raw += chunk
        client.close()
    finally:
        proxy.shutdown()
    assert raw == upstream_response
    request = seen["request"].decode("iso-8859-1")
    assert "Content-Type: application/json" in request
    assert "Accept: application/json, text/event-stream" in request


def test_a_private_address_is_the_computer_that_proved_it(tmp_path, monkeypatch):
    import hashlib

    engine = _open(tmp_path, monkeypatch)
    agent_id, _public = _claim(engine, "dev-a", "AAAA", "Cursor")
    token = "rm_presence"
    before = engine.db().execute("SELECT last_used FROM agents WHERE id=?", (agent_id,)).fetchone()["last_used"]
    engine.db().execute(
        "UPDATE agents SET key_hash=? WHERE id=?",
        (hashlib.sha256(token.encode()).hexdigest(), agent_id),
    )
    engine.db().commit()
    assert engine.device_for_key(token, "dev-a") == "dev-a"
    assert engine.device_for_key(token, "other") == ""
    assert engine.device_for_key("rm_nope", "dev-a") == ""
    after = engine.db().execute("SELECT last_used FROM agents WHERE id=?", (agent_id,)).fetchone()["last_used"]
    assert after == before

    clock = {"now": 1000.0}
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock["now"])
    engine.note_viewer_peer("dev-a", "192.168.31.50")
    engine.note_viewer_peer("dev-a", "172.17.0.1")
    engine.note_viewer_peer("dev-a", "127.0.0.1")
    engine.note_viewer_peer("", "10.1.1.1")
    assert engine.viewer_for_peer("192.168.31.50") == "dev-a"
    assert engine.viewer_for_peer("172.17.0.1") == ""
    assert engine.viewer_for_peer("127.0.0.1") == ""
    assert engine.viewer_for_peer("10.1.1.1") == ""
    engine.note_viewer_peer("dev-b", "192.168.31.50")
    assert engine.viewer_for_peer("192.168.31.50") == ""
    clock["now"] = 1000.0 + engine._VIEWER_PEER_TTL + 1
    engine.note_viewer_peer("dev-a", "10.2.2.2")
    assert engine.viewer_for_peer("10.2.2.2") == "dev-a"
    clock["now"] += engine._VIEWER_PEER_TTL + 1
    assert engine.viewer_for_peer("10.2.2.2") == ""

    from rogmem.app import _viewing_computer
    from rogmem.device_identity import header_device_name

    engine.note_viewer_peer("dev-a", "192.168.31.40")

    class Seen:
        headers: dict = {}
        client = type("C", (), {"host": "192.168.31.40"})()

    assert _viewing_computer(Seen()) == ("dev-a", "")

    class Named:
        headers = {"x-rogmem-device": "from-header", "x-rogmem-device-name": header_device_name("书房")}
        client = type("C", (), {"host": "192.168.31.40"})()

    assert _viewing_computer(Named()) == ("from-header", "书房")


def test_presence_route_checks_the_key_and_leaves_last_used(tmp_path, monkeypatch):
    import hashlib

    from starlette.testclient import TestClient

    from rogmem.app import build_app

    engine = _open(tmp_path, monkeypatch)
    agent_id, _public = _claim(engine, "dev-a", "AAAA", "Cursor")
    token = "rm_presence_route"
    engine.db().execute(
        "UPDATE agents SET key_hash=? WHERE id=?",
        (hashlib.sha256(token.encode()).hexdigest(), agent_id),
    )
    engine.db().commit()
    before = engine.db().execute("SELECT last_used FROM agents WHERE id=?", (agent_id,)).fetchone()["last_used"]
    client = TestClient(build_app())
    assert client.post("/host/presence").status_code == 401
    ok = client.post(
        "/host/presence",
        headers={"Authorization": f"Bearer {token}", "X-Rogmem-Device": "dev-a"},
    )
    assert ok.status_code == 200
    assert ok.json()["ok"] is True
    wrong = client.post(
        "/host/presence",
        headers={"Authorization": f"Bearer {token}", "X-Rogmem-Device": "other"},
    )
    assert wrong.status_code == 401
    after = engine.db().execute("SELECT last_used FROM agents WHERE id=?", (agent_id,)).fetchone()["last_used"]
    assert after == before


def test_the_door_reports_its_computer(monkeypatch):
    import rogmem.cli as cli

    seen = {}

    def open_direct(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        seen["auth"] = request.get_header("Authorization")
        seen["device"] = request.get_header("X-rogmem-device")

        class Resp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Resp()

    monkeypatch.setattr(
        cli,
        "_agent_bearers",
        lambda home: [
            {"program": "Cursor", "url": "http://127.0.0.1:18780/mcp", "token": "rm_test", "device": "dev-local"}
        ],
    )
    monkeypatch.setattr(cli._installer(), "open_direct", open_direct)
    cli._note_presence(Path("/tmp/rogmem-presence"))
    assert seen["url"] == "http://127.0.0.1:18780/host/presence"
    assert seen["timeout"] == 5
    assert seen["auth"] == "Bearer rm_test"
    assert seen["device"] == "dev-local"
    seen.clear()
    monkeypatch.setattr(cli, "_agent_bearers", lambda home: [])
    cli._note_presence(Path("/tmp/rogmem-presence"))
    assert seen == {}


def _memory_tools() -> tuple[set[str], set[str]]:
    source = Path(__file__).resolve().parents[1].joinpath("rogmem", "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    reads = set()
    writes = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # 计数器可以直接调用，也可以交给 to_thread 去跑；异步工具不能因此漏掉。
        used = {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}
        if "_count_memory_read" in used:
            reads.add(node.name)
        elif node.name in {"submit_turn", "guide", "submit_draft"}:
            writes.add(node.name)
    return reads, writes


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port
