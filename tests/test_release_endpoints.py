"""HTTP admin endpoints for the release probe.

- GET  /admin/api/settings/release-view    — pure read, no probe, no write
- POST /admin/api/settings/release-refresh — manual probe; cooldown on success
- POST /admin/api/settings/release-source  — persist and validate the URL
- GET  /admin/settings/about               — about page HTML
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest
from starlette.testclient import TestClient


def _manifest() -> bytes:
    return json.dumps(
        {
            "schema": "rogmem-releases/1",
            "generated_at": "2026-11-12T04:30:00Z",
            "channels": {"stable": {"latest": "V1-2611121012-PUB", "released_at": "2026-11-12T02:14:10Z"}},
            "releases": [
                {"tag": "V1-2611121012-PUB", "channel": "stable", "released_at": "2026-11-12T02:14:10Z"}
            ],
        }
    ).encode()


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_a, **_k):
        return

    def do_GET(self):
        if self.path == "/ok.json":
            body = _manifest()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", '"abc"')
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


_server = None
_port = 0


def _start():
    global _server, _port
    if _server is not None:
        return
    _server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    _port = _server.server_port
    threading.Thread(target=_server.serve_forever, daemon=True).start()


@pytest.fixture
def client(tmp_path, monkeypatch):
    _start()
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_ALLOW_LOOPBACK_SOURCE", "1")
    monkeypatch.delenv("ROGMEM_RELEASES_URL", raising=False)
    from rogmem import engine
    from rogmem.app import build_app

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return TestClient(build_app())


def test_release_view_endpoint_returns_config_missing_before_url_is_set(client):
    response = client.get("/admin/api/settings/release-view")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["view"]["error_kind"] == "config_missing"
    assert "尚未配置发布核对源" in body["html"]


def test_release_source_endpoint_rejects_non_https_public_targets(client):
    response = client.post(
        "/admin/api/settings/release-source",
        json={"url": "http://example.com/x"},
    )
    assert response.status_code == 400
    assert response.json()["error_kind"] == "config_invalid"


def test_set_source_then_refresh_pipes_into_view_and_html(client, monkeypatch):
    monkeypatch.setenv("ROGMEM_BUILD_STAMP", "2611121012")
    monkeypatch.setenv("ROGMEM_RELEASE", "PUB")
    set_result = client.post(
        "/admin/api/settings/release-source",
        json={"url": f"http://127.0.0.1:{_port}/ok.json"},
    )
    assert set_result.status_code == 200
    refresh = client.post("/admin/api/settings/release-refresh", json={})
    assert refresh.status_code == 200
    body = refresh.json()
    assert body["ok"] is True
    assert body["view"]["latest"] == "V1-2611121012-PUB"
    assert "V1-2611121012-PUB" in body["html"]
    assert "已是最新" in body["html"]
    view = client.get("/admin/api/settings/release-view").json()["view"]
    assert view["latest"] == "V1-2611121012-PUB"


def test_about_page_renders_config_missing_before_url_is_set(client):
    page = client.get("/admin/settings/about").text
    assert "尚未配置发布核对源" in page
    assert "关于我们" in page
    assert "refreshRelease" in page
    assert "已是最新" not in page
    assert "请求失败" not in page


def test_about_page_renders_newer_release_after_a_successful_check(client, monkeypatch):
    monkeypatch.setenv("ROGMEM_BUILD_STAMP", "2610010000")
    monkeypatch.setenv("ROGMEM_RELEASE", "PUB")
    client.post(
        "/admin/api/settings/release-source",
        json={"url": f"http://127.0.0.1:{_port}/ok.json"},
    )
    client.post("/admin/api/settings/release-refresh", json={})
    page = client.get("/admin/settings/about").text
    assert "V1-2611121012-PUB" in page
    assert "有新版本" in page
    assert "请求失败" not in page


def test_about_page_renders_failure_detail_as_historical_not_current_latest(client, monkeypatch):
    """Reviewer blocker: after a probe fails, the UI MUST NOT show the
    historical tag as the *current* latest. It can keep the tag as
    historical evidence, but '已是最新' is forbidden in that state.
    """
    monkeypatch.setenv("ROGMEM_BUILD_STAMP", "2611121012")
    monkeypatch.setenv("ROGMEM_RELEASE", "PUB")
    client.post(
        "/admin/api/settings/release-source",
        json={"url": f"http://127.0.0.1:{_port}/ok.json"},
    )
    client.post("/admin/api/settings/release-refresh", json={})
    from rogmem import engine

    engine.set_setting("release_manual_unix", "0")
    engine.db().commit()
    engine.set_release_source_url(f"http://127.0.0.1:{_port}/nope.json")
    client.post("/admin/api/settings/release-refresh", json={})
    page = client.get("/admin/settings/about").text
    assert "V1-2611121012-PUB" in page, "历史成功 tag 应仍可见"
    assert "已是最新" not in page, "失败态不得出现'已是最新'"
    assert "核对源不存在" in page


def test_after_source_switch_the_about_page_hides_the_old_tag(client, monkeypatch):
    """Reviewer blocker 1: switching the source URL must invalidate the old cache."""
    monkeypatch.setenv("ROGMEM_BUILD_STAMP", "2610010000")  # different from remote
    monkeypatch.setenv("ROGMEM_RELEASE", "PUB")
    client.post(
        "/admin/api/settings/release-source",
        json={"url": f"http://127.0.0.1:{_port}/ok.json"},
    )
    client.post("/admin/api/settings/release-refresh", json={})
    before = client.get("/admin/settings/about").text
    assert "V1-2611121012-PUB" in before
    client.post(
        "/admin/api/settings/release-source",
        json={"url": f"http://127.0.0.1:{_port}/somewhere-else.json"},
    )
    page = client.get("/admin/settings/about").text
    assert "V1-2611121012-PUB" not in page, "切源后不得继续展示旧源的 tag"
    assert "已是最新" not in page
    assert "有新版本" not in page
