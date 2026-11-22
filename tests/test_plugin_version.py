"""One rogmem plugin, one version, and a hub that knows which version every host runs.

The plugin is everything the hub installs on a computer (hook, rules, Hermes provider,
DSH plugin, client, local door). Synthetic homes and an in-process hub only.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest

import installer.rogmem_installer as inst
import rogmem.host_hook as hook
from rogmem import plugin
from rogmem.host_client import _wheel, client_files, plugin_version_of


# ---- the version is the content ----------------------------------------------------------


def test_the_version_is_the_series_and_a_digest_of_exactly_what_ships():
    files = client_files()
    value = plugin_version_of(files)
    series, digest = value.split(".")
    assert int(series) == plugin.SERIES
    assert len(digest) == 12 and all(ch in "0123456789abcdef" for ch in digest)
    assert plugin.version() == value
    # Line endings are a checkout setting, not a plugin change.
    lf = {name: body.replace(b"\r\n", b"\n") for name, body in files.items()}
    crlf = {name: body.replace(b"\n", b"\r\n") for name, body in lf.items()}
    assert plugin_version_of(lf) == value
    assert plugin_version_of(crlf) == value
    # Any byte of any shipped file moves it.
    changed = dict(files)
    changed["rogmem/host_hook.py"] = files["rogmem/host_hook.py"] + b"\n# changed\n"
    assert plugin_version_of(changed) != value


def test_the_wheel_carries_its_own_version(tmp_path):
    import io
    import zipfile

    files = client_files()
    with zipfile.ZipFile(io.BytesIO(_wheel(files))) as archive:
        stamped = archive.read("rogmem/PLUGIN_VERSION").decode().strip()
    assert stamped == plugin_version_of(files)


def test_compare_names_where_a_host_stands():
    latest = f"{plugin.SERIES}.aaaaaaaaaaaa"
    assert plugin.compare(latest, latest) == plugin.CURRENT
    assert plugin.compare(f"{plugin.SERIES}.bbbbbbbbbbbb", latest) == plugin.OUTDATED
    assert plugin.compare("", latest) == plugin.UNREPORTED
    assert plugin.compare("1.cccccccccccc", latest) == plugin.INCOMPATIBLE
    # A hand-made rev from the old content-id scheme has no series: it is not compatible.
    assert plugin.compare("0a1b2c3d4e5f", latest) == plugin.INCOMPATIBLE
    assert plugin.notice(plugin.CURRENT, latest, latest) == ""
    told = plugin.notice(plugin.OUTDATED, "2.x", latest, "Cursor")
    assert "Cursor" in told and plugin.UPDATE_COMMAND in told and "不换钥匙" in told


# ---- the computer records what it installed, and the hook reports it ----------------------


def _enroll(home: Path) -> None:
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


def test_an_update_writes_one_manifest_for_the_whole_plugin(tmp_path):
    home = tmp_path / "home"
    _enroll(home)
    result = inst.refresh_host_bundle(home, ["WorkBuddy"])
    assert result["ok"] and result["version"] == plugin.version()
    manifest = json.loads((home / ".rogmem" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["version"] == plugin.version()
    assert manifest["programs"] == ["WorkBuddy"]
    assert plugin.installed(home)["version"] == plugin.version()
    assert inst.installed_bundle_rev(home) == plugin.version()


def test_every_hook_call_carries_the_plugin_version(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _enroll(home)
    inst.refresh_host_bundle(home, ["WorkBuddy"])
    seen = {}

    def post(url, payload, headers, timeout):
        seen.update(headers)
        return {"result": {"content": [{"type": "text", "text": json.dumps({"loaded": []})}]}}

    monkeypatch.setattr(hook, "_post_json", post)
    monkeypatch.setattr(hook, "_flush", lambda home, program, rows, timeout=30: [])
    hook.handle({"hook_event_name": "SessionStart", "session_id": "s"}, home=home, program="WorkBuddy")
    assert seen.get("X-Rogmem-Plugin") == plugin.version()
    probe = hook.handle({"hook_event_name": "rogmem_probe"}, home=home, program="WorkBuddy")
    assert probe["rogmem_probe"]["plugin"] == plugin.version()


def test_a_behind_verdict_reaches_the_agent_as_one_visible_line(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _enroll(home)
    verdict = {
        "installed": "2.old000000000",
        "latest": "2.new000000000",
        "status": "outdated",
        "notice": "本机 rogmem 插件 2.old 落后于中枢 2.new。",
    }
    body = json.dumps({"loaded": [{"name": "记忆主人", "text": "合成句"}], "plugin": verdict}, ensure_ascii=False)
    monkeypatch.setattr(hook, "_core_text", lambda home, program, project="": body)
    monkeypatch.setattr(hook, "_flush", lambda home, program, rows, timeout=30: [])
    context = hook.handle({"hook_event_name": "SessionStart", "session_id": "s"}, home=home, program="WorkBuddy")
    text = context["hookSpecificOutput"]["additionalContext"]
    assert text.splitlines()[1] == "【rogmem 插件】" + verdict["notice"]
    current = json.dumps({"loaded": [], "plugin": {**verdict, "status": "current"}})
    monkeypatch.setattr(hook, "_core_text", lambda home, program, project="": current)
    quiet = hook.handle({"hook_event_name": "SessionStart", "session_id": "t"}, home=home, program="WorkBuddy")
    assert "【rogmem 插件】" not in quiet["hookSpecificOutput"]["additionalContext"]


# ---- the hub: publishes its version, remembers each host's, judges in core ---------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def hub(tmp_path, monkeypatch):
    from rogmem import engine

    data = tmp_path / "data"
    monkeypatch.setenv("ROGMEM_DATA", str(data))
    monkeypatch.setenv("ROGMEM_SCAN", str(data / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_POLL", "3600")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    engine.connect(data, data / "scan")
    engine.hub_id()
    token = "rm_plugin_test"
    digest = __import__("hashlib").sha256(token.encode()).hexdigest()
    engine.db().execute(
        """INSERT INTO agents(id,device_id,device_name,program,display_name,status,pubkey,key_hash,created_at)
           VALUES('agent-p','dev-p','电脑P','Cursor','电脑P · Cursor','启用','cHVi',?,?)""",
        (digest, "2026-11-22T00:00:00+00:00"),
    )
    engine.db().commit()
    from rogmem.app import build_app, mcp
    import uvicorn

    mcp._session_manager = None
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=0.3):
                break
        except Exception:
            time.sleep(0.05)
    yield {"base": base, "token": token, "engine": engine}
    server.should_exit = True
    thread.join(timeout=3)


def _core(hub, reported: str) -> dict:
    headers = {
        "Authorization": f"Bearer {hub['token']}",
        "X-Rogmem-Device": "dev-p",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if reported:
        headers["X-Rogmem-Plugin"] = reported
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "core", "arguments": {}}})
    request = urllib.request.Request(hub["base"] + "/mcp", data=body.encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=20) as resp:
        raw = resp.read().decode()
    payload = hook._decode_rpc(raw)
    return json.loads(hook._tool_text(payload))


def test_the_hub_publishes_the_plugin_it_serves(hub):
    with urllib.request.urlopen(hub["base"] + "/plugin.json", timeout=5) as resp:
        manifest = json.loads(resp.read())
    assert manifest["version"] == plugin.version()
    assert manifest["min_series"] == plugin.MIN_SERIES
    assert manifest["update"] == plugin.UPDATE_COMMAND
    with urllib.request.urlopen(hub["base"] + "/healthz", timeout=5) as resp:
        assert json.loads(resp.read())["plugin"] == plugin.version()


def test_the_hub_remembers_each_hosts_version_and_judges_it_in_core(hub):
    engine = hub["engine"]
    behind = f"{plugin.SERIES}.000000000000"
    packet = _core(hub, behind)
    assert packet["plugin"]["status"] == "outdated"
    assert packet["plugin"]["latest"] == plugin.version()
    assert "Cursor" in packet["plugin"]["notice"]
    row = engine.db().execute("SELECT plugin_version, plugin_seen_at FROM agents WHERE id='agent-p'").fetchone()
    assert row["plugin_version"] == behind and row["plugin_seen_at"]
    fleet = engine.list_connected_hosts()
    mine = next(item for item in fleet["rows"] if item["id"] == "agent-p")
    assert mine["plugin_status"] == "outdated"
    now = _core(hub, plugin.version())
    assert now["plugin"]["status"] == "current" and now["plugin"]["notice"] == ""
    # An agent's own MCP call carries no plugin header and gets no verdict.
    assert "plugin" not in _core(hub, "")


def test_the_host_page_shows_each_version_and_a_banner_only_when_someone_is_behind(tmp_path, monkeypatch):
    from rogmem.ui import hosts_page, plugin_fleet_summary

    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    row = {
        "id": "a", "device_id": "d", "device_name": "电脑B", "device_base": "电脑B", "local": False,
        "agent_name": "电脑B · Cursor", "program": "Cursor", "status": "启用", "code": "",
        "connected_at": "", "last_used": "", "session_count": 0, "history_count": 0, "call_count": 1,
        "plugin_version": "2.000000000000", "plugin_seen_at": "2026-11-22T00:00:00+00:00", "plugin_status": "outdated",
    }
    fleet = {"rows": [row], "facets": {"devices": [], "agents": []}, "viewer_known": False}
    page = hosts_page([], fleet, {})
    assert "插件版本" in page
    assert "2.000000000000" in page and "落后" in page
    banner = page.split('id="update-hosts-note"', 1)[1].split("</div>", 1)[0]
    assert " hidden" not in banner.split(">", 1)[0]
    assert "电脑B" in banner and "python -m rogmem plugin update" in banner
    current = {**row, "plugin_version": plugin.version(), "plugin_status": "current"}
    quiet = hosts_page([], {**fleet, "rows": [current]}, {})
    assert quiet.split('id="update-hosts-note"', 1)[1].split(">", 1)[0].endswith(" hidden")
    assert plugin_fleet_summary([current, {**row, "status": "停用"}]) == {"enabled": 1, "behind": 0, "devices": []}


# ---- the update path ---------------------------------------------------------------------


def test_the_update_prompt_is_one_command_and_never_a_new_key():
    from rogmem.onboard import host_update_prompt

    text = host_update_prompt()
    assert "python -m rogmem plugin update" in text
    assert "python -m rogmem plugin status" in text
    assert "不要新领钥匙" in text and "不要克隆 git 仓库" in text


def test_plugin_status_compares_this_computer_with_the_hub(tmp_path, monkeypatch):
    from rogmem import plugin_cli

    home = tmp_path / "home"
    _enroll(home)
    monkeypatch.setattr(plugin_cli, "hub_plugin", lambda door=plugin_cli.DOOR: {"version": "2.ffffffffffff"})
    behind = plugin_cli.status(home, verify=False)
    assert behind["status"] == "unreported" and behind["ok"] is False
    inst.refresh_host_bundle(home, ["WorkBuddy"])
    still = plugin_cli.status(home, verify=False)
    assert still["status"] == "outdated" and plugin.UPDATE_COMMAND in still["text"]
    monkeypatch.setattr(plugin_cli, "hub_plugin", lambda door=plugin_cli.DOOR: {"version": plugin.version()})
    assert plugin_cli.status(home, verify=False)["status"] == "current"


def test_an_update_refuses_a_door_that_is_not_this_hub(tmp_path, monkeypatch):
    from rogmem import plugin_cli

    home = tmp_path / "home"
    (home / ".rogmem").mkdir(parents=True)
    (home / ".rogmem" / "hub").write_text("rh_mine\n", encoding="utf-8")
    monkeypatch.setattr(plugin_cli, "hub_plugin", lambda door=plugin_cli.DOOR: {"version": "2.x"})
    monkeypatch.setattr(plugin_cli, "_hub_identity", lambda door: "rh_someone_else")
    result = plugin_cli.update(home, restart_door=False)
    assert result["ok"] is False and result["refused"] == "door-not-this-hub"


def test_the_release_machine_must_run_the_plugin_it_ships():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "skills" / "zspace-release" / "smoke.py"
    if not path.is_file():
        pytest.skip("the release skill lives in the private repository")
    spec = importlib.util.spec_from_file_location("smoke_for_plugin", path)
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    assert smoke.check_plugin({"plugin": "2.a"}, "2.a") == []
    errors = smoke.check_plugin({"plugin": "2.a"}, "2.b")
    assert errors and "plugin update" in errors[0]
    assert smoke.bringup_pending(errors) is False
    assert smoke.check_plugin({}, "2.a") == ["中枢没有公布插件版本（healthz 缺 plugin）"]


def test_the_door_launcher_follows_the_plugin_interpreter(tmp_path, monkeypatch):
    import os

    from rogmem import plugin_cli

    home = tmp_path / "home"
    (home / ".rogmem").mkdir(parents=True)
    (home / ".rogmem" / "hub").write_text("rh_mine\n", encoding="utf-8")
    path = plugin_cli.write_door_launcher(home, r"C:/Py/python.exe")
    if os.name != "nt":
        assert path is None
        return
    text = path.read_text(encoding="utf-8")
    assert path.as_posix().endswith("Startup/rogmem-channel.vbs")
    assert "channel" in text and "--session" in text and "rh_mine" in text
    assert ", 0, False" in text and "schtasks" not in text and "rm_" not in text


def test_a_running_door_hands_over_when_the_plugin_changes(tmp_path):
    import rogmem.cli as cli

    home = tmp_path / "home"
    (home / ".rogmem").mkdir(parents=True)
    manifest = home / ".rogmem" / "plugin.json"
    manifest.write_text(json.dumps({"version": "2.aaaaaaaaaaaa"}), encoding="utf-8")
    started = cli._installed_plugin(home)
    assert started == "2.aaaaaaaaaaaa"
    assert cli._door_should_hand_over(home, started) is False
    manifest.write_text(json.dumps({"version": "2.bbbbbbbbbbbb"}), encoding="utf-8")
    assert cli._door_should_hand_over(home, started) is True
    # A door started before any manifest existed never hands over on its own.
    assert cli._door_should_hand_over(home, "") is False


def test_an_update_never_stops_a_door_it_cannot_replace(monkeypatch, tmp_path):
    from rogmem import plugin_cli

    monkeypatch.setattr(plugin_cli, "_door_pid", lambda: 4242)
    monkeypatch.setattr(plugin_cli.time, "sleep", lambda _s: None)
    stopped = []
    monkeypatch.setattr(plugin_cli, "_stop", lambda pid: stopped.append(pid))
    assert plugin_cli._restart_door(tmp_path, "python") == "next-login"
    assert stopped == []
