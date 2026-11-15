"""Release source — URL validation (SSRF guard) and HTTPS GET with budgets.

The URL validator refuses non-https, userinfo, fragments, loopback,
link-local, and private ranges. The HTTP fetcher honors ETag, enforces
connect/read timeouts, and caps response size regardless of
Content-Length.
"""

from __future__ import annotations

import gzip
import http.server
import json
import socket
import ssl
import threading
import time
from pathlib import Path

import pytest

from rogmem.release_source import (
    FetchOutcome,
    SourceError,
    fetch_manifest,
    validate_source_url,
)


# ---- URL validator -------------------------------------------------------


@pytest.fixture
def _no_loopback(monkeypatch):
    monkeypatch.delenv("ROGMEM_ALLOW_LOOPBACK_SOURCE", raising=False)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/releases.json",
        "https://example.com",
        "https://rogdance.github.io/rogmem-releases-manifest/releases.json",
        "https://raw.githubusercontent.com/rogdance/rogmem-releases/main/releases.json",
    ],
)
def test_url_validator_accepts_https_public_hosts(_no_loopback, url):
    assert validate_source_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://example.com/x",
        "ftp://example.com/x",
        "file:///etc/passwd",
        "data:,{}",
        "javascript:alert(1)",
        "https://user:pass@example.com/x",
        "https://example.com/x#frag",
        "https://example.com/x?a=1@2",
        "https://127.0.0.1/x",
        "https://localhost/x",
        "https://test.localhost/x",
        "https://10.1.2.3/x",
        "https://192.168.0.1/x",
        "https://169.254.169.254/x",
        "https://[::1]/x",
        "https://[fe80::1]/x",
        "https://[fc00::1]/x",
        "https:///relative",
        "https://example.com:0/x",
    ],
)
def test_url_validator_rejects_non_https_and_private_targets(_no_loopback, url):
    with pytest.raises(SourceError) as info:
        validate_source_url(url)
    assert info.value.kind == "config_invalid"


def test_url_validator_rejects_overlong_url(_no_loopback):
    long_url = "https://example.com/" + ("a" * 3000)
    with pytest.raises(SourceError) as info:
        validate_source_url(long_url)
    assert info.value.kind == "config_invalid"


# ---- HTTP fetcher (real local server, loopback allow-list) ---------------


def _manifest_bytes(tag: str = "V1-2611121012-PUB") -> bytes:
    return json.dumps(
        {
            "schema": "rogmem-releases/1",
            "generated_at": "2026-11-12T04:30:00Z",
            "channels": {"stable": {"latest": tag, "released_at": "2026-11-12T02:14:10Z"}},
            "releases": [
                {"tag": tag, "channel": "stable", "released_at": "2026-11-12T02:14:10Z"}
            ],
        }
    ).encode("utf-8")


class _Scenarios:
    """Routes keyed by path prefix; each returns (status, headers, body)."""

    def __init__(self):
        self.calls = []
        self.etag_value = '"manifest-abc"'

    def handle(self, path, headers):
        self.calls.append((path, dict(headers)))
        if path == "/ok.json":
            return 200, [("Content-Type", "application/json"), ("ETag", self.etag_value)], _manifest_bytes()
        if path == "/etag.json":
            inm = headers.get("If-None-Match", "")
            if inm == self.etag_value:
                return 304, [("ETag", self.etag_value)], b""
            return 200, [("Content-Type", "application/json"), ("ETag", self.etag_value)], _manifest_bytes()
        if path == "/not-found.json":
            return 404, [("Content-Type", "application/json")], b'{"m":"nope"}'
        if path == "/rate.json":
            return 429, [("Content-Type", "application/json")], b'{"m":"slow down"}'
        if path == "/rate403.json":
            return (
                403,
                [("Content-Type", "application/json"), ("X-RateLimit-Remaining", "0")],
                b'{"m":"rate limited"}',
            )
        if path == "/boom.json":
            return 503, [("Content-Type", "application/json")], b'{"m":"nope"}'
        if path == "/html.json":
            return 200, [("Content-Type", "text/html")], b"<html>not json</html>"
        if path == "/badschema.json":
            return (
                200,
                [("Content-Type", "application/json")],
                b'{"schema":"rogmem-releases/999","releases":[]}',
            )
        if path == "/oversize.json":
            return (
                200,
                [("Content-Type", "application/json"), ("Content-Length", str(300 * 1024))],
                b"0" * (300 * 1024),
            )
        if path == "/oversize-stream.json":
            return (
                200,
                [("Content-Type", "application/json")],
                b"0" * (300 * 1024),
            )
        if path == "/slow.json":
            time.sleep(2.0)
            return 200, [("Content-Type", "application/json")], _manifest_bytes()
        if path == "/redirect-ok":
            return 302, [("Location", "/ok.json")], b""
        if path == "/redirect-http":
            return 302, [("Location", "http://example.com/x")], b""
        if path == "/redirect-loop":
            return 302, [("Location", "/redirect-loop")], b""
        return 404, [("Content-Type", "text/plain")], b"missing"


_scenarios = _Scenarios()


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_a, **_k):
        return

    def do_GET(self):
        status, headers, body = _scenarios.handle(self.path, self.headers)
        self.send_response(status)
        has_length = any(name.lower() == "content-length" for name, _ in headers)
        for name, value in headers:
            self.send_header(name, value)
        if not has_length and body:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


_server = None
_server_port = 0


def _start_server():
    global _server, _server_port
    if _server is not None:
        return
    _server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    _server_port = _server.server_port
    thread = threading.Thread(target=_server.serve_forever, daemon=True)
    thread.start()


@pytest.fixture
def _loopback_allow(monkeypatch):
    _start_server()
    monkeypatch.setenv("ROGMEM_ALLOW_LOOPBACK_SOURCE", "1")
    _scenarios.calls.clear()
    yield


def _url(path: str) -> str:
    return f"https://127.0.0.1:{_server_port}{path}"


def _plain_url(path: str) -> str:
    return f"http://127.0.0.1:{_server_port}{path}"


def test_fetch_returns_body_and_etag_on_200(_loopback_allow):
    outcome = fetch_manifest(_plain_url("/ok.json"), etag=None, timeout_connect=2, timeout_read=2)
    assert isinstance(outcome, FetchOutcome)
    assert outcome.kind == "ok"
    assert outcome.body is not None
    assert b"rogmem-releases/1" in outcome.body
    assert outcome.etag == '"manifest-abc"'


def test_fetch_sends_if_none_match_and_handles_304(_loopback_allow):
    outcome = fetch_manifest(
        _plain_url("/etag.json"),
        etag='"manifest-abc"',
        timeout_connect=2,
        timeout_read=2,
    )
    assert outcome.kind == "not_modified"
    assert outcome.body is None
    assert outcome.etag == '"manifest-abc"'
    sent = _scenarios.calls[-1][1]
    assert sent.get("If-None-Match") == '"manifest-abc"'


def test_fetch_returns_not_found_for_404(_loopback_allow):
    outcome = fetch_manifest(_plain_url("/not-found.json"), etag=None, timeout_connect=2, timeout_read=2)
    assert outcome.kind == "not_found"
    assert outcome.body is None


def test_fetch_returns_rate_limited_for_429(_loopback_allow):
    outcome = fetch_manifest(_plain_url("/rate.json"), etag=None, timeout_connect=2, timeout_read=2)
    assert outcome.kind == "rate_limited"


def test_fetch_returns_rate_limited_for_403_zero_remaining(_loopback_allow):
    outcome = fetch_manifest(_plain_url("/rate403.json"), etag=None, timeout_connect=2, timeout_read=2)
    assert outcome.kind == "rate_limited"


def test_fetch_returns_server_error_for_5xx(_loopback_allow):
    outcome = fetch_manifest(_plain_url("/boom.json"), etag=None, timeout_connect=2, timeout_read=2)
    assert outcome.kind == "server_error"


def test_fetch_rejects_oversize_content_length(_loopback_allow):
    outcome = fetch_manifest(_plain_url("/oversize.json"), etag=None, timeout_connect=2, timeout_read=2)
    assert outcome.kind == "too_large"
    assert outcome.body is None


def test_fetch_rejects_oversize_streamed_body_without_content_length(_loopback_allow):
    outcome = fetch_manifest(
        _plain_url("/oversize-stream.json"), etag=None, timeout_connect=2, timeout_read=2
    )
    assert outcome.kind == "too_large"


def test_fetch_reports_timeout_on_slow_response(_loopback_allow):
    outcome = fetch_manifest(_plain_url("/slow.json"), etag=None, timeout_connect=1, timeout_read=1)
    assert outcome.kind == "timeout"


def test_fetch_follows_https_redirect_to_same_origin(_loopback_allow):
    outcome = fetch_manifest(
        _plain_url("/redirect-ok"), etag=None, timeout_connect=2, timeout_read=2
    )
    assert outcome.kind == "ok"
    assert outcome.body is not None


def test_fetch_refuses_redirect_to_http(_loopback_allow):
    outcome = fetch_manifest(
        _plain_url("/redirect-http"), etag=None, timeout_connect=2, timeout_read=2
    )
    assert outcome.kind == "network"


def test_fetch_detects_redirect_loop_as_network(_loopback_allow):
    outcome = fetch_manifest(
        _plain_url("/redirect-loop"), etag=None, timeout_connect=2, timeout_read=2
    )
    assert outcome.kind == "network"


def test_fetch_classifies_dns_or_connect_failure_as_network(_loopback_allow, monkeypatch):
    monkeypatch.delenv("ROGMEM_ALLOW_LOOPBACK_SOURCE", raising=False)
    outcome = fetch_manifest(
        "https://nonexistent.invalid/releases.json",
        etag=None,
        timeout_connect=2,
        timeout_read=2,
    )
    assert outcome.kind in {"network", "timeout"}


# ---- blocker 2: SSRF — resolver-driven + socket pinning -----------------


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost./x",
        "https://[::ffff:127.0.0.1]/x",
        "https://[::ffff:169.254.169.254]/x",
        "https://0.0.0.0/x",
        "https://127.1/x",
    ],
)
def test_url_validator_rejects_edge_literal_private_hosts(_no_loopback, url):
    with pytest.raises(SourceError) as info:
        validate_source_url(url)
    assert info.value.kind == "config_invalid"


def test_fetch_refuses_public_hostname_that_resolves_to_private(monkeypatch, _no_loopback):
    """A hostname like 127.0.0.1.nip.io passes URL validation but DNS resolves to loopback."""
    import socket as _socket
    from rogmem import release_source

    def fake(host, port, *_args, **_kwargs):
        return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 443))]

    monkeypatch.setattr(release_source.socket, "getaddrinfo", fake)
    outcome = release_source.fetch_manifest(
        "https://not-really-public.example.test/x",
        etag=None,
        timeout_connect=1,
        timeout_read=1,
    )
    assert outcome.kind == "network", f"DNS 解析到私网必须拒；拿到 {outcome.kind}"


def test_fetch_refuses_public_hostname_that_resolves_to_v4_mapped_v6(monkeypatch, _no_loopback):
    import socket as _socket
    from rogmem import release_source

    def fake(host, port, *_args, **_kwargs):
        return [(_socket.AF_INET6, _socket.SOCK_STREAM, 6, "", ("::ffff:127.0.0.1", port or 443, 0, 0))]

    monkeypatch.setattr(release_source.socket, "getaddrinfo", fake)
    outcome = release_source.fetch_manifest(
        "https://mapped.example.test/x",
        etag=None,
        timeout_connect=1,
        timeout_read=1,
    )
    assert outcome.kind == "network"


def test_fetch_refuses_when_any_resolved_ip_is_private(monkeypatch, _no_loopback):
    """Mixed answer: one public + one private → reject (prevents DNS rebinding)."""
    import socket as _socket
    from rogmem import release_source

    def fake(host, port, *_args, **_kwargs):
        return [
            (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443)),
            (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("192.168.1.1", port or 443)),
        ]

    monkeypatch.setattr(release_source.socket, "getaddrinfo", fake)
    outcome = release_source.fetch_manifest(
        "https://mixed.example.test/x",
        etag=None,
        timeout_connect=1,
        timeout_read=1,
    )
    assert outcome.kind == "network"


def test_fetch_pins_to_resolved_ip_without_a_second_dns_call(monkeypatch, _no_loopback):
    """After passing DNS check, the fetcher MUST connect to the resolved IP
    directly so DNS rebinding cannot flip the answer between check and connect.
    """
    _start_server()
    import socket as _socket
    from rogmem import release_source

    calls = {"resolved": 0, "create_connection": []}
    port = _server_port

    def fake_resolve(host, port_arg, *_args, **_kwargs):
        calls["resolved"] += 1
        return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("127.0.0.1", port_arg or port))]

    real_create = _socket.create_connection

    def wrapped(address, *args, **kwargs):
        calls["create_connection"].append(address)
        return real_create(address, *args, **kwargs)

    real_socket = _socket.socket
    connects = []

    class _TrackedSocket(real_socket):
        def connect(self, address):
            connects.append(address)
            return super().connect(address)

    monkeypatch.setenv("ROGMEM_ALLOW_LOOPBACK_SOURCE", "1")
    monkeypatch.setattr(release_source.socket, "getaddrinfo", fake_resolve)
    monkeypatch.setattr(release_source.socket, "socket", _TrackedSocket)
    _ = release_source.fetch_manifest(
        f"https://loop.example.test:{port}/ok.json",
        etag=None,
        timeout_connect=1,
        timeout_read=1,
    )
    assert calls["resolved"] == 1, "应只解析一次主机，而不是先检查再二次 DNS"
    assert connects, "必须手动建 socket 并 connect，以便钉住 IP"
    for address in connects:
        host, _port_ = address
        assert host == "127.0.0.1", f"connect 必须打到解析出来的 IP：{host}"
