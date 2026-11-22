"""打开控制台而不点名某一页时，正门是记忆星图。点名了的地址仍直达。

契约：
- `/` 与 `/index.html`（远程打开）直接是记忆星图，并带上通道标记。
- `/admin/login` GET、`/admin` GET → 302 到 `/admin/graph`。
- `/admin/costs`、`/admin/settings*`、`/admin/crystals/*`、`/admin/sources*` 等深链 200/其自身内容，
  不被改写。
- GET 请求全程不触发服务端写（无任何 settings.* 被改动）。
- 无循环：GET `/admin` → `/admin/graph` → 200（不再 302）。
- 鉴权边界不变：当前 `_admin_ok` 永远放行（匿名可访问），本修改不改这条。
"""

from __future__ import annotations


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    return engine


def _client(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    from starlette.testclient import TestClient
    from rogmem.app import mcp

    return TestClient(mcp.streamable_http_app())


def test_admin_login_get_redirects_to_graph(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/admin/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/graph"


def test_admin_root_redirects_to_graph(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/admin", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/graph"


def test_admin_graph_itself_is_200_no_loop(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/admin/graph", follow_redirects=False)
    assert resp.status_code == 200
    assert "记忆星图" in resp.text


def test_deep_link_cost_preserved(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/admin/costs", follow_redirects=False)
    # /admin/costs 本身是 200；不被劫持改写到 graph。
    assert resp.status_code == 200
    assert "成本明细" in resp.text


def test_deep_link_settings_preserved(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/admin/settings/basic", follow_redirects=False)
    assert resp.status_code == 200
    assert "基础设置" in resp.text


def test_deep_link_crystals_list_preserved(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/admin/crystals", follow_redirects=False)
    # 200（空图谱也会展示空列表）
    assert resp.status_code == 200


def test_deep_link_sources_preserved(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/admin/sources", follow_redirects=False)
    assert resp.status_code == 200


def test_remote_open_is_the_graph(tmp_path, monkeypatch):
    """远程打开的 `/` 就是控制台正门，页面本身是记忆星图，并留下通道标记。"""
    client = _client(tmp_path, monkeypatch)
    for path in ("/", "/index.html"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 200, path
        assert "记忆星图" in resp.text
        assert "<h1>记忆星图</h1>" not in resp.text
        assert "id='graph-count'" in resp.text
        assert "为保障记忆服务通畅，请务必保持本窗口开启或最小化" in resp.text
        assert "rogmem_channel=1" in resp.headers.get("set-cookie", "")


def test_get_entry_does_not_write_settings(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from starlette.testclient import TestClient
    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())

    def snapshot():
        row = engine.db().execute(
            "SELECT k, v FROM settings ORDER BY k"
        ).fetchall()
        return [(r["k"], r["v"]) for r in row]

    before = snapshot()
    for _ in range(5):
        client.get("/", follow_redirects=False)
        client.get("/index.html", follow_redirects=False)
        client.get("/admin/login", follow_redirects=False)
        client.get("/admin", follow_redirects=False)
        client.get("/admin/graph", follow_redirects=False)
    after = snapshot()
    assert before == after, f"GET 默认入口不得写 settings；diff: before={before} after={after}"


def test_admin_login_get_with_query_still_redirects_to_graph(tmp_path, monkeypatch):
    """带 query（如 ?from=xxx）也走同一入口，不劫持转发、不开放跳转。"""
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/admin/login?from=foo", follow_redirects=False)
    assert resp.status_code == 302
    # 不解析 query 做开放跳转；一律进 /admin/graph。
    assert resp.headers["location"] == "/admin/graph"


def test_post_admin_login_returns_json_ok_with_cookie(tmp_path, monkeypatch):
    """POST /admin/login 在 NO_PASSWORD=1 下返回 200 JSON {"ok":true} 并下发 cookie；
    不被改成跳转。这是"默认管理入口"的鉴权契约，不是外部 auth E2E。"""
    import json

    client = _client(tmp_path, monkeypatch)
    resp = client.post("/admin/login", json={"password": "whatever"}, follow_redirects=False)
    assert resp.status_code == 200, f"NO_PASSWORD 下 POST login 应 200，实际 {resp.status_code} body={resp.text}"
    assert "application/json" in resp.headers.get("content-type", "")
    body = json.loads(resp.text)
    assert body.get("ok") is True, f"body.ok 应 True，实际 {body}"
    # cookie 要被下发（Set-Cookie）
    cookie_hdr = resp.headers.get("set-cookie", "")
    assert "=" in cookie_hdr and "HttpOnly" in cookie_hdr, f"必须 Set-Cookie 含 HttpOnly，实际 {cookie_hdr!r}"


def test_default_entry_chain_through_build_app(tmp_path, monkeypatch):
    """走完整 build_app（含 RateGate/AdminGate/BearerGate/ChannelMark 中间件链），
    验证默认入口链在生产 app 下也落到 /admin/graph，而非只在裸 mcp.streamable_http_app 上。"""
    _open(tmp_path, monkeypatch)
    from starlette.testclient import TestClient
    from rogmem.app import build_app

    client = TestClient(build_app())
    r1 = client.get("/admin", follow_redirects=False)
    assert r1.status_code == 302 and r1.headers["location"] == "/admin/graph", (r1.status_code, r1.headers)
    r2 = client.get("/admin/login", follow_redirects=False)
    assert r2.status_code == 302 and r2.headers["location"] == "/admin/graph", (r2.status_code, r2.headers)
    # follow 到 /admin/graph：200 + 含"记忆星图"（沉浸式，无页头 h1）
    r3 = client.get("/admin", follow_redirects=True)
    assert r3.status_code == 200
    assert "记忆星图" in r3.text
    assert "<h1>记忆星图</h1>" not in r3.text
    opened = client.get("/", follow_redirects=False)
    assert opened.status_code == 200
    assert "记忆星图" in opened.text
    assert "<h1>记忆星图</h1>" not in opened.text
    assert "rogmem_channel=1" in opened.headers.get("set-cookie", "")


def test_installer_window_opens_the_graph(tmp_path, monkeypatch):
    """本机窗口打开的也是同一扇正门，不另指到结晶记忆。"""
    from installer import rogmem_installer as inst

    monkeypatch.setattr(inst, "find_rogmem", lambda start, end: 18080)
    monkeypatch.setattr(inst, "forward", lambda *args, **kwargs: object())
    opened = inst.client_launch(18925, 1, 2, show=False, home=tmp_path)
    assert opened["entry"].endswith("/admin/graph")
