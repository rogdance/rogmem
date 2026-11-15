"""A computer installs python -m rogmem from the hub that issued the prompt."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from starlette.testclient import TestClient

from installer import rogmem_installer as inst
from rogmem.host_client import bake_bundle, bundle_bytes, bundle_filename, client_files
from rogmem.onboard import ORIGIN_MARK, host_client_instruction, onboard_prompt

ROOT = Path(__file__).resolve().parents[1]


def _wheel_names(payload: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return set(archive.namelist())


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    import threading

    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def test_client_wheel_is_the_host_role_only():
    names = _wheel_names(bundle_bytes())
    assert "rogmem/__main__.py" in names
    assert "rogmem/cli.py" in names
    assert "installer/rogmem_installer.py" in names
    assert "host/dsh/index.js" in names
    assert "rogmem/app.py" not in names
    assert "rogmem/engine.py" not in names
    assert bundle_filename().endswith(".whl")


def test_checkout_without_a_bake_has_nothing_to_serve(tmp_path, monkeypatch):
    monkeypatch.setattr("rogmem.host_client.package_root", lambda: tmp_path)
    monkeypatch.setattr("rogmem.host_client.baked_path", lambda: tmp_path / "missing.whl")
    assert bundle_bytes() is None


def test_image_serves_the_baked_wheel_after_the_checkout_is_removed(tmp_path, monkeypatch):
    baked = tmp_path / "host_client.whl"
    bake_bundle(ROOT, dest=baked)
    monkeypatch.setattr("rogmem.host_client.package_root", lambda: tmp_path / "image")
    monkeypatch.setattr("rogmem.host_client.baked_path", lambda: baked)
    assert bundle_bytes() == baked.read_bytes()
    assert "rogmem/app.py" not in _wheel_names(bundle_bytes())


def test_config_home_counts_as_cursor(tmp_path):
    home = tmp_path / "cloud"
    (home / ".cursor").mkdir(parents=True)
    row = next(item for item in inst.detect_programs(home) if item["program"] == "Cursor")
    assert row["found"] is True
    assert row["path"].endswith(".cursor")


def test_channel_on_the_local_door_does_not_open_another(monkeypatch):
    import rogmem.cli as cli

    monkeypatch.setattr(
        inst,
        "plan_channel",
        lambda origin, hub="": {"ok": True, "mode": "ready", "refused": "", "origin": origin, "target": 0, "location": "", "hub": hub},
    )
    assert cli.main(["channel", "--origin", "http://127.0.0.1:18780", "--home", "/tmp/rogmem-door"]) == 0


def test_a_direct_hub_is_handed_to_the_login_session(tmp_path, monkeypatch):
    import plistlib

    import rogmem.cli as cli

    monkeypatch.setattr(
        inst,
        "plan_channel",
        lambda origin, hub="": {"ok": True, "mode": "url", "refused": "", "origin": origin, "target": 0, "location": "", "hub": hub},
    )
    monkeypatch.setattr(inst, "door_stands_alone", lambda origin: True)
    code = cli.main(["channel", "--origin", "http://hub.example", "--home", str(tmp_path)])
    assert code == 0
    system = inst.platform.system()
    if system == "Darwin":
        plist = tmp_path / "Library" / "LaunchAgents" / "com.rogmem.channel.plist"
        data = plistlib.loads(plist.read_bytes())
        assert data["Label"] == "com.rogmem.channel"
        assert data["KeepAlive"] is True
        assert data["RunAtLoad"] is True
        assert "--session" in data["ProgramArguments"]
        assert "http://hub.example" in data["ProgramArguments"]
        assert data["StandardErrorPath"].endswith("rogmem-channel.log")
        text = plist.read_text(encoding="utf-8")
    elif system == "Linux":
        text = (tmp_path / ".config/systemd/user/rogmem-channel.service").read_text(encoding="utf-8")
        assert "--session" in text
        assert "http://hub.example" in text
        assert "Restart=on-failure" in text
    else:
        text = (tmp_path / ".rogmem/channel-service.cmd").read_text(encoding="utf-8")
        assert "--session" in text
        assert "http://hub.example" in text
    assert "rm_" not in text
    assert "cookie" not in text.lower()


def test_the_login_door_keeps_the_hub_identity():
    named = inst.session_unit("Linux", "http://hub.example", "/usr/bin/python3", "/work", "rh_login")
    assert "--hub rh_login" in named["text"]
    assert "cookie" not in named["text"].lower()
    plain = inst.session_unit("Linux", "http://hub.example", "/usr/bin/python3", "/work")
    assert "--hub" not in plain["text"]


def test_other_systems_keep_the_same_login_door(tmp_path):
    linux = inst.session_unit("Linux", "http://hub.example", "/usr/bin/python3", "/work")
    assert linux["kind"] == "systemd"
    assert "Restart=on-failure" in linux["text"]
    assert "--session" in linux["text"]
    assert "http://hub.example" in linux["text"]
    written = inst.write_session_unit(tmp_path, linux)
    assert written.name == "rogmem-channel.service"
    windows = inst.session_unit("Windows", "http://hub.example", r"C:\Python\python.exe", r"C:\work")
    assert windows["kind"] == "task"
    assert "--session" in windows["text"]
    assert "http://hub.example" in windows["text"]
    assert "cookie" not in windows["text"].lower()
    assert "rm_" not in windows["text"]


def test_a_login_wall_stays_with_the_process_that_holds_the_cookie(tmp_path, monkeypatch):
    import rogmem.cli as cli

    monkeypatch.setattr(
        inst,
        "plan_channel",
        lambda origin, hub="": {"ok": True, "mode": "url", "refused": "", "origin": origin, "target": 0, "location": "", "hub": hub},
    )
    monkeypatch.setattr(inst, "door_stands_alone", lambda origin: False)
    seen = {}

    def forward_origin(listen, origin, home=None, hub=""):
        seen["origin"] = origin

        class Server:
            def shutdown(self):
                return None

        return Server()

    monkeypatch.setattr(inst, "forward_origin", forward_origin)
    monkeypatch.setattr("rogmem.host_hook.drain_outbox", lambda home: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        cli.main(["channel", "--origin", "http://hub.example", "--home", str(tmp_path)])
    except KeyboardInterrupt:
        pass
    assert seen["origin"] == "http://hub.example"
    assert not (tmp_path / "Library" / "LaunchAgents" / "com.rogmem.channel.plist").exists()


def test_the_session_process_does_not_install_another_door(tmp_path, monkeypatch):
    import rogmem.cli as cli

    monkeypatch.setattr(
        inst,
        "plan_channel",
        lambda origin, hub="": {"ok": True, "mode": "url", "refused": "", "origin": origin, "target": 0, "location": "", "hub": hub},
    )
    monkeypatch.setattr(inst, "door_stands_alone", lambda origin: True)
    seen = {}

    def forward_origin(listen, origin, home=None, hub=""):
        seen["held"] = True

        class Server:
            def shutdown(self):
                return None

        return Server()

    monkeypatch.setattr(inst, "forward_origin", forward_origin)
    monkeypatch.setattr("rogmem.host_hook.drain_outbox", lambda home: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        cli.main(["channel", "--session", "--origin", "http://hub.example", "--home", str(tmp_path)])
    except KeyboardInterrupt:
        pass
    assert seen["held"] is True
    assert not (tmp_path / "Library" / "LaunchAgents" / "com.rogmem.channel.plist").exists()


def test_a_loopback_tunnel_is_not_a_hub_the_login_can_own():
    assert inst.door_stands_alone("http://127.0.0.1:10002") is False
    assert inst.door_stands_alone("http://localhost:18080") is False
    assert inst.door_stands_alone("http://[::1]:18080") is False


def test_launchd_holding_is_the_running_job_not_a_scheduled_restart():
    running = "\tstate = running\n\tpid = 2096\n\tlast exit code = (never exited)\n\t\tstate = active\n"
    restarting = "\tstate = spawn scheduled\n\truns = 1\n\tlast exit code = 1\n\t\tstate = active\n"
    assert inst.launchd_holding(running) is True
    assert inst.launchd_holding(restarting) is False


def test_channel_plan_distinguishes_the_local_door_from_a_remote_hub(monkeypatch):
    monkeypatch.delenv("ROGMEM_HUB", raising=False)
    monkeypatch.setattr(
        inst,
        "probe_origin",
        lambda origin, cookie=None, timeout=5: {"ok": True, "refused": "", "location": "", "hub": ""},
    )
    assert inst.plan_channel("http://127.0.0.1:10002")["mode"] == "port"
    assert inst.plan_channel("http://127.0.0.1:10002")["target"] == 10002
    assert inst.plan_channel("http://127.0.0.1:18780")["mode"] == "ready"
    assert inst.plan_channel("https://hub.example")["mode"] == "url"


def test_a_redirect_is_a_login_wall_not_a_closed_tunnel():
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "https://www.example.test/")
            self.end_headers()

        def log_message(self, fmt, *args):
            return

    server, origin = _serve(Redirect)
    try:
        plan = inst.plan_channel(origin)
    finally:
        server.shutdown()
    assert plan["ok"] is False
    assert plan["refused"] == "login-wall"


def test_remote_origin_is_proxied_with_the_browser_session(monkeypatch):
    seen = {}

    class Hub(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen["cookie"] = self.headers.get("Cookie", "")
            seen["host"] = self.headers.get("Host", "")
            body = b"rogmem" if self.path.startswith("/healthz") else b"hub-ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server, origin = _serve(Hub)
    monkeypatch.setenv("ROGMEM_HUB_COOKIE", "zsession=open")
    listen = ThreadingHTTPServer(("127.0.0.1", 0), Hub)
    door = listen.server_address[1]
    listen.server_close()
    proxy = inst.forward_origin(door, origin)
    try:
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{door}/healthz", timeout=3) as resp:
            assert b"rogmem" in resp.read()
        with urllib.request.urlopen(f"http://127.0.0.1:{door}/mcp", timeout=3) as resp:
            assert resp.read() == b"hub-ok"
    finally:
        proxy.shutdown()
        server.shutdown()
    assert seen["cookie"] == "zsession=open"
    assert seen["host"].startswith("127.0.0.1:")


def test_a_silent_cached_hop_does_not_hide_the_entrance():
    """A shortcut that accepts and never speaks is not a door. The entrance still answers."""

    class Silent(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            import time

            time.sleep(30)

        def log_message(self, fmt, *args):
            return

    class Hub(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b"live"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    silent, silent_origin = _serve(Silent)
    server, origin = _serve(Hub)
    listen = ThreadingHTTPServer(("127.0.0.1", 0), Hub)
    door = listen.server_address[1]
    listen.server_close()
    proxy = inst.forward_origin(door, origin, hub="rh_live")
    proxy.RequestHandlerClass.cached_origin = silent_origin
    try:
        import time
        import urllib.request

        started = time.perf_counter()
        with urllib.request.urlopen(f"http://127.0.0.1:{door}/healthz", timeout=5) as resp:
            assert resp.read() == b"live"
        assert time.perf_counter() - started < 5
        assert proxy.RequestHandlerClass.cached_origin == ""
    finally:
        proxy.shutdown()
        server.shutdown()
        silent.shutdown()


def test_bootstrap_installs_into_a_fresh_interpreter(tmp_path):
    wheel = bundle_bytes()

    class Wheel(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = wheel if self.path.startswith("/host-client.whl") else b""
            self.send_response(200 if body else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server, origin = _serve(Wheel)
    venv = tmp_path / "venv"
    try:
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        python = venv / "bin" / "python"
        bootstrap = ROOT / "rogmem" / "host_bootstrap.py"
        result = subprocess.run(
            [str(python), str(bootstrap), origin],
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        server.shutdown()
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["ok"] is True
    imported = subprocess.run(
        [str(python), "-c", "import rogmem.cli, installer.rogmem_installer as inst; print(inst._hook_source().is_file())"],
        capture_output=True,
        text=True,
    )
    assert imported.returncode == 0, imported.stderr
    assert imported.stdout.strip() == "True"


def test_host_client_routes_and_prompt_carry_the_page_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine
    from rogmem.app import build_app

    engine.connect(tmp_path, tmp_path / "scan")
    client = TestClient(build_app())
    wheel = client.get("/host-client.whl")
    assert wheel.status_code == 200
    assert wheel.content.startswith(b"PK")
    assert client.get("/host-client").content == wheel.content
    bootstrap = client.get("/host-bootstrap")
    assert bootstrap.status_code == 200
    assert "def main" in bootstrap.text
    page = client.get("/admin/hosts").text
    assert ORIGIN_MARK in page
    assert "location.origin" in page
    assert "ROGMEM_HUB" in page
    assert "___ROGMEM_HUB___" not in page
    assert "rh_" in page
    guide = onboard_prompt("rg_test")
    cold = (ROOT / "installer" / "COLD_START.md").read_text(encoding="utf-8")
    named = inst.connect_prompt("Cursor")
    for text in (guide, named, cold):
        assert "/host-bootstrap" in text
        assert "github.com" not in text
        assert "ROGMEM_HUB_COOKIE" in text
        assert "入口会变，身份不变" in text
        assert "不要换地址" not in text
        for line in host_client_instruction().splitlines():
            assert line.strip() in text
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    bake = dockerfile.index("bake_bundle")
    remove = dockerfile.index("rm -rf installer host")
    assert 0 < bake < remove < bake + 180
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "/host-bootstrap" in readme
    assert "不克隆 GitHub" in readme
    assert "入口会变" in readme or "入口以后变了" in readme


def _hub_server(hub: str, mark: str = "") -> tuple[ThreadingHTTPServer, str]:
    payload = json.dumps({"service": "rogmem", "hub": hub, "mark": mark}, ensure_ascii=False).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            return

    return _serve(Handler)


def _port_of(origin: str) -> int:
    return int(origin.rsplit(":", 1)[1])


def _clear_proxy(monkeypatch) -> None:
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")


def test_proc_and_netstat_tables_keep_only_local_listeners():
    proc = "\n".join(
        [
            "sl local_address rem_address st",
            "0: 0100007F:495C 00000000:0000 0A 0",
            "1: 00000000:0050 00000000:0000 0A 0",
            "2: 08080808:01BB 00000000:0000 0A 0",
            "3: 0100007F:0050 00000000:0000 01 0",
            "4: 00000000000000000000000001000000:0009 00000000000000000000000000000000:0000 0A 0",
        ]
    )
    ports = inst._ports_from_proc(proc)
    assert {18780, 80, 9} <= ports
    assert 443 not in ports
    table = "\n".join(
        [
            "tcp        0      0 127.0.0.1:10010       0.0.0.0:*               LISTEN",
            "tcp4       0      0  127.0.0.1.10011      *.*                     LISTEN",
            "tcp6       0      0  ::1.10012            *.*                     LISTEN",
            "TCP    0.0.0.0:10013    0.0.0.0:0    LISTENING",
            "TCP    [::1]:10016    [::]:0    LISTENING",
            "TCP    192.0.2.8:10014   0.0.0.0:0    LISTENING",
            "tcp        0      0 10.1.2.3:10015        0.0.0.0:*               LISTEN",
        ]
    )
    heard = inst._ports_from_listen_table(table)
    assert {10010, 10011, 10012, 10013, 10016} <= heard
    assert 10014 not in heard and 10015 not in heard


def test_finding_a_hub_waits_for_a_slow_entrance(monkeypatch):
    import time

    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            time.sleep(0.8)
            body = b'{"service":"rogmem","hub":"rh_slow"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server, origin = _serve(Slow)
    monkeypatch.setattr(inst, "listening_ports", lambda: [_port_of(origin)])
    try:
        assert inst.find_binding("rh_slow") == origin
    finally:
        server.shutdown()


def test_listening_ports_sees_a_loopback_socket():
    server, origin = _hub_server("rh_listen")
    try:
        assert _port_of(origin) in inst.listening_ports()
    finally:
        server.shutdown()


def test_loopback_health_is_not_sent_through_an_http_proxy(monkeypatch):
    server, origin = _hub_server("rh_direct")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    try:
        probed = inst.probe_origin(origin)
        opened = inst._channel_open(origin + "/mcp")
        with inst.open_direct(origin + "/healthz", 3) as resp:
            body = resp.read()
    finally:
        server.shutdown()
    assert probed["ok"] is True
    assert probed["hub"] == "rh_direct"
    assert opened is True
    assert b"rh_direct" in body


def test_a_closed_entrance_follows_the_same_hub_and_not_another(monkeypatch):
    monkeypatch.delenv("ROGMEM_HUB", raising=False)
    same, same_origin = _hub_server("rh_same", "same")
    other, other_origin = _hub_server("rh_other", "other")
    try:
        monkeypatch.setattr(inst, "listening_ports", lambda: [_port_of(same_origin), _port_of(other_origin)])
        followed = inst.plan_channel("http://127.0.0.1:1", "rh_same")
        refused = inst.plan_channel(other_origin, "rh_same")
        unnamed = inst.plan_channel("http://127.0.0.1:1")
    finally:
        same.shutdown()
        other.shutdown()
    assert followed["ok"] is True
    assert followed["mode"] == "port"
    assert followed["target"] == _port_of(same_origin)
    assert followed["hub"] == "rh_same"
    assert refused["ok"] is True
    assert refused["target"] == _port_of(same_origin)
    assert unnamed["ok"] is False
    assert unnamed["refused"] == "channel-closed"


def test_the_local_door_is_used_when_it_is_already_this_hub(monkeypatch):
    monkeypatch.delenv("ROGMEM_HUB", raising=False)
    server, origin = _hub_server("rh_door")
    monkeypatch.setattr(inst, "FIXED_FORWARD_PORT", _port_of(origin))
    monkeypatch.setattr(inst, "listening_ports", lambda: [_port_of(origin)])
    try:
        plan = inst.plan_channel("http://127.0.0.1:1", "rh_door")
    finally:
        server.shutdown()
    assert plan["ok"] is True
    assert plan["mode"] == "ready"
    assert plan["hub"] == "rh_door"


def test_a_closed_binding_does_not_hide_the_entrance(monkeypatch):
    """A binding the door followed is not the entrance. When it closes, the next dial uses the entrance this process was given."""
    import threading
    import urllib.request

    _clear_proxy(monkeypatch)
    held = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    entrance_port = held.server_address[1]
    held.server_close()
    stray_held = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    stray_port = stray_held.server_address[1]
    stray_held.server_close()
    door_held = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    door = door_held.server_address[1]
    door_held.server_close()
    visible: list[int] = []
    monkeypatch.setattr(inst, "listening_ports", lambda: list(visible))

    def marked(port: int, mark: str) -> ThreadingHTTPServer:
        payload = json.dumps({"service": "rogmem", "hub": "rh_keep", "mark": mark}).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt, *args):
                return

        class Bound(ThreadingHTTPServer):
            allow_reuse_address = True

        server = Bound(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    entrance = marked(entrance_port, "kept")
    origin = f"http://127.0.0.1:{entrance_port}"
    proxy = inst.forward_origin(door, origin, hub="rh_keep")
    stray = None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{door}/healthz", timeout=5) as resp:
            assert b'"mark": "kept"' in resp.read()
        entrance.shutdown()
        entrance.server_close()
        stray = marked(stray_port, "moved")
        visible.append(stray_port)
        with urllib.request.urlopen(f"http://127.0.0.1:{door}/healthz", timeout=5) as resp:
            assert b'"mark": "moved"' in resp.read()
        stray.shutdown()
        stray.server_close()
        stray = None
        visible.clear()
        entrance = marked(entrance_port, "kept")
        with urllib.request.urlopen(f"http://127.0.0.1:{door}/healthz", timeout=5) as resp:
            body = resp.read()
    finally:
        proxy.shutdown()
        entrance.shutdown()
        entrance.server_close()
        if stray is not None:
            stray.shutdown()
            stray.server_close()
    assert b'"mark": "kept"' in body


def test_the_door_follows_the_hub_when_its_entrance_moves(monkeypatch):
    import urllib.request

    _clear_proxy(monkeypatch)
    held = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    dead = held.server_address[1]
    held.server_close()
    second, second_origin = _hub_server("rh_move", "now")
    stranger, stranger_origin = _hub_server("rh_else", "else")
    monkeypatch.setattr(
        inst,
        "listening_ports",
        lambda: [_port_of(second_origin), _port_of(stranger_origin)],
    )
    proxy = inst.forward(0, dead, hub="rh_move")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{proxy.server_address[1]}/healthz", timeout=5) as resp:
            body = resp.read()
    finally:
        proxy.shutdown()
        second.shutdown()
        stranger.shutdown()
    assert b'"mark": "now"' in body
    assert b'"mark": "else"' not in body


def test_bootstrap_uses_the_door_of_this_hub_when_the_entrance_is_gone(monkeypatch):
    from rogmem import host_bootstrap as bootstrap

    wheel = bundle_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/healthz"):
                body = b'{"service":"rogmem","hub":"rh_boot"}'
            elif self.path.startswith("/host-client.whl"):
                body = wheel
            else:
                body = b""
            self.send_response(200 if body else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server, origin = _serve(Handler)
    monkeypatch.setattr(bootstrap, "DOOR", origin)
    monkeypatch.setenv("ROGMEM_HUB", "rh_boot")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    try:
        assert bootstrap.fetch_client("http://127.0.0.1:1") == wheel
    finally:
        server.shutdown()


def test_bootstrap_does_not_install_from_another_hub(monkeypatch):
    import urllib.error

    from rogmem import host_bootstrap as bootstrap

    server, origin = _hub_server("rh_other")
    monkeypatch.setattr(bootstrap, "DOOR", origin)
    monkeypatch.setenv("ROGMEM_HUB", "rh_boot")
    try:
        try:
            bootstrap.fetch_client("http://127.0.0.1:1")
        except urllib.error.URLError:
            return
        raise AssertionError("another hub was accepted")
    finally:
        server.shutdown()


def test_health_names_one_hub_and_keeps_it(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    first = engine.health()["hub"]
    assert first.startswith("rh_") and len(first) == 35
    assert engine.health()["hub"] == first
