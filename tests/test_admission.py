"""Admission tickets: one signed capability, identity-first entrance negotiation.

These cover the failure this change fixes: a prompt copied on one computer used
to describe the address that computer happened to open. Now the hub signs an
ordered entrance policy and the client accepts an address only when /healthz
answers with the hub identity written in the ticket.
"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from installer import rogmem_installer as inst
from rogmem import admission

ROOT = Path(__file__).resolve().parents[1]
KEY = b"rogmem-test-hub-key"


def _ticket(*, hub: str = "rh_ticket", secret: str = "rg_secret", minutes: int = 10,
            entrances=(admission.Entrance("https://hub.example/rogmem", "declared"),)) -> admission.AdmissionTicket:
    return admission.AdmissionTicket(
        hub=hub,
        secret=secret,
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(),
        grant_id="grant-1",
        entrances=tuple(entrances),
    )


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    import threading

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def test_ticket_round_trip_and_tamper_detection():
    ticket = _ticket()
    token = admission.encode(ticket, KEY)
    assert token.startswith("rg1.")
    assert admission.decode(token) == ticket
    assert admission.verify(token, KEY) == ticket
    assert admission.verify(token, b"another-hub") is None

    parts = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    payload["entrances"][0]["url"] = "https://evil.example/rogmem"
    forged_body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    forged = f"rg1.{forged_body}.{parts[2]}"
    assert admission.decode(forged).urls() == ["https://evil.example/rogmem"]
    assert admission.verify(forged, KEY) is None


def test_expired_or_malformed_ticket_is_never_accepted():
    assert admission.verify(admission.encode(_ticket(minutes=-1), KEY), KEY) is None
    assert admission.decode("rg1.not-base64.sig") is None
    assert admission.decode("plain-origin") is None
    assert admission.decode("") is None


def test_entrance_policy_keeps_the_proxy_path_and_is_ordered(monkeypatch):
    monkeypatch.setenv("ROGMEM_PUBLIC_ORIGIN", "https://nas.example/rogmem/")
    entrances = admission.derive_entrances("http://127.0.0.1:18080/")
    assert [item.kind for item in entrances] == ["declared", "request", "door"]
    assert entrances[0].url == "https://nas.example/rogmem"
    assert entrances[1].url == "http://127.0.0.1:18080"
    assert entrances[2].url == admission.DOOR

    monkeypatch.delenv("ROGMEM_PUBLIC_ORIGIN")
    deduped = admission.derive_entrances("https://nas.example/rogmem/rogmem?q=1#f")
    assert [item.url for item in deduped] == ["https://nas.example/rogmem/rogmem", admission.DOOR]
    assert "location.origin" not in "".join(item.url for item in deduped)


def test_resolve_entrance_accepts_only_the_identity_in_the_ticket():
    ticket = _ticket(entrances=(
        admission.Entrance("https://dead.example", "declared"),
        admission.Entrance("https://other.example", "request"),
        admission.Entrance("https://hub.example/rogmem", "request"),
    ))
    answers = {
        "https://dead.example": {"ok": False, "refused": "channel-closed", "hub": ""},
        "https://other.example": {"ok": True, "refused": "", "hub": "rh_someone_else"},
        "https://hub.example/rogmem": {"ok": True, "refused": "", "hub": "rh_ticket"},
    }
    resolved = admission.resolve_entrance(ticket, lambda url: answers[url])
    assert resolved["ok"] is True
    assert resolved["origin"] == "https://hub.example/rogmem"
    assert [row["ok"] for row in resolved["tried"]] == [False, False, True]

    all_wrong = admission.resolve_entrance(ticket, lambda url: {"ok": True, "hub": "rh_stranger"})
    assert all_wrong["ok"] is False
    assert all_wrong["refused"] == "no-matching-hub"


def test_render_prompt_leaves_no_placeholder_behind():
    template = (
        "凭证 ___ROGMEM_GRANT___ / 票据 ___ROGMEM_TICKET___ / "
        "身份 ___ROGMEM_HUB___ / 入口 ___ROGMEM_ORIGIN___ / 清单 ___ROGMEM_ENTRANCES___"
    )
    text = admission.render_prompt(
        template, "rg1.token.sig", "rh_ticket", "rg_secret",
        "https://hub.example/rogmem",
        (admission.Entrance("https://hub.example/rogmem", "declared"), admission.Entrance(admission.DOOR, "door")),
    )
    assert "___ROGMEM_" not in text
    assert "rg_secret" in text and "rg1.token.sig" in text and "rh_ticket" in text
    assert "https://hub.example/rogmem" in text
    assert admission.DOOR in text


def _api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_PUBLIC_ORIGIN", "https://nas.example/rogmem")
    from rogmem import engine
    from rogmem.app import build_app

    engine.connect(tmp_path, tmp_path / "scan")
    return TestClient(build_app()), engine


def test_admin_api_mints_a_signed_ticket_and_a_final_prompt(tmp_path, monkeypatch):
    client, engine = _api_client(tmp_path, monkeypatch)
    response = client.post("/admin/api/enroll-grants", json={})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ok"] is True and data["secret"].startswith("rg_")
    assert data["ticket"].startswith("rg1.")

    ticket = admission.verify(data["ticket"], admission.hub_key())
    assert ticket is not None
    assert ticket.hub == data["hub"] == engine.hub_id()
    assert ticket.secret == data["secret"]
    assert ticket.grant_id == data["id"]
    assert data["entrances"] == ticket.urls()
    assert data["entrances"][0] == "https://nas.example/rogmem"

    prompt = data["prompt"]
    assert "___ROGMEM_" not in prompt
    assert data["ticket"] in prompt
    assert data["secret"] in prompt
    assert "https://nas.example/rogmem" in prompt
    assert "channel --ticket" in prompt
    assert "hosts connect --confirm --ticket" in prompt
    assert "location.origin" not in prompt


def test_redeem_refuses_a_forged_ticket_before_touching_the_grant(tmp_path, monkeypatch):
    client, _ = _api_client(tmp_path, monkeypatch)
    minted = client.post("/admin/api/enroll-grants", json={}).json()
    body = {
        "grant": minted["secret"],
        "device_id": "dev-a",
        "device_name": "AAAA",
        "program": "Cursor",
        "pubkey": "not-a-key",
    }
    forged = client.post("/enroll/redeem", json={**body, "ticket": "rg1.forged.sig"})
    assert forged.status_code == 403
    assert forged.json()["ok"] is False

    swapped = admission.encode(_ticket(secret="rg_other"), b"wrong-key")
    mismatch = client.post("/enroll/redeem", json={**body, "ticket": swapped})
    assert mismatch.status_code == 403


def test_page_and_wheel_carry_the_shared_admission_module(tmp_path, monkeypatch):
    client, _ = _api_client(tmp_path, monkeypatch)
    wheel = client.get("/host-client.whl")
    assert wheel.status_code == 200
    with zipfile.ZipFile(io.BytesIO(wheel.content)) as archive:
        names = set(archive.namelist())
    assert "rogmem/admission.py" in names
    assert "rogmem/app.py" not in names
    bootstrap = client.get("/host-bootstrap").text
    assert "fetch_client_from_ticket" in bootstrap
    assert "def main" in bootstrap


def test_bootstrap_reads_the_policy_and_picks_the_matching_entrance(monkeypatch):
    from rogmem import host_bootstrap as bootstrap

    wheel = b"PK\x03\x04test-wheel"

    class Hub(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/healthz"):
                body = b'{"service":"rogmem","hub":"rh_ticket"}'
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

    server, origin = _serve(Hub)
    monkeypatch.setenv("ROGMEM_HUB_COOKIE", "")
    try:
        token = admission.encode(_ticket(entrances=(
            admission.Entrance("http://127.0.0.1:1", "declared"),
            admission.Entrance(origin, "request"),
        )), KEY)
        policy = bootstrap._ticket_policy(token)
        assert policy["hub"] == "rh_ticket"
        assert policy["entrances"] == ["http://127.0.0.1:1", origin]
        assert bootstrap.fetch_client_from_ticket(token) == wheel

        wrong = admission.encode(_ticket(hub="rh_someone_else", entrances=(
            admission.Entrance(origin, "request"),
        )), KEY)
        with pytest.raises(bootstrap._Wall):
            bootstrap.fetch_client_from_ticket(wrong)
    finally:
        server.shutdown()


def test_channel_ticket_negotiates_the_entrance_before_planning(tmp_path, monkeypatch):
    import rogmem.cli as cli

    token = admission.encode(_ticket(), KEY)
    seen = {}

    def resolve(token_text, *, cookie=""):
        seen["token"] = token_text
        return {"ok": True, "origin": "http://127.0.0.1:18780", "hub": "rh_ticket", "refused": "", "tried": []}

    def plan(origin, hub=""):
        seen["origin"], seen["hub"] = origin, hub
        return {"ok": True, "mode": "ready", "refused": "", "origin": origin, "target": 0, "location": "", "hub": hub}

    monkeypatch.setattr(inst, "resolve_ticket", resolve)
    monkeypatch.setattr(inst, "plan_channel", plan)
    assert cli.main(["channel", "--ticket", token, "--home", str(tmp_path)]) == 0
    assert seen == {"token": token, "origin": "http://127.0.0.1:18780", "hub": "rh_ticket"}


def test_channel_ticket_reports_a_dead_policy_without_opening_a_door(tmp_path, monkeypatch):
    import rogmem.cli as cli

    monkeypatch.setattr(inst, "resolve_ticket", lambda token, *, cookie="": {
        "ok": False, "origin": "", "hub": "rh_ticket", "refused": "no-matching-hub", "tried": [],
    })
    assert cli.main(["channel", "--ticket", "rg1.dead.policy", "--home", str(tmp_path)]) == 1


def test_hosts_connect_ticket_derives_the_grant_and_lets_the_hub_verify(tmp_path, monkeypatch):
    import rogmem.cli as cli

    token = admission.encode(_ticket(secret="rg_from_ticket"), KEY)
    posted = []
    monkeypatch.setattr(inst, "connect_hosts", lambda home, base, programs, confirm=False, allow_real=False: {
        "ok": True, "written": ["Cursor"], "text": "", "refused": "",
    })
    monkeypatch.setattr(inst, "chosen_hosts", lambda home, programs: ["Cursor"])

    def redeem(home, base, grant, program, post, allow_real=False):
        post({"grant": grant, "program": program})
        return {"ok": True, "status": "启用", "refused": ""}

    monkeypatch.setattr(inst, "redeem_grant", redeem)
    monkeypatch.setattr(cli, "_post_json", lambda url, body: posted.append(body) or {"ok": True})
    assert cli.main(["hosts", "connect", "--confirm", "--ticket", token, "--home", str(tmp_path)]) == 0
    assert posted[0]["grant"] == "rg_from_ticket"
    assert posted[0]["ticket"] == token


def test_hosts_connect_refuses_a_mismatched_grant(tmp_path, monkeypatch):
    import rogmem.cli as cli

    token = admission.encode(_ticket(secret="rg_from_ticket"), KEY)
    assert cli.main([
        "hosts", "connect", "--confirm", "--ticket", token,
        "--grant", "rg_different", "--home", str(tmp_path),
    ]) == 1
