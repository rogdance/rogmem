"""Install the host client into the Python that runs this file.

The hub serves this before `python -m rogmem` exists. It uses the standard
library, downloads the wheel from the same hub, and installs it into this
interpreter. The argument is the entrance copied with the prompt. If that
entrance is gone, the local door is used when it is still this hub.
A login wall is reported; the cookie already in ROGMEM_HUB_COOKIE is sent
and never printed. Loopback is not sent through an HTTP proxy.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import HTTPErrorProcessor, ProxyHandler, Request, build_opener

COOKIE_ENV = "ROGMEM_HUB_COOKIE"
HUB_ENV = "ROGMEM_HUB"
DOOR = "http://127.0.0.1:18780"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    origin = (args[0] if args else "").strip().rstrip("/")
    if not origin:
        return _fail("bad-origin")
    try:
        wheel = fetch_client(origin)
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
        [sys.executable, "-c", "import rogmem.cli, installer.rogmem_installer"],
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


def _download(url: str) -> bytes:
    headers = {}
    cookie = os.environ.get(COOKIE_ENV, "").strip()
    if cookie:
        headers["Cookie"] = cookie
    handlers: list = [_Keep]
    if _loopback(url):
        handlers.append(ProxyHandler({}))
    with build_opener(*handlers).open(Request(url, headers=headers), timeout=120) as resp:
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


def _from_door(origin: str) -> bytes | None:
    """The stable door, only when it is the hub named in the environment."""
    hub = os.environ.get(HUB_ENV, "").strip()
    door = DOOR.rstrip("/")
    if not hub or origin.rstrip("/") == door:
        return None
    try:
        health = _download(door + "/healthz")
    except Exception:
        return None
    if not _same_hub(health, hub):
        return None
    try:
        return _download(door + "/host-client.whl")
    except Exception:
        return None


def fetch_client(origin: str) -> bytes:
    """The wheel from this hub. A dead copied entrance may still leave the door."""
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
