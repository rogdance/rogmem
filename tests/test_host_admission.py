"""The new-user door is python -m rogmem. One ledger, one confirm, one post."""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from installer.rogmem_installer import collect_history, stable_turn, write_configs  # noqa: E402
from rogmem import cli  # noqa: E402
from rogmem.hermes_memory import stable_seq, work_repository  # noqa: E402


def _ledger(home: Path) -> None:
    session = home / ".hermes"
    session.mkdir(parents=True)
    (session / "sessions").mkdir()
    (session / "sessions" / "session_old.json").write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "合成句：这份导出不该再被当成原文。",
                        "timestamp": "2026-06-01T00:00:00+00:00",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    db = session / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id TEXT, source TEXT, parent_session_id TEXT)")
    con.execute(
        "CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, content TEXT, timestamp REAL, active INTEGER)"
    )
    con.executemany(
        "INSERT INTO sessions VALUES (?,?,?)",
        [
            ("s-own", "desktop", ""),
            ("s-child", "desktop", "s-own"),
            ("s-cron", "cron", ""),
        ],
    )
    con.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?,?)",
        [
            (10, "s-own", "user", "合成句：北岸灯塔还亮着。", 1780000000, 1),
            (11, "s-own", "assistant", "合成句：灯塔的光记在账上。", 1780000001, 1),
            (12, "s-own", "tool", "工具结果不该进记忆", 1780000002, 1),
            (13, "s-own", "user", "合成句：已经收回的一句。", 1780000003, 0),
            (14, "s-child", "user", "合成句：子会话不该进来。", 1780000004, 1),
            (15, "s-cron", "user", "合成句：定时任务不该进来。", 1780000005, 1),
        ],
    )
    con.commit()
    con.close()


def test_hermes_ledger_is_the_transcript_and_seq_is_stable(tmp_path):
    home = tmp_path / "home"
    _ledger(home)
    collected = collect_history(home, allow_real=True)
    hermes = [turn for turn in collected["turns"] if turn["program"] == "Hermes"]
    texts = [turn["text"] for turn in hermes]
    assert texts == ["合成句：北岸灯塔还亮着。", "合成句：灯塔的光记在账上。"]
    assert "合成句：这份导出不该再被当成原文。" not in texts
    first = hermes[0]
    assert first["seq"] == stable_seq(first["session_id"], first["role"], first["text"])
    assert first["seq"] == stable_turn(first["session_id"], first["role"], first["text"])["seq"]
    assert first["event_at"]


class _Hub(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_GET(self):  # noqa: N802
        body = b'{"ok":true,"service":"rogmem"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode())
        self.seen.append({"path": self.path, "payload": payload})
        body = json.dumps(
            {"ok": True, "duplicates": 0, "imported": [], "projects": len(payload.get("projects") or [])}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def _serve() -> tuple[ThreadingHTTPServer, str]:
    _Hub.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Hub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}"


def test_confirm_posts_the_ledger_and_dry_run_does_not(tmp_path):
    home = tmp_path / "home"
    _ledger(home)
    server, base = _serve()
    try:
        dry = cli.main(
            ["history", "upload", "--dry-run", "--home", str(home), "--program", "Hermes", "--base-url", base]
        )
        assert dry == 0
        assert _Hub.seen == []
        posted = cli.main(
            ["history", "upload", "--confirm", "--home", str(home), "--program", "Hermes", "--base-url", base]
        )
        assert posted == 0
        assert len(_Hub.seen) == 1
        turns = _Hub.seen[0]["payload"]["turns"]
        assert [item["text"] for item in turns] == ["合成句：灯塔的光记在账上。", "合成句：北岸灯塔还亮着。"]
        again = cli.main(
            ["history", "upload", "--confirm", "--home", str(home), "--program", "Hermes", "--base-url", base]
        )
        assert again == 0
        second = _Hub.seen[1]["payload"]["turns"]
        assert [(item["session_id"], item["seq"], item["text"]) for item in second] == [
            (item["session_id"], item["seq"], item["text"]) for item in turns
        ]
    finally:
        server.shutdown()


def test_ingress_does_not_count_a_failed_post(tmp_path):
    del tmp_path
    from rogmem.ingress import accept, envelope

    env = envelope(
        kind="turn",
        body="合成句：送不出去就不能算已经导入。",
        event_at="2026-11-07T09:00:00+00:00",
        role="user",
        session_id="s-fail",
        seq=1,
    )
    result = accept([env], dry_run=False, post=lambda turns: {"ok": False, "refused": "post-failed"})
    assert result["posted"] == 0
    assert result["ok"] is False
    assert result["refused"] == "post-failed"


def test_connect_dry_run_previews_a_real_home_without_writing(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes" / "config.yaml").write_text("memory:\n  provider: onemem\n", encoding="utf-8")
    monkeypatch.setattr("installer.rogmem_installer._real_user_home", lambda path: True)
    assert cli.main(
        ["hosts", "connect", "--dry-run", "--home", str(home), "--program", "Hermes"]
    ) == 0
    text = capsys.readouterr().out
    assert "将指向" in text
    assert "真实用户主目录不会写入" not in text
    assert "provider: onemem" in (home / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    assert "rogmem" not in (home / ".hermes" / "config.yaml").read_text(encoding="utf-8")


def test_channel_stops_when_remote_access_is_closed(monkeypatch):
    monkeypatch.setattr("installer.rogmem_installer.find_rogmem", lambda *args, **kwargs: None)
    assert cli.main(["channel", "--home", "/tmp/rogmem-channel-closed"]) == 1


def test_token_points_hermes_memory_at_rogmem_and_live_turn_uses_the_same_seq(tmp_path):
    home = tmp_path / "home"
    _ledger(home)
    (home / ".hermes" / "config.yaml").write_text("memory:\n  provider: onemem\n", encoding="utf-8")
    assert write_configs(home, "http://127.0.0.1:18780", programs=["Hermes"]) == ["Hermes"]
    assert "provider: onemem" in (home / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    write_configs(home, "http://127.0.0.1:18780", "rm_test_token", ["Hermes"])
    text = (home / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    assert "provider: rogmem" in text
    assert "provider: onemem" not in text
    plugin = home / ".hermes" / "plugins" / "rogmem" / "__init__.py"
    assert plugin.is_file()
    assert "stable_seq" in plugin.read_text(encoding="utf-8")


def test_hermes_memory_account_replaces_the_previous_provider(tmp_path):
    home = tmp_path / "home"
    _ledger(home)
    hermes = home / ".hermes"
    config = hermes / "config.yaml"
    config.write_text(
        "memory:\n  provider: onemem\nmcp_servers:\n  onemem:\n    command: node\n"
        "  rea:\n    command: rea\n",
        encoding="utf-8",
    )
    plugin = hermes / "plugins" / "onemem"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: onemem\nruntime: hermes_memory_provider\n", encoding="utf-8")
    (plugin / "__init__.py").write_text("class MemoryProvider:\n    pass\n", encoding="utf-8")
    profile = hermes / "profiles" / "nuan"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("memory:\n  provider: onemem\nmcp_servers: {}\n", encoding="utf-8")
    write_configs(home, "http://127.0.0.1:18780", "rm_test_token", ["Hermes"])
    text = config.read_text(encoding="utf-8")
    assert "provider: rogmem" in text
    assert "onemem:" not in text
    assert "rea:" in text
    assert not plugin.exists()
    profile_text = (profile / "config.yaml").read_text(encoding="utf-8")
    assert "provider: rogmem" in profile_text
    assert "url: http://127.0.0.1:18780/mcp" in profile_text
    assert (profile / "plugins" / "rogmem" / "plugin.yaml").is_file()

    from rogmem.hermes_memory import RogmemMemoryProvider

    server, base = _serve()
    try:
        provider = RogmemMemoryProvider()
        provider.initialize("s-own", hermes_home=str(home / ".hermes"), hub_url=base)
        provider.sync_turn("合成句：北岸灯塔还亮着。", "合成句：灯塔的光记在账上。", session_id="s-own")
        live = _Hub.seen[-1]["payload"]["turns"]
        history = collect_history(home, allow_real=True)
        hermes = [turn for turn in history["turns"] if turn["program"] == "Hermes"]
        assert [(item["seq"], item["text"]) for item in live] == [(item["seq"], item["text"]) for item in hermes]
    finally:
        server.shutdown()


def test_follow_sends_the_ledger_once_and_skips_it_next_time(tmp_path):
    home = tmp_path / "home"
    _ledger(home)
    server, base = _serve()
    try:
        first = cli.import_fresh_hermes(home, base)
        assert first["ok"] is True
        assert first["posted"] == 2
        assert len(_Hub.seen) == 1
        second = cli.import_fresh_hermes(home, base)
        assert second["posted"] == 0
        assert len(_Hub.seen) == 1
    finally:
        server.shutdown()


def test_envelope_keeps_the_repository():
    from rogmem import ingress

    env = ingress.envelope(
        kind="turn",
        body="合成句：北岸仓库还在。",
        event_at="2026-04-01T00:00:00+00:00",
        role="user",
        session_id="s-repo",
        seq=1,
        project="https://git.example.test/org/demo.git",
    )
    checked = ingress.validate(env)
    turn = ingress.turn_from_envelope(checked["envelope"])
    assert turn["project"] == "https://git.example.test/org/demo.git"
    plain = ingress.turn_from_envelope(ingress.envelope(kind="turn", body="合成句：没有仓库。", role="user", event_at="2026-04-01T00:00:01+00:00"))
    assert "project" not in plain


def test_work_repository_names_the_checkout_and_drops_credentials():
    origin = "https://user:secret@git.example.test/org/demo.git"
    assert (
        work_repository("/work/demo", "", resolve=lambda _path: (origin, "/work/demo"))
        == "https://git.example.test/org/demo"
    )
    assert work_repository("/work/demo", "", resolve=lambda _path: ("", "/work/demo")) == "/work/demo"
    assert (
        work_repository("/missing", "https://git.example.test/org/demo.git", resolve=lambda _path: None)
        == "https://git.example.test/org/demo"
    )
    assert work_repository("/gone/demo", "/gone/demo", resolve=lambda _path: None) == "/gone/demo"
    assert work_repository("", "") == ""


def test_renamed_account_keeps_the_repository_name():
    from rogmem.hermes_memory import choose_project_ref, current_remote

    old = "https://github.com/old-name/demo.git"
    assert current_remote(old, ask=lambda _url: "https://github.com/new-name/demo") == "https://github.com/new-name/demo"
    assert current_remote(old, ask=lambda _url: "") == "https://github.com/old-name/demo"
    assert current_remote(old, ask=lambda _url: "https://github.com/new-name/other") == "https://github.com/old-name/demo"
    assert choose_project_ref(old, "https://github.com/new-name/demo") == "https://github.com/new-name/demo"
    assert choose_project_ref(old, "https://github.com/new-name/other") == old


def test_repository_spellings_are_one_project():
    from rogmem.hermes_memory import repository_key

    key = "git.example.test/org/demo"
    assert repository_key("git@git.example.test:org/demo.git") == key
    assert repository_key("https://user:secret@git.example.test/org/demo.git") == key
    assert repository_key("ssh://git@git.example.test:22/org/demo.git") == key
    assert repository_key("https://git.example.test/org/demo-extra") != key


def test_empty_git_directory_is_not_a_project(tmp_path):
    import subprocess

    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    assert work_repository(str(repo), str(repo)) == ""


def test_git_probe_ignores_the_parent_repository(tmp_path, monkeypatch):
    from rogmem import hermes_memory

    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs.get("env") or {}

        class Result:
            returncode = 1
            stdout = ""

        return Result()

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "other"))
    monkeypatch.setattr(hermes_memory.subprocess, "run", fake_run)
    assert hermes_memory._git_line(str(work), ["rev-parse", "--show-toplevel"]) == ""
    assert "GIT_DIR" not in seen["env"]
    assert seen["env"].get("GIT_TERMINAL_PROMPT") == "0"


def test_hermes_ledger_marks_the_recorded_repository(tmp_path):
    home = tmp_path / "home"
    session = home / ".hermes"
    session.mkdir(parents=True)
    db = session / "state.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE sessions (id TEXT, source TEXT, parent_session_id TEXT, cwd TEXT, git_repo_root TEXT)"
    )
    con.execute(
        "CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, content TEXT, timestamp REAL, active INTEGER)"
    )
    con.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?)",
        (
            "s-repo",
            "desktop",
            "",
            str(tmp_path / "missing-work"),
            "https://user:secret@git.example.test/org/demo.git",
        ),
    )
    con.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", ("s-plain", "desktop", "", "", ""))
    con.execute("INSERT INTO messages VALUES (1,'s-repo','user','合成句：北岸仓库还在。',1780000100,1)")
    con.execute("INSERT INTO messages VALUES (2,'s-plain','user','合成句：这段没有工作目录。',1780000101,1)")
    con.commit()
    con.close()
    collected = collect_history(home, allow_real=True)
    by_id = {turn["session_id"]: turn for turn in collected["turns"] if turn["program"] == "Hermes"}
    assert by_id["s-repo"]["project"] == "https://git.example.test/org/demo"
    assert "secret" not in by_id["s-repo"]["project"]
    assert "project" not in by_id["s-plain"]


def test_follow_posts_the_repository_for_sessions_already_sent(tmp_path):
    home = tmp_path / "home"
    session = home / ".hermes"
    session.mkdir(parents=True)
    db = session / "state.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE sessions (id TEXT, source TEXT, parent_session_id TEXT, cwd TEXT, git_repo_root TEXT)"
    )
    con.execute(
        "CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, content TEXT, timestamp REAL, active INTEGER)"
    )
    con.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?)",
        ("s-repo", "desktop", "", str(tmp_path / "missing-work"), "https://git.example.test/org/demo.git"),
    )
    con.execute("INSERT INTO messages VALUES (1,'s-repo','user','合成句：北岸仓库还在。',1780000200,1)")
    con.commit()
    con.close()
    server, base = _serve()
    try:
        first = cli.import_fresh_hermes(home, base)
        assert first["ok"] is True
        assert first["posted"] == 1
        assert _Hub.seen[0]["payload"]["turns"][0]["project"] == "https://git.example.test/org/demo"
        assert _Hub.seen[1]["payload"]["projects"] == [
            {"session_id": "s-repo", "project": "https://git.example.test/org/demo"}
        ]
        second = cli.import_fresh_hermes(home, base)
        assert second["posted"] == 0
        assert len(_Hub.seen) == 2
    finally:
        server.shutdown()


def test_a_closed_channel_records_when_the_gap_starts_and_not_the_text(tmp_path):
    home = tmp_path / "home"
    turns = [
        {
            "session_id": "s",
            "seq": 2,
            "event_at": "2026-05-01T00:00:00+00:00",
            "text": "合成句：这段原文不该写进缺口。",
        },
        {
            "session_id": "s",
            "seq": 1,
            "event_at": "2026-04-01T00:00:00+00:00",
            "text": "合成句：更早的一句也不该写进缺口。",
        },
    ]
    cli.note_upload_gap(home, turns)
    raw = (home / ".rogmem" / "upload-gap.json").read_text(encoding="utf-8")
    saved = json.loads(raw)
    assert saved == {"since": "2026-04-01T00:00:00+00:00", "pending": 2}
    assert "合成句" not in raw
    cli.note_upload_gap(
        home,
        [{"session_id": "s", "seq": 3, "event_at": "2026-06-01T00:00:00+00:00"}],
    )
    assert json.loads((home / ".rogmem" / "upload-gap.json").read_text(encoding="utf-8")) == {
        "since": "2026-06-01T00:00:00+00:00",
        "pending": 1,
    }
    assert [item["program"] for item in cli._in_scope(
        [{"program": "Cursor"}, {"program": "Hermes"}],
        ["Cursor"],
    )] == ["Cursor"]
    cli._arm_resume(home, "http://127.0.0.1:18780", ["Cursor"])
    cli._arm_resume(home, "http://127.0.0.1:18780", ["Hermes"])
    assert json.loads((home / ".rogmem" / "history.scope").read_text(encoding="utf-8")) == ["Cursor", "Hermes"]
    cli.clear_upload_gap(home)
    assert not (home / ".rogmem" / "upload-gap.json").exists()
