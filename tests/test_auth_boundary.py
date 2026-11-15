"""Trust boundary: admin password, signed cookie, device bind, and admin writes."""

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from rogmem import auth, crypto, engine


def test_login_follows_the_current_data_directory(tmp_path):
    """A thread that already opened another database must not keep that password."""
    import os

    previous = os.environ.get("ROGMEM_NO_PASSWORD")
    os.environ.pop("ROGMEM_NO_PASSWORD", None)
    try:
        first = tmp_path / "first"
        engine.connect(first, first / "scan")
        opened = threading.Event()
        release = threading.Event()
        result: dict[str, object] = {}

        def worker():
            engine.db().execute("SELECT 1")
            opened.set()
            release.wait(5)
            data = engine._DATA
            assert data is not None
            password = (data / "admin.password").read_text(encoding="utf-8").strip()
            result["ok"] = engine.admin_login(password)

        thread = threading.Thread(target=worker)
        thread.start()
        assert opened.wait(5)
        second = tmp_path / "second"
        engine.connect(second, second / "scan")
        release.set()
        thread.join(5)
        assert result["ok"] is True
    finally:
        if previous is None:
            os.environ.pop("ROGMEM_NO_PASSWORD", None)
        else:
            os.environ["ROGMEM_NO_PASSWORD"] = previous


def test_password_roundtrip_and_reject():
    encoded = auth.hash_password("correct horse")
    assert auth.verify_password("correct horse", encoded)
    assert not auth.verify_password("wrong", encoded)
    assert not auth.verify_password("correct horse", "not-a-hash")
    assert not auth.verify_password("", encoded)


def test_cookie_mac_binds_secret_and_expires():
    token = auth.sign_cookie(b"secret-a", "ok", now=1_000)
    assert auth.verify_cookie(b"secret-a", token, now=1_000 + 10) == "ok"
    assert auth.verify_cookie(b"secret-b", token, now=1_000 + 10) is None
    assert auth.verify_cookie(b"secret-a", "ok.deadbeef", now=1_000) is None
    assert auth.verify_cookie(b"secret-a", token, now=1_000 + 86400 * 14 + 1) is None
    forged = auth.sign_cookie(b"secret-a", "nope", now=1_000)
    assert auth.verify_cookie(b"secret-a", forged, now=1_010) is None


def _open_data(tmp_path: Path, monkeypatch, *, bypass: bool) -> None:
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    if bypass:
        monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    else:
        monkeypatch.delenv("ROGMEM_NO_PASSWORD", raising=False)
    engine.connect(tmp_path, tmp_path / "scan")


def test_ensure_password_writes_file_and_checks_it(tmp_path, monkeypatch):
    _open_data(tmp_path, monkeypatch, bypass=False)
    path = tmp_path / "admin.password"
    raw = path.read_text(encoding="utf-8").strip()
    assert raw == "Rogmem@2026"
    assert "\n" not in raw
    assert (path.stat().st_mode & 0o777) == 0o600
    assert engine.password_ready() is True
    assert engine.admin_login(raw) is True
    assert engine.admin_login("wrong-password") is False
    assert engine.get_setting("admin_password") != raw
    assert engine.get_setting("admin_password").startswith("scrypt$")


def test_temp_bypass_does_not_mint_a_password(tmp_path, monkeypatch):
    _open_data(tmp_path, monkeypatch, bypass=True)
    assert not (tmp_path / "admin.password").exists()
    assert engine.password_ready() is False
    assert engine.admin_login("anything") is True


def test_bypass_flag_ignored_outside_temp(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setattr(auth, "allow_password_bypass", lambda *_args, **_kwargs: False)
    engine.connect(tmp_path, tmp_path / "scan")
    assert (tmp_path / "admin.password").is_file()
    assert engine.password_ready() is True
    assert engine.admin_login("anything") is False


def test_reveal_is_once_and_change_drops_plaintext(tmp_path, monkeypatch):
    _open_data(tmp_path, monkeypatch, bypass=False)
    raw = (tmp_path / "admin.password").read_text(encoding="utf-8").strip()
    assert engine.take_password_reveal() == raw
    assert engine.take_password_reveal() == ""
    changed = engine.change_password(raw, "FreshPass1!")
    assert changed["ok"] is True
    assert not (tmp_path / "admin.password").exists()
    assert engine.admin_login(raw) is False
    assert engine.admin_login("FreshPass1!") is True
    refused = engine.change_password("FreshPass1!", "short")
    assert refused["ok"] is False
    assert "至少 8 位" in refused["error"]
    assert "大写" in refused["error"]
    assert "数字" in refused["error"]
    assert "符号" in refused["error"]
    weak = engine.change_password("FreshPass1!", "lowercase1!")
    assert weak["ok"] is False
    assert "大写" in weak["error"]
    assert "小写" not in weak["error"].split("还缺", 1)[-1].split("。")[0]
    no_digit = engine.change_password("FreshPass1!", "NoDigit!!")
    assert no_digit["ok"] is False and "数字" in no_digit["error"]
    no_symbol = engine.change_password("FreshPass1!", "NoSymbol1")
    assert no_symbol["ok"] is False and "符号" in no_symbol["error"]
    no_lower = engine.change_password("FreshPass1!", "NOLOWER1!")
    assert no_lower["ok"] is False and "小写" in no_lower["error"]
    mismatch = engine.change_password("FreshPass1!", "OtherPass1!", "OtherPass2!")
    assert mismatch["ok"] is False and "不一致" in mismatch["error"]
    assert engine.admin_login("FreshPass1!") is True


def test_agent_by_token_requires_device(tmp_path, monkeypatch):
    _open_data(tmp_path, monkeypatch, bypass=True)
    token = "rm_device_test"
    digest = __import__("hashlib").sha256(token.encode()).hexdigest()
    engine.db().execute(
        """INSERT INTO agents(id,device_id,device_name,program,display_name,status,pubkey,key_hash,created_at)
           VALUES('agent-1','dev-a','电脑A','Cursor','电脑A · Cursor','启用','cHVi','"""
        + digest
        + """', '2026-10-25T00:00:00+00:00')"""
    )
    engine.db().commit()
    assert engine.agent_by_token(token, None) is None
    assert engine.agent_by_token(token, "") is None
    assert engine.agent_by_token(token, "   ") is None
    assert engine.agent_by_token(token, "dev-b") is None
    row = engine.agent_by_token(token, "dev-a")
    assert row is not None and row["id"] == "agent-1"


def test_agent_by_token_still_opens_when_last_used_is_locked(tmp_path, monkeypatch):
    """A busy distill write must not turn a valid key into a broken door."""
    import sqlite3

    _open_data(tmp_path, monkeypatch, bypass=True)
    token = "rm_lock_test"
    digest = __import__("hashlib").sha256(token.encode()).hexdigest()
    engine.db().execute(
        """INSERT INTO agents(id,device_id,device_name,program,display_name,status,pubkey,key_hash,created_at,last_used)
           VALUES('agent-lock','dev-a','电脑A','Cursor','电脑A · Cursor','启用','cHVi',?,?,?)""",
        (digest, "2026-10-25T00:00:00+00:00", "2026-10-25T00:00:00+00:00"),
    )
    engine.db().commit()
    before = engine.db().execute("SELECT last_used FROM agents WHERE id='agent-lock'").fetchone()["last_used"]
    real = engine.db()

    class BusyDoor:
        def execute(self, sql, params=()):
            if isinstance(sql, str) and sql.startswith("UPDATE agents SET last_used="):
                raise sqlite3.OperationalError("database is locked")
            return real.execute(sql, params)

        def commit(self):
            return real.commit()

        def rollback(self):
            return real.rollback()

    monkeypatch.setattr(engine, "db", lambda: BusyDoor())
    row = engine.agent_by_token(token, "dev-a")
    assert row is not None and row["id"] == "agent-lock"
    after = real.execute("SELECT last_used FROM agents WHERE id='agent-lock'").fetchone()["last_used"]
    assert after == before


def _login_panel(page: str) -> str:
    """登录窗本体。页首的品牌 logo 不算在内。"""
    return page.split('<main class="login-panel"', 1)[1].split("</main>", 1)[0]


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Hub:
    def __init__(self, tmp_path: Path, monkeypatch, *, enroll_quota: str = "10/60", admin_quota: str = "60/60"):
        self.data = tmp_path / "data"
        self.scan = tmp_path / "scan"
        monkeypatch.setenv("ROGMEM_DATA", str(self.data))
        monkeypatch.setenv("ROGMEM_SCAN", str(self.scan))
        monkeypatch.setenv("ROGMEM_EMBED", "off")
        monkeypatch.setenv("ROGMEM_POLL", "3600")
        monkeypatch.delenv("ROGMEM_NO_PASSWORD", raising=False)
        monkeypatch.setenv("ROGMEM_RATE_ENROLL", enroll_quota)
        monkeypatch.setenv("ROGMEM_RATE_ADMIN", admin_quota)
        engine.connect(self.data, self.scan)
        # Mint the hub id on this thread. Concurrent /healthz calls otherwise
        # deadlock on the first insert.
        engine.hub_id()
        self.password = (self.data / "admin.password").read_text(encoding="utf-8").strip()
        from rogmem.app import build_app, mcp
        import uvicorn

        mcp._session_manager = None

        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(build_app(), host="127.0.0.1", port=self.port, log_level="error", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        try:
            deadline = time.time() + 8
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(self.base + "/healthz", timeout=0.3) as resp:
                        if b"rogmem" in resp.read():
                            break
                except Exception:
                    time.sleep(0.05)
            else:
                raise RuntimeError("hub did not start")
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=3)

    def req(self, method: str, path: str, body=None, headers=None, raw: bytes | None = None):
        data = raw
        headers = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=5) as resp:
                return resp.status, resp.read().decode(), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(), exc.headers

    def login(self, password: str | None = None) -> str:
        status, _body, headers = self.req("POST", "/admin/login", {"password": self.password if password is None else password})
        assert status == 200, _body
        return headers.get("Set-Cookie", "").split(";", 1)[0]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def test_first_visit_opens_without_a_password(tmp_path, monkeypatch):
    hub = Hub(tmp_path, monkeypatch)
    try:
        from rogmem.ui import login_page

        assert engine.passwordless_enabled() is True
        assert engine.user_password_set() is False
        assert engine.admin_password_required() is False
        status, body, headers = hub.req("GET", "/")
        assert status == 200, body[:200]
        assert "<h1>记忆图谱</h1>" in body
        assert "/admin/login" not in headers.get("Location", "")
        login, _page, login_headers = hub.req("GET", "/admin/login")
        assert login == 302
        assert "/admin/graph" in login_headers.get("Location", "")
        opened = hub.req("POST", "/admin/api/settings/webhook", {"url": "https://1.1.1.1/hook"})
        assert opened[0] == 200, opened[1]
        page = login_page()
        panel = _login_panel(page)
        assert panel.count("初始密码") == 1
        assert "初始密码：Rogmem@2026" in panel
        assert "帮助" not in page
        assert "复制给 Agent" not in page
        health, health_body, _headers = hub.req("GET", "/healthz")
        assert health == 200
        assert "rogmem" in health_body
        bootstrap, bootstrap_body, _headers = hub.req("GET", "/host-bootstrap")
        assert bootstrap == 200
        assert "def main" in bootstrap_body
        mcp_status, mcp_body, _headers = hub.req("POST", "/mcp", {})
        assert mcp_status == 401
        assert "未授权" in mcp_body
        wheel_request = urllib.request.Request(hub.base + "/host-client.whl")
        try:
            with urllib.request.urlopen(wheel_request, timeout=5) as wheel_resp:
                wheel_status = wheel_resp.status
        except urllib.error.HTTPError as exc:
            wheel_status = exc.code
        assert wheel_status != 401
    finally:
        hub.close()


def test_login_page_keeps_only_the_password_panel(tmp_path, monkeypatch):
    hub = Hub(tmp_path, monkeypatch)
    try:
        closed = hub.req("POST", "/admin/api/settings/basic", {"passwordless": "0"})
        assert closed[0] == 200, closed[1]
        login, page, login_headers = hub.req("GET", "/admin/login")
        assert login == 200
        assert "帮助" not in page
        assert "help-dialog" not in page
        assert "/admin/static/login/" not in page
        panel = _login_panel(page)
        assert panel.count("初始密码") == 1
        assert "初始密码：Rogmem@2026" in panel
        assert "ROGMEM 私人记忆中枢" in panel
        assert "立即登录" in panel
        assert "登录密码" in panel
        assert 'placeholder="输入访问密码"' in panel
        assert 'id="password-input"' in panel
        assert "color: var(--text-muted)" in page
        assert 'class="login-panel"' in page
        assert 'id="neural-network-canvas"' in page
        assert 'id="nebula"' not in page
        assert "cosmos-graph.min.js" not in page
        assert "spaceDimensions" not in page
        assert 'aria-label="ROGMEM"' in page
        assert 'type="password"' in page
        assert "/admin/graph" not in login_headers.get("Location", "")
        missing, _missing_body, _missing_headers = hub.req("GET", "/admin/static/login/step-enter.png")
        assert missing != 200
        script, _script_body, script_headers = hub.req("GET", "/admin/static/cosmos-graph.min.js")
        assert script == 200
        assert "javascript" in script_headers.get("Content-Type", "")
        module, module_body, module_headers = hub.req("GET", "/admin/static/three/build/three.module.js")
        assert module == 200
        assert "javascript" in module_headers.get("Content-Type", "")
        assert "REVISION" in module_body
        addon, _addon_body, addon_headers = hub.req("GET", "/admin/static/three/examples/jsm/controls/OrbitControls.js")
        assert addon == 200
        assert "javascript" in addon_headers.get("Content-Type", "")
        assert hub.req("GET", "/admin/static/three/nope.js")[0] == 404
    finally:
        hub.close()


def test_login_assets_open_without_a_session():
    """登录页画 3D 视效要用到 three.js，但只放行 vendor/three 里的文件。"""
    from rogmem import app as server

    assert server._login_asset("/admin/static/three/build/three.module.js") is True
    assert server._login_asset("/admin/static/three/examples/jsm/controls/OrbitControls.js") is True
    assert server._login_asset("/admin/static/three/") is False
    assert server._login_asset("/admin/static/three/../vendor/cosmos-graph.min.js") is False
    assert server._login_asset("/admin/static/login/../ui.py") is False
    assert server._login_asset("/admin/api/settings/basic") is False


def test_admin_writes_require_the_password_session(tmp_path, monkeypatch):
    hub = Hub(tmp_path, monkeypatch)
    try:
        opened = hub.req("POST", "/admin/api/settings/basic", {"passwordless": "0"})
        assert opened[0] == 200, opened[1]
        assert engine.admin_password_required() is True
        _secret, public = crypto.new_keypair()
        applied = json.loads(
            hub.req(
                "POST",
                "/enroll/apply",
                {"device_id": "dev-x", "device_name": "qa", "program": "Cursor", "pubkey": crypto.b64encode(public)},
            )[1]
        )
        assert applied["ok"] is True
        aid = applied["id"]
        writes = [
            ("POST", f"/admin/api/agents/{aid}/approve", {}),
            ("POST", f"/admin/api/agents/{aid}/disable", {}),
            ("POST", f"/admin/api/agents/{aid}/reject", {}),
            ("POST", f"/admin/api/agents/{aid}/delete", {}),
            ("POST", "/admin/api/settings/webhook", {"url": "https://1.1.1.1/hook"}),
            ("POST", "/admin/api/settings/collect", {"scan_interval": "1", "turn_wait": "1", "file_types": "txt", "sweep_at": "03:00"}),
            ("POST", "/admin/api/hosts/report", {"hosts": []}),
            ("POST", "/admin/api/history", {"turns": [], "program": "Cursor"}),
            ("POST", "/admin/api/sweep", {}),
            ("POST", "/admin/api/password", {"current": hub.password, "password": "another-password"}),
            ("POST", "/admin/api/enroll-grants", {}),
        ]
        for method, path, body in writes:
            status, payload, _headers = hub.req(method, path, body)
            assert status == 401, (path, status, payload)
        boundary = "----rogmemtest"
        raw = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="note.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            "should-not-import\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        status, payload, _headers = hub.req(
            "POST",
            "/admin/api/sources/import",
            raw=raw,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        assert status == 401, payload
        page, _body, headers = hub.req("GET", "/admin/crystals")
        assert page == 302
        assert "/admin/login" in headers.get("Location", "")
        graph, gbody, _headers = hub.req("GET", "/admin/api/graph")
        assert graph == 401
        assert "nodes" not in gbody
        forged = hub.req("GET", "/admin/api/graph", headers={"Cookie": "rogmem_admin=ok.1.deadbeef"})
        assert forged[0] == 401
        cookie = hub.login()
        saved = hub.req("POST", "/admin/api/settings/webhook", {"url": "https://1.1.1.1/hook"}, {"Cookie": cookie})
        assert saved[0] == 200, saved[1]
        home, body, home_headers = hub.req("GET", "/", headers={"Cookie": cookie})
        assert home == 200
        assert "<h1>记忆图谱</h1>" in body
        assert "rogmem_channel=1" in home_headers.get("Set-Cookie", "")
    finally:
        hub.close()


def test_passwordless_defaults_open_and_can_close(tmp_path, monkeypatch):
    hub = Hub(tmp_path, monkeypatch)
    try:
        from rogmem.ui import basic_page

        assert engine.passwordless_enabled() is True
        html = basic_page(engine.basic_config())
        assert "<h2>登录密码设置</h2>" in html
        assert "初始密码: Rogmem@2026" in html
        assert 'id="password-gap"' in html
        assert "至少 8 位" in html
        assert "两次输入不一致" in html
        assert html.count('class="peek"') == 2
        module = html.split('id="login-password"', 1)[1].split("主人称呼", 1)[0]
        before_fields = module.split('class="login-password-fields"', 1)[0]
        assert 'name="current_password"' in before_fields
        assert 'class="peek"' not in before_fields
        assert "login-password-switch" in module
        assert "password-save-line" in module
        assert "setting-foot" not in module
        assert "dashed" not in module
        assert "login-password-split" not in module
        assert 'aria-label="显示新密码"' in html
        assert 'aria-label="显示确认新密码"' in html
        assert 'aria-label="当前密码"' in html
        assert 'aria-label="新密码"' in html
        assert 'aria-label="确认新密码"' in html
        assert "<h2>修改口令</h2>" not in html
        assert "<h2>免密登录</h2>" not in html
        tag = html.split('name="passwordless"', 1)[1].split(">", 1)[0]
        assert "checked" in tag
        home, body, _headers = hub.req("GET", "/")
        assert home == 200
        assert "<h1>记忆图谱</h1>" in body
        closed = hub.req("POST", "/admin/api/settings/basic", {"passwordless": "0"})
        assert closed[0] == 200, closed[1]
        assert engine.passwordless_enabled() is False
        again, _again_body, _headers = hub.req("POST", "/admin/api/settings/webhook", {"url": "https://1.1.1.1/hook"})
        assert again == 401
        locked, _locked_body, headers = hub.req("GET", "/")
        assert locked == 302
        assert "/admin/login" in headers.get("Location", "")
        reopened = hub.req(
            "POST",
            "/admin/api/settings/basic",
            {"passwordless": "1"},
            {"Cookie": hub.login()},
        )
        assert reopened[0] == 200, reopened[1]
        assert engine.passwordless_enabled() is True
        assert engine.admin_password_required() is False
        restored, _restored_body, _headers = hub.req("GET", "/")
        assert restored == 200
        health, _health_body, _headers = hub.req("GET", "/healthz")
        assert health == 200
        bootstrap, bootstrap_body, _headers = hub.req("GET", "/host-bootstrap")
        assert bootstrap == 200
        assert "def main" in bootstrap_body
        mcp_status, mcp_body, _headers = hub.req("POST", "/mcp", {})
        assert mcp_status == 401
        assert "未授权" in mcp_body
        refused = engine.save_basic({"passwordless": "maybe"})
        assert refused["ok"] is False
    finally:
        hub.close()


def test_setting_a_password_starts_the_check(tmp_path, monkeypatch):
    hub = Hub(tmp_path, monkeypatch)
    try:
        assert engine.admin_password_required() is False
        changed = json.loads(
            hub.req(
                "POST",
                "/admin/api/password",
                {"current": "Rogmem@2026", "password": "FreshPass1!", "confirm": "FreshPass1!"},
            )[1]
        )
        assert changed["ok"] is True, changed
        assert engine.user_password_set() is True
        assert engine.passwordless_enabled() is True
        assert engine.admin_password_required() is True
        assert engine.admin_login("FreshPass1!") is True
        denied, _body, _headers = hub.req("POST", "/admin/api/settings/webhook", {"url": "https://1.1.1.1/hook"})
        assert denied == 401
        cookie = hub.login("FreshPass1!")
        saved = hub.req(
            "POST",
            "/admin/api/settings/webhook",
            {"url": "https://1.1.1.1/hook"},
            {"Cookie": cookie},
        )
        assert saved[0] == 200, saved[1]
        still = hub.req("POST", "/admin/api/settings/basic", {"passwordless": "1"}, {"Cookie": cookie})
        assert still[0] == 200, still[1]
        assert engine.admin_password_required() is True
        again, _again_body, _headers = hub.req("GET", "/")
        assert again == 302
    finally:
        hub.close()


def test_turning_passwordless_off_retires_existing_sessions(tmp_path, monkeypatch):
    hub = Hub(tmp_path, monkeypatch)
    try:
        assert engine.admin_login("Rogmem@2026") is True
        cookie = hub.login("Rogmem@2026")
        home, body, _headers = hub.req("GET", "/", headers={"Cookie": cookie})
        assert home == 200
        assert "<h1>记忆图谱</h1>" in body
        mismatched = json.loads(
            hub.req(
                "POST",
                "/admin/api/password",
                {"current": "Rogmem@2026", "password": "FreshPass1!", "confirm": "different-pass"},
                {"Cookie": cookie},
            )[1]
        )
        assert mismatched["ok"] is False
        assert engine.admin_login("Rogmem@2026") is True
        turned = hub.req("POST", "/admin/api/settings/basic", {"passwordless": "0"}, {"Cookie": cookie})
        assert turned[0] == 200, turned[1]
        assert engine.passwordless_enabled() is False
        assert engine.admin_password_required() is True
        again, _again_body, headers = hub.req("GET", "/", headers={"Cookie": cookie})
        assert again == 302
        assert "/admin/login" in headers.get("Location", "")
        page, _page_body, page_headers = hub.req("GET", "/admin/settings/basic", headers={"Cookie": cookie})
        assert page == 302
        assert "/admin/login" in page_headers.get("Location", "")
        graph, graph_body, _headers = hub.req("GET", "/admin/api/graph", headers={"Cookie": cookie})
        assert graph == 401
        assert "nodes" not in graph_body
        bare, _bare_body, bare_headers = hub.req("GET", "/")
        assert bare == 302
        assert "/admin/login" in bare_headers.get("Location", "")
        fresh = hub.login("Rogmem@2026")
        assert fresh != cookie
        back, back_body, _headers = hub.req("GET", "/", headers={"Cookie": fresh})
        assert back == 200
        assert "<h1>记忆图谱</h1>" in back_body
    finally:
        hub.close()


def test_login_then_approve_disable_and_password_change(tmp_path, monkeypatch):
    hub = Hub(tmp_path, monkeypatch)
    try:
        bad, _body, _headers = hub.req("POST", "/admin/login", {"password": "nope"})
        assert bad == 401
        cookie = hub.login()
        secret, public = crypto.new_keypair()
        applied = json.loads(
            hub.req(
                "POST",
                "/enroll/apply",
                {"device_id": "dev-ok", "device_name": "qa", "program": "Cursor", "pubkey": crypto.b64encode(public)},
            )[1]
        )
        approved = json.loads(hub.req("POST", f"/admin/api/agents/{applied['id']}/approve", {}, {"Cookie": cookie})[1])
        assert approved.get("status") == "待领取"
        claimed = json.loads(hub.req("POST", "/enroll/claim", {"pubkey": crypto.b64encode(public)})[1])
        token = crypto.open_seal(secret, crypto.b64decode(claimed["wrapped"])).decode()
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        }
        missing = hub.req(
            "POST",
            "/mcp",
            init,
            {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"},
        )
        assert missing[0] == 401
        wrong = hub.req(
            "POST",
            "/mcp",
            init,
            {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream", "X-Rogmem-Device": "other-device"},
        )
        assert wrong[0] == 401
        right = hub.req(
            "POST",
            "/mcp",
            init,
            {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream", "X-Rogmem-Device": "dev-ok"},
        )
        assert right[0] == 200, right[1][:300]
        blocked = hub.req(
            "POST",
            "/admin/api/settings/webhook",
            {"url": "http://169.254.169.254/latest/meta-data"},
            {"Cookie": cookie},
        )
        assert blocked[0] == 400, blocked[1]
        assert engine.get_setting("alert_webhook") == ""
        saved = hub.req("POST", "/admin/api/settings/webhook", {"url": "https://1.1.1.1/hook"}, {"Cookie": cookie})
        assert saved[0] == 200, saved[1]
        assert engine.get_setting("alert_webhook") == "https://1.1.1.1/hook"
        changed = json.loads(
            hub.req(
                "POST",
                "/admin/api/password",
                {"current": hub.password, "password": "FreshPass1!", "confirm": "FreshPass1!"},
                {"Cookie": cookie},
            )[1]
        )
        assert changed["ok"] is True
        stopped, body, _headers = hub.req("POST", f"/admin/api/agents/{applied['id']}/disable", {})
        assert stopped == 401, body
        fresh = hub.login("FreshPass1!")
        stopped, body, _headers = hub.req(
            "POST",
            f"/admin/api/agents/{applied['id']}/disable",
            {},
            {"Cookie": fresh},
        )
        assert stopped == 200, body
        after = hub.req(
            "POST",
            "/mcp",
            init,
            {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream", "X-Rogmem-Device": "dev-ok"},
        )
        assert after[0] == 401
    finally:
        hub.close()


def test_cursor_config_writes_device_header(tmp_path):
    from installer.rogmem_installer import write_configs

    home = tmp_path / "home"
    (home / "Library" / "Application Support" / "Cursor").mkdir(parents=True)
    written = write_configs(home, "http://127.0.0.1:9", "rm_exampletoken", ["Cursor"])
    assert written == ["Cursor"]
    saved = json.loads((home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    headers = saved["mcpServers"]["rogmem"]["headers"]
    device = (home / ".rogmem" / "device_id").read_text(encoding="utf-8").strip()
    assert headers["Authorization"] == "Bearer rm_exampletoken"
    assert headers["X-Rogmem-Device"] == device


def test_password_bypass_only_on_temp_data():
    assert auth.allow_password_bypass(Path("/data"), flag="1", tmp_root=Path("/tmp")) is False
    assert auth.allow_password_bypass(Path("/var/lib/rogmem"), flag="1", tmp_root=Path("/tmp")) is False
    assert auth.allow_password_bypass(Path("/tmp/rogmem-live"), flag="1", tmp_root=Path("/tmp")) is True
    assert auth.allow_password_bypass(Path("/tmp/rogmem-live"), flag="0", tmp_root=Path("/tmp")) is False
    assert auth.allow_password_bypass(Path("/tmp/rogmem-live"), flag=None, tmp_root=Path("/tmp")) is False
