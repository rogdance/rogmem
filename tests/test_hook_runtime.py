"""A registered hook is a command a host really runs, on every platform the host runs on.

Synthetic homes only. Nothing here reads or writes the real user home.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import installer.rogmem_installer as inst
import rogmem.host_hook as hook


def _enroll_workbuddy(home: Path) -> None:
    (home / ".workbuddy").mkdir(parents=True, exist_ok=True)
    (home / ".workbuddy" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "rogmem": {
                        "url": "http://127.0.0.1:18780/mcp",
                        "headers": {"Authorization": "Bearer rm_synthetic", "X-Rogmem-Device": "dev"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )


# ---- the registered string ---------------------------------------------------------------


def test_windows_registration_names_an_interpreter_the_host_and_the_home():
    command = inst.hook_command(
        Path(r"C:\Users\someone\.workbuddy\hooks\rogmem.py"),
        system="Windows",
        interpreter=r"C:\Tools\py\python.exe",
        program="WorkBuddy",
        home=Path(r"C:\Users\someone"),
    )
    # Bash eats backslashes and Windows has no shebang: forward slashes, interpreter first.
    assert "\\" not in command
    assert command.startswith("C:/Tools/py/python.exe C:/Users/someone/.workbuddy/hooks/rogmem.py")
    assert command.endswith("--program WorkBuddy --home C:/Users/someone")


def test_posix_registration_keeps_the_script_path():
    script = Path("/home/someone/.cursor/hooks/rogmem.py")
    assert inst.hook_command(script, system="Linux", program="Cursor", home=Path("/home/someone")) == str(script)


def test_an_earlier_shape_of_the_same_hook_is_replaced_not_kept_beside(tmp_path):
    settings = tmp_path / "settings.json"
    script = tmp_path / ".workbuddy" / "hooks" / "rogmem.py"
    old = str(script).replace("/", "\\")
    settings.write_text(
        json.dumps(
            {
                "theme": "dark",
                "hooks": {
                    "SessionStart": [
                        {"matcher": "", "hooks": [{"type": "command", "command": old, "timeout": 3}]},
                        {"matcher": "", "hooks": [{"type": "command", "command": "bash keep.sh", "timeout": 5}]},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    new = f"C:/py/python.exe {script.as_posix()} --program WorkBuddy"
    for _ in range(2):
        inst._merge_claude_hooks(settings, new, ("SessionStart", "Stop"), script=script)
    data = json.loads(settings.read_text(encoding="utf-8"))
    commands = [item["command"] for group in data["hooks"]["SessionStart"] for item in group["hooks"]]
    assert commands.count(new) == 1
    assert old not in commands
    assert "bash keep.sh" in commands
    assert data["theme"] == "dark"
    assert [item["command"] for group in data["hooks"]["Stop"] for item in group["hooks"]] == [new]


def test_a_hermes_registration_is_yaml_that_still_parses(tmp_path):
    config = tmp_path / "config.yaml"
    script = Path(r"C:\Users\someone\AppData\Local\hermes\agent-hooks\rogmem.py")
    # The shape an earlier install wrote: a double-quoted Windows path, where \U is an escape.
    config.write_text(
        "memory:\n  provider: rogmem\n"
        "hooks:\n"
        "  pre_llm_call:\n"
        f'    - command: "{script}"\n'
        "      timeout: 3\n"
        "    - command: /bin/echo keep\n"
        "      timeout: 1\n"
        "  post_llm_call:\n"
        f'    - command: "{script}"\n'
        "      timeout: 3\n",
        encoding="utf-8",
    )
    command = r"C:\odd path\python.exe " + str(script)
    for _ in range(2):
        inst._merge_hermes_hooks(config, command, script)
    text = config.read_text(encoding="utf-8")
    assert f'"{script}"' not in text
    assert text.count(inst._yaml_scalar(command)) == 2
    assert "/bin/echo keep" in text
    assert "provider: rogmem" in text
    for line in text.splitlines():
        if "- command:" in line and "rogmem.py" in line:
            value = line.split("- command:", 1)[1].strip()
            # Single-quoted YAML keeps backslashes literal; doubled quotes are the only escape.
            assert value.startswith("'") and value.endswith("'")
    assert inst._registered_hook_commands_from_yaml(text) == [command, "/bin/echo keep"]


# ---- the hook itself ---------------------------------------------------------------------


def test_the_probe_event_answers_without_touching_state(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(hook, "_core_text", lambda *a, **k: called.append(1) or "历史包")
    _enroll_workbuddy(tmp_path)
    result = hook.handle({"hook_event_name": "rogmem_probe", "session_id": "x"}, home=tmp_path, program="WorkBuddy")
    assert result["rogmem_probe"]["ok"] is True
    assert result["rogmem_probe"]["program"] == "WorkBuddy"
    assert result["rogmem_probe"]["enrolled"] is True
    assert called == []
    assert not (tmp_path / ".rogmem").exists()
    unknown = hook.handle({"hook_event_name": "rogmem_probe"}, home=tmp_path, program="")
    assert unknown["rogmem_probe"]["ok"] is False


def test_hermes_in_localappdata_is_recognised_from_its_path(tmp_path):
    script = tmp_path / "AppData" / "Local" / "hermes" / "agent-hooks" / "rogmem.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    assert hook.program_of(script) == "Hermes"
    assert hook.home_of(script) == Path.home()


def test_a_slow_hub_still_opens_the_session_with_the_last_good_packet(tmp_path, monkeypatch):
    _enroll_workbuddy(tmp_path)
    monkeypatch.setattr(hook, "_flush", lambda home, program, rows, timeout=30: [])
    monkeypatch.setattr(hook, "_core_text", lambda home, program, project="": "合成句：上一次的历史包。")
    first = hook.handle({"hook_event_name": "SessionStart", "session_id": "a"}, home=tmp_path, program="WorkBuddy")
    assert first["hookSpecificOutput"]["additionalContext"].endswith("合成句：上一次的历史包。")
    monkeypatch.setattr(hook, "_core_text", lambda home, program, project="": "")
    second = hook.handle({"hook_event_name": "SessionStart", "session_id": "b"}, home=tmp_path, program="WorkBuddy")
    context = second["hookSpecificOutput"]["additionalContext"]
    assert context.startswith(hook.PACKET_MARK)
    assert "没有及时回答" in context and context.endswith("合成句：上一次的历史包。")
    beat = json.loads((tmp_path / ".rogmem" / "hook-core" / "last-WorkBuddy.json").read_text(encoding="utf-8"))
    assert beat["outcome"] == "cache" and beat["event"] == "SessionStart"


def test_a_host_without_a_key_gets_nothing_from_the_cache(tmp_path, monkeypatch):
    _enroll_workbuddy(tmp_path)
    monkeypatch.setattr(hook, "_flush", lambda home, program, rows, timeout=30: [])
    monkeypatch.setattr(hook, "_core_text", lambda home, program, project="": "合成句：旧包。")
    hook.handle({"hook_event_name": "SessionStart", "session_id": "a"}, home=tmp_path, program="WorkBuddy")
    (tmp_path / ".workbuddy" / "mcp.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(hook, "_core_text", lambda home, program, project="": "")
    assert hook.handle({"hook_event_name": "SessionStart", "session_id": "b"}, home=tmp_path, program="WorkBuddy") == {}


def test_one_opening_fetch_per_session_when_the_host_fires_twice(tmp_path, monkeypatch):
    _enroll_workbuddy(tmp_path)
    calls = []
    monkeypatch.setattr(hook, "_flush", lambda home, program, rows, timeout=30: [])
    monkeypatch.setattr(hook, "_core_text", lambda home, program, project="": calls.append(1) or "包")
    claim = hook._marker(tmp_path, "WorkBuddy", "s", "claim")
    claim.parent.mkdir(parents=True)
    claim.write_text("", encoding="utf-8")  # another process is fetching right now
    assert hook.handle({"hook_event_name": "SessionStart", "session_id": "s"}, home=tmp_path, program="WorkBuddy") == {}
    assert calls == []
    old = time.time() - hook._CLAIM_STALE - 1
    import os

    os.utime(claim, (old, old))  # the other process died mid-fetch
    opened = hook.handle({"hook_event_name": "SessionStart", "session_id": "s"}, home=tmp_path, program="WorkBuddy")
    assert "包" in opened["hookSpecificOutput"]["additionalContext"]
    assert calls == [1]


_WRITER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import rogmem.host_hook as hook
home = Path(sys.argv[2])
for i in range(int(sys.argv[4])):
    hook._capture(home, "Cursor", {"hook_event_name": "beforeSubmitPrompt", "session_id": sys.argv[3], "prompt": f"合成句 {sys.argv[3]} {i}"}, drain=False)
"""


def test_concurrent_hook_processes_lose_no_turn(tmp_path):
    root = str(Path(__file__).resolve().parents[1])
    writers, each = 8, 25
    procs = [
        subprocess.Popen([sys.executable, "-c", _WRITER, root, str(tmp_path), f"s{n}", str(each)])
        for n in range(writers)
    ]
    for proc in procs:
        assert proc.wait(timeout=120) == 0
    rows = hook._read_outbox(tmp_path / ".rogmem" / "outbox.jsonl")
    assert len(rows) == writers * each
    assert len({row["text"] for row in rows}) == writers * each


# ---- executing what the host registered --------------------------------------------------


def test_the_installed_registration_really_runs_through_every_runner(tmp_path):
    home = tmp_path / "home"
    (home / ".workbuddy").mkdir(parents=True)
    _enroll_workbuddy(home)
    assert inst.install_host_hooks(home, ["WorkBuddy"]) == ["WorkBuddy"]
    rows = inst.verify_host_hooks(home, ["WorkBuddy"])
    assert len(rows) == 1, rows
    assert rows[0]["ok"], rows
    assert all(run["ok"] for run in rows[0]["runners"])
    assert not (home / ".rogmem" / "hook-core").exists()


def test_a_bare_windows_path_is_reported_as_not_running_without_starting_it(tmp_path):
    script = tmp_path / "home" / ".workbuddy" / "hooks" / "rogmem.py"
    result = inst.verify_hook_command("WorkBuddy", str(script).replace("/", "\\"), system="Windows")
    # Starting a .py by association pops 「打开方式」 on the desktop; the check must not do that.
    assert result["ok"] is False
    assert result["runners"] == [{"runner": "static", "ok": False, "code": "not-a-program", "seconds": 0.0}]
    assert inst._windows_not_a_program("C:/py/python.exe C:/x/rogmem.py --program WorkBuddy") is False


# ---- the local door does not run a tool call twice ---------------------------------------


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def test_a_slow_tool_call_is_not_sent_down_a_second_hop():
    seen = {"slow": 0, "entrance": 0}

    def answering(name, delay):
        class Hub(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
                seen[name] += 1
                time.sleep(delay)
                body = name.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        return Hub

    slow, slow_origin = _serve(answering("slow", inst._ENTRANCE_TIMEOUT + 1.5))
    entrance, entrance_origin = _serve(answering("entrance", 0))
    probe = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    door = probe.server_address[1]
    probe.server_close()
    proxy = inst.forward_origin(door, entrance_origin, hub="rh_test")
    proxy.RequestHandlerClass.cached_origin = slow_origin
    try:
        import urllib.request

        request = urllib.request.Request(f"http://127.0.0.1:{door}/mcp", data=b"{}", method="POST")
        with urllib.request.urlopen(request, timeout=15) as resp:
            assert resp.read() == b"slow"
        assert seen == {"slow": 1, "entrance": 0}
    finally:
        proxy.shutdown()
        slow.shutdown()
        entrance.shutdown()
