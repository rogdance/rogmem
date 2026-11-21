"""Install the host client into the Python that runs this file.

The hub serves this before `python -m rogmem` exists, so it is stdlib only.
It accepts ONE argument, which is either a hub-signed admission ticket (the
current path) or a bare origin (kept for older prompts). A ticket carries the
hub identity and an ordered entrance policy, so the bootstrap probes the
candidates itself instead of trusting the address that happened to be copied.
An entrance counts only when its /healthz names that identity. A login wall is
reported; the cookie already in ROGMEM_HUB_COOKIE is sent and never printed.
Loopback is not sent through an HTTP proxy.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import HTTPErrorProcessor, ProxyHandler, Request, build_opener

COOKIE_ENV = "ROGMEM_HUB_COOKIE"
HUB_ENV = "ROGMEM_HUB"
TICKET_ENV = "ROGMEM_TICKET"
TICKET_PREFIX = "rg1"
DOOR = "http://127.0.0.1:18780"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    argument = (args[0] if args else "").strip() or os.environ.get(TICKET_ENV, "").strip()
    if not argument:
        return _fail("bad-origin")
    argument = argument.rstrip("/")
    try:
        if argument.startswith(TICKET_PREFIX + "."):
            wheel = fetch_client_from_ticket(argument)
        else:
            wheel = fetch_client(argument)
    except _Wall:
        return _fail("login-wall")
    except Exception:
        return _fail("download-failed")
    if not wheel.startswith(b"PK"):
        return _fail("login-wall")
    path = Path(tempfile.mkdtemp(prefix="rogmem-host-")) / "rogmem_host-1.0.0-py3-none-any.whl"
    path.write_bytes(wheel)
    if not _install(path):
        return _fail("pip-failed")
    check = subprocess.run(
        [sys.executable, "-c", "import rogmem.cli, installer.rogmem_installer, rogmem.admission"],
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        return _fail("import-failed")
    print(json.dumps({"ok": True, "python": sys.executable}, ensure_ascii=False))
    return 0


def _fail(refused: str) -> int:
    print(json.dumps({"ok": False, "refused": refused}, ensure_ascii=False))
    return 1


class _Wall(Exception):
    pass


class _Keep(HTTPErrorProcessor):
    def http_response(self, request, response):  # noqa: ANN001
        return response

    https_response = http_response


def _loopback(url: str) -> bool:
    import ipaddress
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _download(url: str, *, timeout: float = 120) -> bytes:
    headers = {}
    cookie = os.environ.get(COOKIE_ENV, "").strip()
    if cookie:
        headers["Cookie"] = cookie
    handlers: list = [_Keep]
    if _loopback(url):
        handlers.append(ProxyHandler({}))
    with build_opener(*handlers).open(Request(url, headers=headers), timeout=timeout) as resp:
        status = getattr(resp, "status", None) or resp.getcode()
        body = resp.read()
    if status != 200:
        raise _Wall()
    return body


def _same_hub(body: bytes, hub: str) -> bool:
    if b"rogmem" not in body:
        return False
    try:
        loaded = json.loads(body.decode("utf-8", "replace"))
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return False
    return isinstance(loaded, dict) and str(loaded.get("hub") or "").strip() == hub


def _ticket_policy(token: str) -> dict | None:
    """Read the identity and ordered entrances out of a ticket. Stdlib only.

    The signature cannot be checked here: the signing key stays on the hub.
    That is fine, because no entrance is trusted on the strength of the blob —
    each one must answer /healthz with the identity written in it, and the hub
    re-verifies the ticket at redeem time.
    """
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != TICKET_PREFIX:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        loaded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeError, TypeError):
        return None
    if not isinstance(loaded, dict):
        return None
    hub = str(loaded.get("hub") or "").strip()
    entrances = []
    for row in loaded.get("entrances") or []:
        if isinstance(row, dict):
            url = str(row.get("url") or "").strip().rstrip("/")
            if url:
                entrances.append(url)
    if not hub or not entrances:
        return None
    return {"hub": hub, "entrances": entrances}


def _from_door(origin: str, hub: str = "") -> bytes | None:
    """The stable door, only when it is the hub named by the ticket / environment."""
    identity = (hub or os.environ.get(HUB_ENV, "")).strip()
    door = DOOR.rstrip("/")
    if not identity or origin.rstrip("/") == door:
        return None
    try:
        health = _download(door + "/healthz", timeout=5)
    except Exception:
        return None
    if not _same_hub(health, identity):
        return None
    try:
        return _download(door + "/host-client.whl")
    except Exception:
        return None


def fetch_client_from_ticket(token: str) -> bytes:
    """Download the wheel from the first candidate that answers as this hub."""
    policy = _ticket_policy(token)
    if policy is None:
        raise _Wall()
    for url in policy["entrances"]:
        try:
            health = _download(url + "/healthz", timeout=5)
        except Exception:
            continue
        if not _same_hub(health, policy["hub"]):
            continue
        try:
            wheel = _download(url + "/host-client.whl")
        except Exception:
            continue
        if wheel.startswith(b"PK"):
            return wheel
    door = _from_door("", policy["hub"])
    if door is not None and door.startswith(b"PK"):
        return door
    raise _Wall()


def fetch_client(origin: str) -> bytes:
    """Legacy path: the wheel from this origin. A dead entrance may leave the door."""
    try:
        return _download(origin.rstrip("/") + "/host-client.whl")
    except Exception as first:
        door = _from_door(origin)
        if door is not None:
            return door
        raise first


def _install(wheel: Path) -> bool:
    base = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--upgrade",
        "--force-reinstall",
        str(wheel),
    ]
    in_venv = sys.prefix != sys.base_prefix
    attempts = [base] if in_venv else [base + ["--user"], base + ["--user", "--break-system-packages"]]
    for command in attempts:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0:
            return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
