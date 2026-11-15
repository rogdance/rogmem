"""Webhook URL policy: block loopback, private, link-local, and metadata targets."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rogmem.webhook_policy import alert_payload, deliver, validate_webhook_url


def test_blocks_link_local_metadata_and_private():
    blocked = [
        "http://127.0.0.1:9/x",
        "http://127.1/latest",
        "http://0.0.0.0/",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://10.1.2.3/hook",
        "http://192.168.1.9/hook",
        "http://172.16.5.5/hook",
        "http://169.254.169.254/latest/meta-data",
        "http://169.254.1.1/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fd00:ec2::254]/",
        "http://[::ffff:169.254.169.254]/",
        "http://localhost/hook",
        "http://metadata.google.internal/computeMetadata/v1/",
        "file:///etc/passwd",
        "http://user:pass@1.1.1.1/hook",
        "gopher://1.1.1.1/",
        "",
    ]
    for url in blocked:
        assert validate_webhook_url(url), url


def test_allows_public_literal():
    assert validate_webhook_url("https://1.1.1.1/hook") is None
    assert validate_webhook_url("https://8.8.8.8/hook") is None


def test_dns_to_link_local_is_blocked():
    def resolve(host, port, *args, **kwargs):
        assert host == "evil.example"
        return [(socket_family(), None, None, "", ("169.254.169.254", port))]

    assert validate_webhook_url("https://evil.example/hook", resolve=resolve)


def test_mixed_public_and_private_answers_are_blocked():
    def resolve(host, port, *args, **kwargs):
        return [
            (socket_family(), None, None, "", ("1.1.1.1", port)),
            (socket_family(), None, None, "", ("10.1.2.3", port)),
        ]

    assert validate_webhook_url("https://mixed.example/hook", resolve=resolve)
    try:
        deliver("https://mixed.example/hook", b"{}", resolve=resolve, dial=lambda *args: 1)
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_deliver_pins_the_single_lookup():
    calls = {"n": 0}

    def resolve(host, port, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return [(socket_family(), None, None, "", ("1.1.1.1", port))]
        return [
            (socket_family(), None, None, "", ("8.8.8.8", port)),
            (socket_family(), None, None, "", ("127.0.0.1", port)),
        ]

    seen = {}

    def dial(ip, port_, scheme, host, path, payload, timeout):
        seen["ip"] = ip
        return 204

    status = deliver("https://hooks.example/hook", b"{}", resolve=resolve, dial=dial)
    assert status == 204
    assert calls["n"] == 1
    assert seen["ip"] == "1.1.1.1"


def test_dns_to_public_is_allowed():
    def resolve(host, port, *args, **kwargs):
        return [(socket_family(), None, None, "", ("1.1.1.1", port))]

    assert validate_webhook_url("https://hooks.example/hook", resolve=resolve) is None


def test_unresolved_name_fails_closed():
    def resolve(host, port, *args, **kwargs):
        raise OSError("no dns")

    assert validate_webhook_url("https://missing.example/hook", resolve=resolve)


def test_notify_alert_does_not_fetch_stored_loopback(tmp_path, monkeypatch):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
        monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
        monkeypatch.setenv("ROGMEM_EMBED", "off")
        from rogmem import engine

        engine.connect(tmp_path, tmp_path / "scan")
        port = server.server_address[1]
        engine.set_setting("alert_webhook", f"http://127.0.0.1:{port}/hook")
        engine.db().commit()
        engine.notify_alert("外部模型鉴权失败\n原文不该出现")
        assert hits == []
    finally:
        server.shutdown()


def test_deliver_does_not_touch_loopback():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        try:
            deliver(f"http://127.0.0.1:{port}/hook", alert_payload("外部模型鉴权失败"))
            raised = False
        except ValueError:
            raised = True
        assert raised
        assert hits == []
    finally:
        server.shutdown()


def test_deliver_posts_pinned_public_target_without_following_redirect():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            hits.append((self.path, self.rfile.read(length), self.headers.get("Host")))
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data")
            self.end_headers()

        def do_GET(self):
            hits.append(("GET", self.path, b""))
            self.send_response(200)
            self.end_headers()

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        body = alert_payload("外部模型鉴权失败", now="2026-10-25T00:00:00+00:00")
        parsed = json.loads(body)
        assert parsed["text"] == "外部模型鉴权失败"
        assert set(parsed) == {"at", "text"}
        status = deliver(
            "https://1.1.1.1/hook",
            body,
            dial=lambda ip, port_, scheme, host, path, payload, timeout: _dial_local(server, hits, ip, scheme, host, path, payload),
        )
        assert status == 302
        assert len(hits) == 1
        assert hits[0][0] == "/hook"
        assert b"169.254.169.254" not in hits[0][1]
    finally:
        server.shutdown()


def _dial_local(server, hits, ip, scheme, host, path, payload):
    assert ip == "1.1.1.1"
    assert scheme == "https"
    assert host == "1.1.1.1"
    from rogmem.webhook_policy import post_once

    return post_once("127.0.0.1", server.server_address[1], "http", host, path, payload, timeout=2)


def socket_family():
    import socket

    return socket.AF_INET
