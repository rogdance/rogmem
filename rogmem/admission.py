"""Admission tickets: identity-first, address-negotiated enrollment.

The old shape split one capability in two: the server minted a bare secret and
the browser stitched ``location.origin`` into the prompt at copy time. That
address describes the machine that *opened* the console, not the machine being
*enrolled*. A ticket makes the capability self-contained instead:

* The hub signs one object carrying the stable identity, the short-lived
  secret, an expiry and an ORDERED POLICY of candidate entrances.
* An address is never trusted because it was copied. A client probes the
  candidates and accepts only the one whose ``/healthz`` answers with the same
  identity. Entrances may change; the identity is the only authority.
* Rendering happens once, on the server, so no placeholder ever reaches a
  copied prompt and the API and the page cannot disagree.

Nothing here imports the server, the database or a browser. The module is
shared verbatim by the hub and by the wheel installed on a host.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

TICKET_PREFIX = "rg1"
TICKET_MARK = "___ROGMEM_TICKET___"
ENTRANCES_MARK = "___ROGMEM_ENTRANCES___"
TICKET_ENV = "ROGMEM_TICKET"
HUB_ENV = "ROGMEM_HUB"
PUBLIC_ORIGIN_ENV = "ROGMEM_PUBLIC_ORIGIN"
DOOR = "http://127.0.0.1:18780"
_GRANT_TTL_SECONDS = 20 * 60


@dataclass(frozen=True)
class Entrance:
    """One way the hub may be reached. ``kind`` explains where it came from."""

    url: str
    kind: str


@dataclass(frozen=True)
class AdmissionTicket:
    """A signed, self-contained capability for one computer to join one hub."""

    hub: str
    secret: str
    expires_at: str
    grant_id: str
    entrances: tuple[Entrance, ...]

    def urls(self) -> list[str]:
        return [item.url for item in self.entrances]

    def expired(self, now: datetime | None = None) -> bool:
        moment = now or datetime.now(timezone.utc)
        try:
            end = datetime.fromisoformat(self.expires_at)
        except (TypeError, ValueError):
            return True
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return moment >= end


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hub_key(data_dir: str | Path | None = None) -> bytes:
    """Stable per-hub signing key. The hub owns it; a host never needs it."""
    raw = str(data_dir or os.environ.get("ROGMEM_DATA", "")).strip()
    if not raw:
        return b"rogmem-ephemeral-key"
    path = Path(raw) / "cookie.key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(os.urandom(32))
    return hashlib.sha256(path.read_bytes() + b"|admission").digest()


def _payload(ticket: AdmissionTicket) -> bytes:
    body = {
        "hub": ticket.hub,
        "secret": ticket.secret,
        "expires_at": ticket.expires_at,
        "grant_id": ticket.grant_id,
        "entrances": [{"url": item.url, "kind": item.kind} for item in ticket.entrances],
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def encode(ticket: AdmissionTicket, key: bytes) -> str:
    body = _b64(_payload(ticket))
    signature = _b64(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    return f"{TICKET_PREFIX}.{body}.{signature}"


def decode(token: str) -> AdmissionTicket | None:
    """Parse without a key. A host reads the policy; only the hub verifies it."""
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != TICKET_PREFIX:
        return None
    try:
        loaded = json.loads(_unb64(parts[1]).decode("utf-8"))
        if not isinstance(loaded, dict):
            return None
        entrances = tuple(
            Entrance(str(row.get("url") or "").strip(), str(row.get("kind") or "").strip())
            for row in (loaded.get("entrances") or [])
            if isinstance(row, dict) and str(row.get("url") or "").strip()
        )
        ticket = AdmissionTicket(
            hub=str(loaded.get("hub") or "").strip(),
            secret=str(loaded.get("secret") or "").strip(),
            expires_at=str(loaded.get("expires_at") or "").strip(),
            grant_id=str(loaded.get("grant_id") or "").strip(),
            entrances=entrances,
        )
    except (ValueError, UnicodeError, TypeError):
        return None
    if not ticket.hub or not ticket.secret.startswith("rg_"):
        return None
    return ticket


def verify(token: str, key: bytes) -> AdmissionTicket | None:
    """Hub-side integrity check. A forged entrance policy must not be redeemed."""
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != TICKET_PREFIX:
        return None
    expected = _b64(hmac.new(key, parts[1].encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(parts[2], expected):
        return None
    ticket = decode(token)
    if ticket is None or ticket.expired():
        return None
    return ticket


def _clean_origin(value: str) -> str:
    """Keep scheme, host, port AND the reverse-proxy path prefix.

    ``location.origin`` drops the pathname, which is exactly why a hub served
    under a sub-path could never be found by a copied prompt.
    """
    text = (value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    path = parsed.path.rstrip("/")
    if path in {"/", ""}:
        path = ""
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "")).rstrip("/")


def derive_entrances(request_url: str, public_origin: str = "") -> tuple[Entrance, ...]:
    """Build the ordered entrance policy from authoritative server context.

    Priority: an operator-declared public origin, then the address this very
    request arrived on (with its path prefix intact), then the local door. The
    door is last because it only means something on a computer already joined.
    """
    declared = _clean_origin(public_origin or os.environ.get(PUBLIC_ORIGIN_ENV, ""))
    seen: set[str] = set()
    ordered: list[Entrance] = []

    def add(url: str, kind: str) -> None:
        cleaned = _clean_origin(url)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            ordered.append(Entrance(cleaned, kind))

    if declared:
        add(declared, "declared")
    add(request_url, "request")
    add(DOOR, "door")
    return tuple(ordered)


def resolve_entrance(
    ticket: AdmissionTicket,
    probe: Callable[[str], dict],
    *,
    door: str = DOOR,
) -> dict:
    """Negotiate a reachable entrance by identity, never by trust in a string.

    ``probe`` is injected so this stays free of any HTTP client. A candidate
    counts only when it answers and names the hub in the ticket.
    """
    wanted = (ticket.hub or "").strip()
    tried: list[dict] = []
    for item in ticket.entrances:
        url = (item.url or "").strip().rstrip("/")
        if not url:
            continue
        answer = dict(probe(url) or {})
        ok = bool(answer.get("ok")) and (not wanted or str(answer.get("hub") or "").strip() == wanted)
        tried.append({"url": url, "kind": item.kind, "ok": ok, "refused": str(answer.get("refused") or "")})
        if ok:
            return {"ok": True, "origin": url, "kind": item.kind, "tried": tried, "refused": ""}
    if ticket.expired():
        return {"ok": False, "origin": "", "kind": "", "tried": tried, "refused": "ticket-expired"}
    if not tried:
        return {"ok": False, "origin": "", "kind": "", "tried": tried, "refused": "no-entrance"}
    refused = next((row["refused"] for row in reversed(tried) if row["refused"]), "")
    return {"ok": False, "origin": "", "kind": "", "tried": tried, "refused": refused or "no-matching-hub"}


def render_prompt(
    template: str,
    ticket_text: str,
    hub: str,
    secret: str,
    origin: str,
    entrances: tuple[Entrance, ...] | list[str] | str = (),
) -> str:
    """The one place a prompt becomes final. No mark survives this call.

    ``entrances`` is rendered as a readable, ordered list as well as being
    signed into the ticket. The human (or the agent reading the prompt) needs
    a concrete URL to fetch the stdlib bootstrap before the wheel exists; the
    ticket is still what decides which of those URLs is actually this hub.
    """
    from rogmem.onboard import GRANT_MARK, HUB_MARK, ORIGIN_MARK

    if isinstance(entrances, str):
        readable = entrances
    else:
        readable = "、".join(
            (item.url if isinstance(item, Entrance) else str(item))
            for item in entrances
        )
    return (
        (template or "")
        .replace(GRANT_MARK, secret or "")
        .replace(TICKET_MARK, ticket_text or "")
        .replace(ENTRANCES_MARK, readable)
        .replace(HUB_MARK, hub or "")
        .replace(ORIGIN_MARK, origin or "")
    )
