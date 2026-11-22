"""Find the rogmem health check and forward a fixed local port to it.

Official host path is this script plus the Web admin. No packaged client.
Requires Python 3.12+. Cold-start prompt: installer/COLD_START.md
"""

from __future__ import annotations

import json
import os
import platform
import re
import secrets
import shutil
import threading
import time
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request

from rogmem.hermes_memory import stable_seq, work_repository

FIXED_FORWARD_PORT = 18780
# A relay can accept the socket and still take a moment to answer. Discovery has to outlast that.
_ENTRANCE_TIMEOUT = 2


def find_rogmem(start: int = 10000, end: int = 10050) -> int | None:
    """A range scan for callers that have no hub identity. Loopback is not sent through a proxy."""
    for port in range(start, end + 1):
        if probe_origin(f"http://127.0.0.1:{port}", timeout=3).get("ok"):
            return port
    return None


def open_direct(request, timeout: float = 5):
    """Open a URL. A loopback destination is this machine, so it does not go through an HTTP proxy."""
    from urllib.request import ProxyHandler, Request, build_opener

    if not isinstance(request, Request):
        request = Request(request)
    handlers = [ProxyHandler({})] if _loopback(request.full_url) else []
    return build_opener(*handlers).open(request, timeout=timeout)


def _hub_from(body: bytes) -> str:
    try:
        loaded = json.loads(body.decode("utf-8", errors="replace"))
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(loaded, dict):
        return ""
    return str(loaded.get("hub") or "").strip()


def same_hub(expected: str, actual: str) -> bool:
    """An unnamed ask accepts whatever answered. A named hub must be that one."""
    wanted = (expected or "").strip()
    if not wanted:
        return True
    return (actual or "").strip() == wanted


def _loopback_host(host: str) -> bool:
    import ipaddress

    name = (host or "").lower().rstrip(".")
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def probe_origin(origin: str, cookie: str | None = None, *, timeout: float = 5) -> dict:
    """Ask origin/healthz without following a redirect off this address.

    cookie None uses the process environment. An empty string asks with no browser session.
    A loopback entrance is this machine. It is not sent through an HTTP proxy:
    the proxy would ask some other machine to open its own loopback.
    """
    import os
    from urllib.parse import urlsplit
    from urllib.request import HTTPErrorProcessor, ProxyHandler, Request, build_opener

    cleaned = (origin or "").strip().rstrip("/")
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return {"ok": False, "refused": "bad-origin", "location": "", "hub": ""}

    class _Keep(HTTPErrorProcessor):
        def http_response(self, request, response):  # noqa: ANN001
            return response

        https_response = http_response

    headers = {}
    if cookie is None:
        cookie = os.environ.get("ROGMEM_HUB_COOKIE", "")
    cookie = cookie.strip()
    if cookie:
        headers["Cookie"] = cookie
    handlers: list = [_Keep]
    if _loopback_host(parsed.hostname or ""):
        handlers.append(ProxyHandler({}))
    try:
        with build_opener(*handlers).open(Request(cleaned + "/healthz", headers=headers), timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            location = resp.headers.get("Location", "")
            body = resp.read(65536)
    except Exception:
        return {"ok": False, "refused": "channel-closed", "location": "", "hub": ""}
    identity = _hub_from(body)
    if status == 200 and b"rogmem" in body:
        return {"ok": True, "refused": "", "location": "", "hub": identity}
    if status in {301, 302, 303, 307, 308}:
        return {"ok": False, "refused": "login-wall", "location": location, "hub": ""}
    return {"ok": False, "refused": "channel-closed", "location": location, "hub": ""}


def _proc_address_is_local(ip_hex: str) -> bool:
    import ipaddress

    try:
        if len(ip_hex) == 8:
            address = ipaddress.ip_address(bytes.fromhex(ip_hex)[::-1])
        elif len(ip_hex) == 32:
            words = [ip_hex[index : index + 8] for index in range(0, 32, 8)]
            raw = b"".join(bytes.fromhex(word)[::-1] for word in words)
            address = ipaddress.ip_address(raw)
        else:
            return False
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _ports_from_proc(text: str) -> set[int]:
    """LISTEN rows whose local address accepts a connection to this machine."""
    ports: set[int] = set()
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or parts[3] != "0A" or ":" not in parts[1]:
            continue
        ip_hex, port_hex = parts[1].rsplit(":", 1)
        try:
            port = int(port_hex, 16)
        except ValueError:
            continue
        if 0 < port <= 65535 and _proc_address_is_local(ip_hex):
            ports.add(port)
    return ports


def _local_name(host: str) -> bool:
    import ipaddress

    name = host.strip().strip("[]").lower()
    if name in {"*", "", "localhost"}:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _listen_endpoint(token: str) -> tuple[str, int | None]:
    text = token.strip()
    if text.startswith("[") and "]:" in text:
        host, _, port_text = text[1:].partition("]:")
        if port_text.isdigit():
            return host, int(port_text)
        return "", None
    if ":" in text:
        host, _, port_text = text.rpartition(":")
        if port_text.isdigit() and host:
            return host, int(port_text)
    if "." in text:
        host, _, port_text = text.rpartition(".")
        if port_text.isdigit() and host:
            return host, int(port_text)
    return "", None


def _ports_from_listen_table(text: str) -> set[int]:
    """Local ports in a LISTEN table. The remote column is not a binding."""
    ports: set[int] = set()
    for line in text.splitlines():
        if "LISTEN" not in line.upper():
            continue
        for token in line.split():
            host, port = _listen_endpoint(token)
            if port is None or not 0 < port <= 65535 or not _local_name(host):
                continue
            ports.add(port)
    return ports


def _netstat_text() -> str:
    """Read the listen table without assuming the OS speaks UTF-8.

    A Chinese Windows ``netstat`` emits the local code page (cp936/GBK). Asking
    ``subprocess`` for ``text=True`` decodes with UTF-8, raises inside its
    reader thread and silently yields no stdout — which makes every local-door
    fallback look like there is no listener at all. Decode bytes ourselves and
    tolerate undecodable bytes: the table we parse is ASCII regardless.
    """
    import locale
    import subprocess

    command = ["netstat", "-an", "-p", "tcp"] if platform.system() == "Windows" else ["netstat", "-an"]
    try:
        result = subprocess.run(command, check=False, capture_output=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    raw = result.stdout or b""
    if not raw:
        return ""
    # On Windows netstat writes the system ANSI code page, not the interpreter
    # locale. Python may report UTF-8 even when the bytes are cp936/GBK.
    if platform.system() == "Windows":
        encoding = "mbcs"
    else:
        try:
            encoding = locale.getpreferredencoding(False) or "utf-8"
        except Exception:
            encoding = "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def listening_ports() -> list[int]:
    """TCP ports on this machine that a loopback client can open. Not a guessed band."""
    tcp = Path("/proc/net/tcp")
    if tcp.is_file():
        found = _ports_from_proc(tcp.read_text(encoding="utf-8", errors="replace"))
        v6 = Path("/proc/net/tcp6")
        if v6.is_file():
            found |= _ports_from_proc(v6.read_text(encoding="utf-8", errors="replace"))
        return sorted(found)
    return sorted(_ports_from_listen_table(_netstat_text()))


def find_binding(hub: str, *, allow_door: bool = True, skip: set[int] | None = None) -> str:
    """The current entrance of this hub. The fixed door wins when it is already that hub."""
    identity = (hub or "").strip()
    if not identity:
        return ""
    ignored = set(skip or ())
    if not allow_door:
        ignored.add(FIXED_FORWARD_PORT)
    ports = [port for port in listening_ports() if port not in ignored and 0 < port <= 65535]
    door = FIXED_FORWARD_PORT
    if allow_door and door in ports:
        origin = f"http://127.0.0.1:{door}"
        probed = probe_origin(origin, timeout=_ENTRANCE_TIMEOUT)
        if probed.get("ok") and same_hub(identity, str(probed.get("hub") or "")):
            return origin
        ports = [port for port in ports if port != door]
    if not ports:
        return ""

    def check(port: int) -> int:
        probed = probe_origin(f"http://127.0.0.1:{port}", timeout=_ENTRANCE_TIMEOUT)
        if probed.get("ok") and same_hub(identity, str(probed.get("hub") or "")):
            return port
        return 0

    from concurrent.futures import ThreadPoolExecutor

    hits: list[int] = []
    workers = min(16, len(ports))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for port in pool.map(check, ports):
            if port:
                hits.append(port)
    if not hits:
        return ""
    return f"http://127.0.0.1:{min(hits)}"


def resolve_ticket(token: str, *, cookie: str = "") -> dict:
    """Negotiate a live entrance from a hub-signed admission ticket.

    Addresses are candidates, never authorities. Each is probed and accepted
    only when it answers with the identity written in the ticket. This is what
    makes one copied prompt work on a different computer, behind a reverse
    proxy sub-path, or after the entrance has moved.
    """
    from rogmem import admission

    ticket = admission.decode(token)
    if ticket is None:
        return {"ok": False, "origin": "", "hub": "", "refused": "bad-ticket", "tried": []}
    if ticket.expired():
        return {"ok": False, "origin": "", "hub": ticket.hub, "refused": "ticket-expired", "tried": []}

    def probe(url: str) -> dict:
        return probe_origin(url, cookie or None, timeout=_ENTRANCE_TIMEOUT)

    answer = admission.resolve_entrance(ticket, probe)
    if answer.get("ok"):
        return {"ok": True, "origin": answer["origin"], "hub": ticket.hub, "refused": "", "tried": answer["tried"]}
    # A remembered door on this computer is the last resort, but it still has
    # to answer with the ticket's identity.
    bound = find_binding(ticket.hub, allow_door=True)
    if bound and same_hub(ticket.hub, str(probe_origin(bound, cookie or None, timeout=_ENTRANCE_TIMEOUT).get("hub") or "")):
        return {"ok": True, "origin": bound, "hub": ticket.hub, "refused": "", "tried": answer["tried"]}
    return {
        "ok": False,
        "origin": "",
        "hub": ticket.hub,
        "refused": str(answer.get("refused") or "no-matching-hub"),
        "tried": answer["tried"],
    }


def _channel_shape(origin: str, hub: str) -> dict:
    from urllib.parse import urlsplit

    parsed = urlsplit(origin)
    host = (parsed.hostname or "").lower()
    plan = {"mode": "", "target": 0, "origin": origin, "location": "", "hub": hub, "ok": True, "refused": ""}
    if parsed.scheme == "http" and host in {"127.0.0.1", "localhost"}:
        port = parsed.port or 80
        if port == FIXED_FORWARD_PORT:
            plan["mode"] = "ready"
            return plan
        plan.update(mode="port", target=port)
        return plan
    plan["mode"] = "url"
    return plan


def plan_channel(origin: str, hub: str | None = None) -> dict:
    """Splice this hub to the local door.

    The origin is the entrance copied with the prompt. A loopback port is only
    the current binding of that entrance. When it is not this hub, another
    listener that is this hub is the same entrance. A different hub is not.
    """
    import os

    if hub is None:
        hub = os.environ.get("ROGMEM_HUB", "")
    identity = (hub or "").strip()
    cleaned = (origin or "").strip().rstrip("/")
    probed = (
        probe_origin(cleaned)
        if cleaned
        else {"ok": False, "refused": "bad-origin", "location": "", "hub": ""}
    )
    chosen = cleaned if probed.get("ok") and same_hub(identity, str(probed.get("hub") or "")) else ""
    if not chosen and identity:
        chosen = find_binding(identity, allow_door=True)
        if chosen:
            probed = probe_origin(chosen)
    if chosen and identity:
        door = f"http://127.0.0.1:{FIXED_FORWARD_PORT}"
        if chosen.rstrip("/") != door:
            held = probe_origin(door, timeout=_ENTRANCE_TIMEOUT)
            if held.get("ok") and same_hub(identity, str(held.get("hub") or "")):
                chosen = door
                probed = held
    if not chosen:
        refused = "channel-closed" if probed.get("ok") else str(probed.get("refused") or "channel-closed")
        return {
            "mode": "",
            "target": 0,
            "origin": cleaned,
            "location": "" if probed.get("ok") else (probed.get("location") or ""),
            "hub": identity,
            "ok": False,
            "refused": refused,
        }
    resolved = identity or str(probed.get("hub") or "")
    return _channel_shape(chosen, resolved)


def _vscode_app_dirs(home: Path, name: str) -> list[Path]:
    """Every place a VS Code-derived host may keep its per-user data on any system."""
    return [
        home / "Library" / "Application Support" / name,
        home / "AppData" / "Roaming" / name,
        home / ".config" / name,
    ]


def _preferred_app_dir(home: Path, name: str) -> Path:
    system = platform.system()
    if system == "Windows":
        return home / "AppData" / "Roaming" / name
    if system == "Linux":
        return home / ".config" / name
    return home / "Library" / "Application Support" / name


def _cursor_app_dirs(home: Path) -> list[Path]:
    return _vscode_app_dirs(home, "Cursor")


def _preferred_cursor_dir(home: Path) -> Path:
    return _preferred_app_dir(home, "Cursor")


def trae_mcp_path(home: Path) -> Path:
    """Trae keeps user-level MCP in its VS Code-style User dir, not in ~/.trae."""
    return _preferred_app_dir(home, "Trae") / "User" / "mcp.json"


def hermes_home(home: Path) -> Path:
    """Hermes' own home. Windows keeps it under %LOCALAPPDATA%, not in the dotfile dir.

    A simulated or test home still wins when it already has `.hermes`, so fixtures
    keep working; only a real user home falls through to the platform location.
    """
    dot = home / ".hermes"
    if os.name == "nt" and _real_user_home(home):
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            candidate = Path(local) / "hermes"
            if candidate.is_dir():
                return candidate
    return dot


SUPPORTED = ("Cursor", "Trae", "Hermes", "Codex", "WorkBuddy", "CodeBuddy", "DSH")


def detect_programs(home: Path) -> list[dict]:
    known = {
        "Cursor": [*_cursor_app_dirs(home), home / ".cursor"],
        "Trae": [home / ".trae", *_vscode_app_dirs(home, "Trae")],
        "Hermes": [hermes_home(home)],
        "Codex": [home / ".codex"],
        "WorkBuddy": [home / ".workbuddy"],
        "CodeBuddy": [home / ".codebuddy"],
        "DSH": [home / ".dsh"],
    }
    found = []
    for name, paths in known.items():
        hit = next((path for path in paths if path.exists()), None)
        fallback = _preferred_cursor_dir(home) if name == "Cursor" else paths[0]
        found.append({"program": name, "found": hit is not None, "path": str(hit or fallback)})
    return found


def _host_config(home: Path, program: str) -> Path | None:
    paths = {
        "Cursor": home / ".cursor" / "mcp.json",
        "Trae": trae_mcp_path(home),
        "WorkBuddy": home / ".workbuddy" / "mcp.json",
        "Codex": home / ".codex" / "config.toml",
        "Hermes": hermes_home(home) / "config.yaml",
        "CodeBuddy": home / ".codebuddy" / "mcp.json",
        "DSH": home / ".rogmem" / "hosts" / "DSH.json",
    }
    return paths.get(program)


def _points_at_hub(path: Path | None) -> bool:
    if path is None or not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return "rogmem" in text and "/mcp" in text


def _has_token(path: Path | None) -> bool:
    if path is None or not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return "Bearer rm_" in text or 'bearer_token = "rm_' in text or "bearer_token: rm_" in text


def _key_file(home: Path, program: str) -> Path:
    return home / ".rogmem" / "keys" / f"{program}.json"


def _load_key(home: Path, program: str) -> dict | None:
    path = _key_file(home, program)
    if not path.exists():
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or "public" not in loaded or "private" not in loaded:
        return None
    return loaded


def _pending_code(home: Path, program: str) -> str:
    saved = _load_key(home, program)
    if saved is None:
        return ""
    from rogmem import crypto

    return crypto.code_for(crypto.b64decode(saved["public"]))


def inspect_hosts(home: Path, allow_real: bool = False) -> list[dict]:
    """Scan one home for supported hosts. A real user home is not scanned unless allow_real."""
    if _real_user_home(Path(home)) and not allow_real:
        return []
    detected = {item["program"]: item["found"] for item in detect_programs(home)}
    counted = {item["program"]: item for item in history_preview(home, allow_real=allow_real).get("programs") or []}
    out = []
    for program in SUPPORTED:
        found = bool(detected.get(program))
        config = _host_config(home, program)
        if not found:
            access, health, detail = "未安装", "通道未开", ""
        elif _points_at_hub(config) and _has_token(config):
            probe = _saved_probe(home, program)
            if probe and probe.get("health") == "正常":
                access, health, detail = "已接入", "正常", "最近一次调用成功。"
            elif probe and probe.get("health") == "异常":
                access, health, detail = "已接入", "异常", str(probe.get("detail") or "调用没有成功。")
            elif probe and probe.get("health") == "通道未开":
                access, health, detail = "已接入", "通道未开", str(probe.get("detail") or "调用没有成功。")
            else:
                access, health, detail = "已接入", "通道未开", "密钥已写入。调用尚未成功。"
        elif _points_at_hub(config):
            code = _pending_code(home, program)
            detail = f"核对码：{code}" if code else "配置已指向记忆中枢。尚未领到密钥。"
            access, health = "待授权", "通道未开"
        else:
            access, health, detail = "已发现未接入", "通道未开", ""
        counted_row = counted.get(program)
        importable = None
        if found:
            importable = int(counted_row["importable"]) if counted_row else 0
        if counted_row:
            history = (
                f"可导入 {counted_row['importable']} 条，跳过 {counted_row['skipped']} 条。"
                "原文会送到记忆中枢。确认之前不会导入。"
            )
            detail = f"{detail} {history}".strip()
        out.append(
            {
                "program": program,
                "found": found,
                "access": access,
                "health": health,
                "detail": detail,
                "importable": importable,
            }
        )
    return out


def hosts_for_report(home: Path, base_url: str) -> list[dict]:
    """What the installer reports. A listed but unwritten diff is 待确认."""
    return preview_hosts(home, base_url)


def preview_hosts(home: Path, base_url: str) -> list[dict]:
    """List hosts. A found host with an unwritten diff is 待确认, not 已发现未接入."""
    rows = inspect_hosts(home)
    for row in rows:
        if row.get("access") != "已发现未接入":
            continue
        plan = connect_hosts(home, base_url, [row["program"]], confirm=False)
        diff = plan.get("text") or ""
        if "将指向" in diff:
            row["access"] = "待确认"
            row["detail"] = f"{diff} {row.get('detail') or ''}".strip()
    return rows


def _device_id(home: Path) -> str:
    path = home / ".rogmem" / "device_id"
    if path.exists():
        found = path.read_text(encoding="utf-8").strip()
        if found:
            return found
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(16)
    path.write_text(value, encoding="utf-8")
    return value


def device_display_name(home: Path, override: str = "") -> str:
    """Stable device_id stays in device_id; this is only the outward label."""
    from rogmem.device_identity import normalize_device_name

    name = normalize_device_name(override)
    if _real_user_home(Path(home)):
        return name
    path = Path(home) / ".rogmem" / "device_name"
    chosen = (override or "").strip()
    if chosen:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name + "\n", encoding="utf-8")
        return name
    if path.exists():
        saved = path.read_text(encoding="utf-8").strip()
        if saved:
            return normalize_device_name(saved)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name + "\n", encoding="utf-8")
    return name


def relabel_local_device(data_dir: Path, name: str = "") -> int:
    """Optional installer-local UPDATE of historical 本机. Never touch $HOME or NAS blindly."""
    from rogmem.device_identity import migrate_local_device_labels

    root = Path(data_dir)
    if _real_user_home(root) or _real_user_home(root.parent):
        return 0
    db_path = root / "rogmem.sqlite"
    if not db_path.is_file():
        return 0
    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        return migrate_local_device_labels(con, name or None)
    finally:
        con.close()


def authorize_host(home: Path, base_url: str, program: str, post, device_name: str = "", allow_real: bool = False) -> dict:
    """Ask for a host key. The key is not written until it is claimed."""
    del base_url
    if _real_user_home(Path(home)) and not allow_real:
        return {"ok": False, "refused": "real-home", "code": "", "status": "", "wrote": False}
    if program not in plan(home):
        return {"ok": False, "refused": "not-installed", "code": "", "status": "", "wrote": False}
    config = _host_config(home, program)
    if not _points_at_hub(config):
        return {"ok": False, "refused": "unconfirmed", "code": "", "status": "", "wrote": False}
    if _has_token(config):
        return {"ok": True, "refused": "", "code": "", "status": "已接入", "wrote": False}
    if post is None:
        return {"ok": False, "refused": "no-target", "code": "", "status": "", "wrote": False}
    from rogmem import crypto

    saved = _load_key(home, program)
    if saved is None:
        secret, public = crypto.new_keypair()
        saved = {"public": crypto.b64encode(public), "private": crypto.b64encode(secret)}
        path = _key_file(home, program)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(saved), encoding="utf-8")
    result = post(
        {
            "device_id": _device_id(home),
            "device_name": device_display_name(home, device_name),
            "program": program,
            "pubkey": saved["public"],
        }
    )
    if not isinstance(result, dict) or not result.get("ok"):
        return {"ok": False, "refused": "unclaimed", "code": "", "status": "", "wrote": False}
    return {
        "ok": True,
        "refused": "",
        "code": str(result.get("code") or ""),
        "status": str(result.get("status") or ""),
        "id": result.get("id") or "",
        "wrote": False,
    }


def reclaim_host(home: Path, base_url: str, program: str, post) -> dict:
    """Lost key: the same device and program go back to 待放行. The old key dies. Nothing is written."""
    del base_url
    if _real_user_home(Path(home)):
        return {"ok": False, "refused": "real-home", "status": "", "code": "", "wrote": False}
    if program not in plan(home):
        return {"ok": False, "refused": "not-installed", "status": "", "code": "", "wrote": False}
    if post is None:
        return {"ok": False, "refused": "no-target", "status": "", "code": "", "wrote": False}
    from rogmem import crypto

    saved = _load_key(home, program)
    if saved is None:
        secret, public = crypto.new_keypair()
        saved = {"public": crypto.b64encode(public), "private": crypto.b64encode(secret)}
        path = _key_file(home, program)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(saved), encoding="utf-8")
    result = post(
        {
            "pubkey": saved["public"],
            "device_id": _device_id(home),
            "program": program,
        }
    )
    if not isinstance(result, dict) or not result.get("ok"):
        return {"ok": False, "refused": "unclaimed", "status": "", "code": "", "wrote": False}
    return {
        "ok": True,
        "refused": "",
        "status": str(result.get("status") or ""),
        "code": str(result.get("code") or ""),
        "id": result.get("id") or "",
        "wrote": False,
    }


def claim_host(home: Path, base_url: str, program: str, post, allow_real: bool = False) -> dict:
    """Write a claimed key into the confirmed config. A missed claim writes nothing."""
    if _real_user_home(Path(home)) and not allow_real:
        return {"ok": False, "refused": "real-home", "wrote": False}
    if program not in plan(home) or not _points_at_hub(_host_config(home, program)):
        return {"ok": False, "refused": "unconfirmed", "wrote": False}
    if _has_token(_host_config(home, program)):
        return {"ok": True, "refused": "", "wrote": False}
    saved = _load_key(home, program)
    if saved is None or post is None:
        return {"ok": False, "refused": "unclaimed", "wrote": False}
    result = post({"pubkey": saved["public"]})
    if not isinstance(result, dict) or not result.get("ok") or not result.get("wrapped"):
        return {"ok": False, "refused": "unclaimed", "wrote": False}
    from rogmem import crypto

    token = crypto.open_seal(crypto.b64decode(saved["private"]), crypto.b64decode(result["wrapped"])).decode()
    if not token.startswith("rm_"):
        return {"ok": False, "refused": "unclaimed", "wrote": False}
    written = write_configs(home, base_url, token, [program], allow_real=allow_real)
    if program not in written or not _has_token(_host_config(home, program)):
        return {"ok": False, "refused": "unclaimed", "wrote": False}
    _key_file(home, program).unlink(missing_ok=True)
    return {"ok": True, "refused": "", "wrote": True}


def _probe_file(home: Path, program: str) -> Path:
    return home / ".rogmem" / "probes" / f"{program}.json"


def _saved_probe(home: Path, program: str) -> dict | None:
    path = _probe_file(home, program)
    if not path.exists():
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return None
    return loaded


def _endpoint(home: Path, program: str) -> tuple[str, str]:
    path = _host_config(home, program)
    if path is None or not path.exists():
        return "", ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if program in {"Cursor", "Trae", "WorkBuddy", "CodeBuddy"}:
        loaded = json.loads(text or "{}")
        server = loaded.get("mcpServers", {}).get("rogmem", {}) if isinstance(loaded, dict) else {}
        if not isinstance(server, dict):
            return "", ""
        header = server.get("headers", {})
        auth = header.get("Authorization", "") if isinstance(header, dict) else ""
        token = auth[7:] if isinstance(auth, str) and auth.startswith("Bearer ") else ""
        return str(server.get("url") or ""), token
    if program == "DSH":
        try:
            loaded = json.loads(text or "{}")
        except json.JSONDecodeError:
            return "", ""
        if not isinstance(loaded, dict):
            return "", ""
        return str(loaded.get("url") or ""), str(loaded.get("token") or "")
    url = ""
    token = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("url:"):
            url = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("url "):
            url = stripped.split("=", 1)[1].strip().strip('"')
        elif stripped.startswith("bearer_token:"):
            token = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("bearer_token "):
            token = stripped.split("=", 1)[1].strip().strip('"')
    return url, token


def _channel_open(mcp_url: str) -> bool:
    from urllib.parse import urlsplit

    parts = urlsplit(mcp_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return False
    return bool(probe_origin(f"{parts.scheme}://{parts.netloc}", timeout=_ENTRANCE_TIMEOUT).get("ok"))


def _remember_probe(home: Path, program: str, health: str, detail: str) -> None:
    path = _probe_file(home, program)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"health": health, "detail": detail}, ensure_ascii=False), encoding="utf-8")


def verify_host(home: Path, program: str, allow_real: bool = False) -> dict:
    """Call the configured hub once. Written config alone is not a successful connection."""
    if _real_user_home(Path(home)) and not allow_real:
        return {"ok": False, "refused": "real-home", "health": "", "detail": ""}
    if program not in plan(home) or not _points_at_hub(_host_config(home, program)):
        return {"ok": False, "refused": "unconfirmed", "health": "", "detail": ""}
    url, token = _endpoint(home, program)
    if not url or not token.startswith("rm_"):
        return {"ok": False, "refused": "unclaimed", "health": "通道未开", "detail": "尚未领到密钥。"}
    if not _channel_open(url):
        _remember_probe(home, program, "通道未开", "通道未开。")
        return {"ok": False, "refused": "", "health": "通道未开", "detail": "通道未开。"}
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "rogmem", "version": "0"}},
        }
    ).encode()
    request = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-Rogmem-Device": _device_id(home),
        },
        method="POST",
    )
    try:
        with open_direct(request, 5) as resp:
            raw = resp.read().decode(errors="replace")
            status = getattr(resp, "status", None) or resp.getcode()
    except urllib.error.HTTPError as exc:
        health, detail = ("异常", "密钥被拒绝。") if exc.code in {401, 403} else ("异常", f"调用失败：{exc.code}")
        _remember_probe(home, program, health, detail)
        return {"ok": False, "refused": "", "health": health, "detail": detail}
    except Exception:
        _remember_probe(home, program, "通道未开", "通道未开。")
        return {"ok": False, "refused": "", "health": "通道未开", "detail": "通道未开。"}
    if status == 200 and "result" in raw:
        _remember_probe(home, program, "正常", "最近一次调用成功。")
        return {"ok": True, "refused": "", "health": "正常", "detail": "最近一次调用成功。"}
    _remember_probe(home, program, "异常", "调用没有成功。")
    return {"ok": False, "refused": "", "health": "异常", "detail": "调用没有成功。"}


def connect_hosts(home: Path, base_url: str, programs: list[str] | None = None, confirm: bool = False, allow_real: bool = False) -> dict:
    """Point selected hosts at rogmem only after confirm. A real user home is not written unless allow_real."""
    if _real_user_home(Path(home)) and not allow_real:
        return {"ok": False, "refused": "real-home", "written": [], "text": "真实用户主目录不会写入。"}
    url = base_url.rstrip("/") + "/mcp"
    selected = chosen_hosts(home, programs)
    files = []
    for program in selected:
        path = _host_config(home, program)
        if path is None:
            continue
        files.append(f"{program}：{path} 将指向 {url}")
    text = "确认之前不会写入。"
    if files:
        text += "\n" + "\n".join(files)
    else:
        text += "\n没有可写入的宿主。"
    if selected:
        from rogmem.host_rules import rule_path

        text += "\n这次接入的宿主会写入全局规则，并安装同一份 hook。没有点名时，就是上面这些已发现的宿主。"
        for program in selected:
            path = rule_path(home, program)
            if path is not None:
                text += f"\n{program} 全局规则：{path}"
            text += f"\n{program} hook：会一起安装"
    if confirm is not True:
        return {"ok": False, "refused": "unconfirmed", "written": [], "text": text}
    written = write_configs(home, base_url, programs=selected, allow_real=allow_real)
    return {"ok": True, "refused": "", "written": written, "text": text}


def plan(home: Path) -> list[str]:
    return [item["program"] for item in detect_programs(home) if item["found"]]


def chosen_hosts(home: Path, programs: list[str] | None) -> list[str]:
    """No selection means every detected host this installer knows how to connect."""
    installed = plan(home)
    if programs is None:
        return installed
    return [program for program in programs if program in installed]


def install_global_rules(home: Path, allow_real: bool = False, programs: list[str] | None = None) -> list[str]:
    """Write the standing order for the selected hosts. An empty selection is every detected host."""
    if _real_user_home(Path(home)) and not allow_real:
        return []
    from rogmem.host_rules import write_host_rule

    written = []
    for program in chosen_hosts(home, programs):
        if write_host_rule(home, program) is not None:
            written.append(program)
    return written


def _hook_source() -> Path:
    return Path(__file__).resolve().parents[1] / "rogmem" / "host_hook.py"


def _install_hook_script(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_hook_source().read_text(encoding="utf-8"), encoding="utf-8")
    dest.chmod(0o755)


def _merge_cursor_hooks(path: Path, command: str) -> None:
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict):
            data = loaded
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    for event in ("sessionStart", "beforeSubmitPrompt", "afterAgentResponse"):
        rows = hooks.get(event)
        if not isinstance(rows, list):
            rows = []
        if not any(isinstance(item, dict) and item.get("command") == command for item in rows):
            rows.append({"command": command, "timeout": 3})
        hooks[event] = rows
    data["version"] = 1
    data["hooks"] = hooks
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _hook_lines(command: str) -> list[str]:
    return [f'    - command: "{command}"', "      timeout: 3"]


def _merge_hermes_hooks(path: Path, command: str) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if command in text:
        return
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == "hooks:" and not line.startswith(" ")), None)
    events = ("pre_llm_call", "post_llm_call")
    if start is None:
        block = ["hooks:"]
        for name in events:
            block.append(f"  {name}:")
            block.extend(_hook_lines(command))
        if text and not text.endswith("\n"):
            text += "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + ("\n" if text else "") + "\n".join(block) + "\n", encoding="utf-8")
        return
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith(" ")):
        end += 1
    section = lines[start + 1 : end]
    rebuilt: list[str] = []
    seen: set[str] = set()
    index = 0
    while index < len(section):
        line = section[index]
        rebuilt.append(line)
        index += 1
        name = next((item for item in events if line.strip() == f"{item}:"), "")
        if not name:
            continue
        seen.add(name)
        rebuilt.extend(_hook_lines(command))
    for name in events:
        if name in seen:
            continue
        rebuilt.append(f"  {name}:")
        rebuilt.extend(_hook_lines(command))
    lines[start + 1 : end] = rebuilt
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _merge_claude_hooks(path: Path, command: str, events: tuple[str, ...], version: int | None = None) -> None:
    """Codex and WorkBuddy share this registry: event, then a list of command groups."""
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict):
            data = loaded
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    for event in events:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            groups = []
        present = False
        for group in groups:
            inner = group.get("hooks") if isinstance(group, dict) else None
            if isinstance(inner, list) and any(
                isinstance(item, dict) and item.get("command") == command for item in inner
            ):
                present = True
                break
        if not present:
            groups.append(
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": command, "timeout": 3}],
                }
            )
        hooks[event] = groups
    if version is not None:
        data["version"] = version
    data["hooks"] = hooks
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _install_dsh_plugin(home: Path) -> None:
    """Copy the DSH plugin and record it on each live profile. Backup profiles stay untouched."""
    source = Path(__file__).resolve().parents[1] / "host" / "dsh"
    dest = home / ".rogmem" / "plugins" / "rogmem-dsh"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "index.js", "cordis.patch.yml"):
        (dest / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    _install_hook_script(dest / "rogmem.py")
    profiles = home / ".dsh" / "profiles"
    if not profiles.is_dir():
        return
    link = f"link:{dest}"
    for profile in profiles.iterdir():
        if not profile.is_dir() or "backup" in profile.name or profile.name == "node_modules":
            continue
        manifest_path = profile / "package.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(manifest, dict):
            continue
        dependencies = manifest.get("dependencies")
        if not isinstance(dependencies, dict):
            dependencies = {}
        dependencies["rogmem-dsh"] = link
        manifest["dependencies"] = dependencies
        dsh = manifest.get("dsh")
        if not isinstance(dsh, dict):
            dsh = {}
        profile_block = dsh.get("profile")
        if not isinstance(profile_block, dict):
            profile_block = {}
        bundles = profile_block.get("bundles")
        if not isinstance(bundles, list):
            bundles = []
        if "rogmem-dsh" not in bundles:
            bundles.append("rogmem-dsh")
        profile_block["bundles"] = bundles
        dsh["profile"] = profile_block
        manifest["dsh"] = dsh
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


_DSH_MCP_START = "# rogmem-mcp:start"
_DSH_MCP_END = "# rogmem-mcp:end"


def _held_dsh_token(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(loaded, dict):
        return ""
    token = _usable_token(str(loaded.get("token") or ""))
    if token:
        return token
    auth = str(loaded.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return _usable_token(auth.split(None, 1)[1])
    return ""


def _merge_dsh_mcp(home: Path, url: str, token: str, device_id: str) -> None:
    path = home / ".rogmem" / "hosts" / "DSH.json"
    token = _carry_token(token, _held_dsh_token(path))
    record = {"server": "rogmem", "url": url, "token": token, "device": device_id}
    if token:
        record["Authorization"] = f"Bearer {token}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        _DSH_MCP_START,
        "- insert:",
        "    - id: rogmem-mcp",
        '      name: "@deepseek-ai/dsh-mcp-client"',
        "      config:",
        "        serverName: rogmem",
        "        transport: streamable-http",
        f'        url: "{url}"',
        "        failOnStartupError: false",
    ]
    if token or device_id:
        lines.append("        headers:")
        if token:
            lines.append(f'          Authorization: "Bearer {token}"')
        if device_id:
            lines.append(f'          X-Rogmem-Device: "{device_id}"')
    lines.append(_DSH_MCP_END)
    block = "\n".join(lines)
    patch = home / ".dsh" / "cordis.patch.yml"
    existing = patch.read_text(encoding="utf-8") if patch.exists() else ""
    start = existing.find(_DSH_MCP_START)
    end = existing.find(_DSH_MCP_END)
    if start >= 0 and end > start:
        existing = existing[:start] + block + existing[end + len(_DSH_MCP_END) :]
    else:
        if existing and not existing.endswith("\n"):
            existing += "\n"
        existing = existing + ("\n" if existing.strip() else "") + block + "\n"
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text(existing, encoding="utf-8")


def install_host_hooks(home: Path, programs: list[str] | None = None, allow_real: bool = False) -> list[str]:
    """Copy one prepared hook into every selected host and register it in that host's own file."""
    if _real_user_home(Path(home)) and not allow_real:
        return []
    written = []
    for program in chosen_hosts(home, programs):
        if program == "Cursor":
            script = home / ".cursor" / "hooks" / "rogmem.py"
            _install_hook_script(script)
            _merge_cursor_hooks(home / ".cursor" / "hooks.json", str(script))
        elif program == "Trae":
            script = home / ".trae" / "hooks" / "rogmem.py"
            _install_hook_script(script)
            _merge_claude_hooks(
                home / ".trae" / "hooks.json",
                str(script),
                ("SessionStart", "UserPromptSubmit", "Stop"),
                version=1,
            )
        elif program == "Hermes":
            script = hermes_home(home) / "agent-hooks" / "rogmem.py"
            _install_hook_script(script)
            _merge_hermes_hooks(hermes_home(home) / "config.yaml", str(script))
        elif program == "Codex":
            script = home / ".codex" / "hooks" / "rogmem.py"
            _install_hook_script(script)
            _merge_claude_hooks(home / ".codex" / "hooks.json", str(script), ("UserPromptSubmit", "Stop"))
        elif program == "WorkBuddy":
            script = home / ".workbuddy" / "hooks" / "rogmem.py"
            _install_hook_script(script)
            _merge_claude_hooks(
                home / ".workbuddy" / "settings.json",
                str(script),
                ("SessionStart", "UserPromptSubmit", "Stop"),
            )
        elif program == "CodeBuddy":
            script = home / ".codebuddy" / "hooks" / "rogmem.py"
            _install_hook_script(script)
            _merge_claude_hooks(
                home / ".codebuddy" / "settings.json",
                str(script),
                ("SessionStart", "UserPromptSubmit", "Stop"),
            )
        elif program == "DSH":
            _install_dsh_plugin(home)
        else:
            continue
        written.append(program)
    return written


def refresh_host_bundle(home: Path, programs: list[str] | None = None, allow_real: bool = False) -> dict:
    """Rewrite the host-side plugin (global rules + hook) for installed hosts.

    This is the update path behind 宿主管理's 「更新宿主插件」. It only touches the two
    host-side artifacts and a local version marker: no key is minted, no existing config
    changes, no memory data moves. An empty selection is every detected supported host.
    """
    if _real_user_home(Path(home)) and not allow_real:
        return {"ok": False, "refused": "real-home", "rev": 0, "programs": [], "rules": [], "hooks": []}
    from rogmem.host_rules import HOST_BUNDLE_REV

    selected = chosen_hosts(home, programs)
    rules = install_global_rules(home, allow_real=True, programs=programs)
    hooks = install_host_hooks(home, programs=programs, allow_real=True)
    try:
        marker = Path(home) / ".rogmem" / "host-bundle.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({"rev": HOST_BUNDLE_REV, "programs": selected}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    return {
        "ok": True,
        "refused": "",
        "rev": HOST_BUNDLE_REV,
        "programs": selected,
        "rules": rules,
        "hooks": hooks,
    }


def installed_bundle_rev(home: Path) -> str:
    """The host-side plugin rev this computer last installed. Empty when it never refreshed."""
    try:
        loaded = json.loads((Path(home) / ".rogmem" / "host-bundle.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(loaded, dict):
        return ""
    return str(loaded.get("rev") or "")


def onboard_prompt(secret: str) -> str:
    """One prompt for any installed host. The secret enrolls every installed program on this computer."""
    from rogmem.onboard import onboard_prompt as build

    return build(secret)


def host_update_prompt() -> str:
    """One prompt that refreshes the client and re-installs the plugin on every supported host."""
    from rogmem.onboard import host_update_prompt as build

    return build()


def redeem_grant(home: Path, base_url: str, grant: str, program: str, post, allow_real: bool = False) -> dict:
    """Trade the one-time grant for this program's own key and write it. An existing key is left in place."""
    if _real_user_home(Path(home)) and not allow_real:
        return {"ok": False, "refused": "real-home", "status": "", "wrote": False}
    if program not in plan(home):
        return {"ok": False, "refused": "not-installed", "status": "", "wrote": False}
    if _has_token(_host_config(home, program)):
        return {"ok": True, "refused": "", "status": "已接入", "wrote": False}
    if not _points_at_hub(_host_config(home, program)):
        return {"ok": False, "refused": "unconfirmed", "status": "", "wrote": False}
    if post is None or not str(grant or "").strip():
        return {"ok": False, "refused": "no-target", "status": "", "wrote": False}
    from rogmem import crypto

    saved = _load_key(home, program)
    if saved is None:
        secret, public = crypto.new_keypair()
        saved = {"public": crypto.b64encode(public), "private": crypto.b64encode(secret)}
        path = _key_file(home, program)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(saved), encoding="utf-8")
    result = post(
        {
            "grant": str(grant).strip(),
            "device_id": _device_id(home),
            "device_name": device_display_name(home),
            "program": program,
            "pubkey": saved["public"],
        }
    )
    if not isinstance(result, dict):
        return {"ok": False, "refused": "unclaimed", "status": "", "wrote": False}
    if result.get("ok") and result.get("already"):
        if _has_token(_host_config(home, program)):
            return {"ok": True, "refused": "", "status": "已接入", "wrote": False}
        return {"ok": False, "refused": "unclaimed", "status": "", "wrote": False}
    if not result.get("ok") or not result.get("wrapped"):
        return {"ok": False, "refused": str(result.get("error") or "unclaimed"), "status": str(result.get("status") or ""), "wrote": False}
    token = crypto.open_seal(crypto.b64decode(saved["private"]), crypto.b64decode(result["wrapped"])).decode()
    if not token.startswith("rm_"):
        return {"ok": False, "refused": "unclaimed", "status": "", "wrote": False}
    written = write_configs(home, base_url, token, [program], allow_real=allow_real)
    if program not in written or not _has_token(_host_config(home, program)):
        return {"ok": False, "refused": "unclaimed", "status": "", "wrote": False}
    _key_file(home, program).unlink(missing_ok=True)
    return {"ok": True, "refused": "", "status": "启用", "wrote": True}


def connect_prompt(program: str) -> str:
    """Prompt the named host can follow after the user copies it from 添加宿主.

    This is the legacy single-host scan prompt, not a server-rendered ticket.
    The shared instruction carries ticket marks; here they become plain words so
    no unfilled placeholder ever reaches a user.
    """
    from rogmem.admission import ENTRANCES_MARK, TICKET_MARK
    from rogmem.onboard import HUB_MARK, host_client_instruction

    name = (program or "").strip()
    if name not in SUPPORTED:
        return ""
    instruction = (
        host_client_instruction()
        .replace(TICKET_MARK, "（宿主管理里「添加宿主」复制出来的接入票据）")
        .replace(ENTRANCES_MARK, "（票据里带的入口清单）")
        .replace(HUB_MARK, "（宿主列表里这份中枢的身份）")
    )
    return (
        f"请把这台电脑上的 {name} 接入 rogmem。用户已经在宿主管理里点过添加宿主，并复制了这段提示，就是同意只接入 {name}。\n\n"
        "不要寻找或安装 Windows、macOS 安装包。记忆只在极空间上，不要在这台电脑另存一份。\n\n"
        "先确认本机 Python 是 3.12 或更高。极空间远程访问要开着，NAS 上的 rogmem 固定听 18080，不要改这个端口。\n\n"
        "使用 python -m rogmem hosts 子命令。客户端里的库是 installer/rogmem_installer.py，不要再当独立安装器跑。\n"
        + instruction
        + f"用户这次点名的是 {name}。调用 connect_hosts、authorize_host、claim_host、verify_host 时传入 allow_real=True，"
        "因为这是用户刚刚确认要接入的这台电脑。只处理点名的宿主；用户没有点名时才接入全部已发现且受支持的宿主。\n"
        f"connect_hosts 把 {name} 指到记忆中枢，写入它的全局规则，并安装客户端里的同一份 hook。"
        "Cursor 登记到 .cursor/hooks.json，Trae 登记到 .trae/hooks.json，Hermes 登记到 config.yaml 的 hooks，Codex 登记到 .codex/hooks.json，WorkBuddy 登记到 .workbuddy/settings.json 的 hooks，CodeBuddy 登记到 .codebuddy/settings.json 的 hooks。DSH 安装 rogmem-dsh 插件，并把 MCP 写进 .dsh/cordis.patch.yml。"
        "hook 调用失败也不能挡住宿主继续工作。绝对禁止改写 Hermes 的 MEMORY.md 和 USER.md。"
        f"authorize_host 为 {name} 申请授权。"
        "设备名用这台电脑的主机名，不要写成「本机」。\n"
        "申请发出后把核对码告诉用户，请用户在宿主列表里找到这一行，点操作列的「通过授权」。\n"
        f"用户放行后，claim_host 领取密钥并写入 {name}。再用 verify_host 做一次真实调用。调用成功才算接入完成。\n"
        "不要把密钥原文写进对话。不要删除 NAS 上的 rogmem 数据。"
    )


def viewer_identity(home: Path | None) -> dict:
    """Read this computer's saved identity. Do not create files just to answer."""
    from rogmem.device_identity import display_device_name

    if home is None:
        return {"device_id": "", "device_name": ""}
    root = Path(home)
    id_path = root / ".rogmem" / "device_id"
    name_path = root / ".rogmem" / "device_name"
    device_id = id_path.read_text(encoding="utf-8").strip() if id_path.is_file() else ""
    raw = name_path.read_text(encoding="utf-8").strip() if name_path.is_file() else ""
    device_name = display_device_name(raw) if raw else ""
    if device_name in {"未命名设备", "本机"} or device_name.lower() in {"localhost", "local"}:
        device_name = ""
    return {"device_id": device_id, "device_name": device_name}


def local_host_scan(home: Path | None) -> dict:
    """Scan the computer running the installer. Unconnected hosts get a copyable prompt."""
    identity = viewer_identity(home)
    hosts = []
    if home is not None:
        for row in inspect_hosts(Path(home), allow_real=True):
            installed = bool(row.get("found"))
            connected = row.get("access") == "已接入"
            if not installed:
                status = "未安装"
            elif connected:
                status = "已接入"
            else:
                status = "未接入"
            program = str(row.get("program") or "")
            hosts.append(
                {
                    "program": program,
                    "installed": installed,
                    "connected": connected,
                    "status": status,
                    "importable": row.get("importable"),
                    "prompt": connect_prompt(program) if status == "未接入" else "",
                }
            )
    return {
        "ok": True,
        "device_id": identity["device_id"],
        "device_name": identity["device_name"],
        "hosts": hosts,
    }


_HOST_SCAN_TTL = 60
_host_scan_cache: dict[str, tuple[float, dict]] = {}
_host_scan_lock = threading.Lock()


def cached_local_host_scan(home: Path | None) -> dict:
    """The page asks often. Recounting every transcript on each ask would stall the door."""
    key = "" if home is None else str(Path(home).resolve())
    now = time.monotonic()
    with _host_scan_lock:
        hit = _host_scan_cache.get(key)
        if hit is not None and now - hit[0] < _HOST_SCAN_TTL:
            return hit[1]
        payload = local_host_scan(home)
        _host_scan_cache[key] = (time.monotonic(), payload)
        return payload


def _viewer_headers(home: Path | None) -> dict[str, str]:
    from rogmem.device_identity import header_device_name

    identity = viewer_identity(home)
    headers = {}
    if identity["device_id"]:
        headers["X-Rogmem-Device"] = identity["device_id"]
    if identity["device_name"]:
        headers["X-Rogmem-Device-Name"] = header_device_name(identity["device_name"])
    return headers


# Request hop-by-hop fields are rebuilt. The hub's response is not.
_DROP_REQUEST_HEADERS = {
    "host",
    "connection",
    "proxy-connection",
    "keep-alive",
    "te",
    "trailers",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


def _header_bytes(key: str, value: str) -> bytes:
    text = f"{key}: {value}"
    try:
        return text.encode("latin-1")
    except UnicodeEncodeError:
        return text.encode("utf-8")


class _Proxy(BaseHTTPRequestHandler):
    target = 0
    home: Path | None = None
    origin = ""
    hub = ""
    # The entrance this process was given. A binding found later is only a cache.
    recorded_origin = ""
    recorded_target = 0
    cached_origin = ""

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in {"/client/whoami", "/client/hosts"}:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _forward(self):
        path = self.path.split("?", 1)[0]
        if path == "/client/whoami":
            self._send_json(viewer_identity(self.home))
            return
        if path == "/client/hosts":
            self._send_json(cached_local_host_scan(self.home))
            return
        if path == "/client/open-browser":
            from urllib.parse import parse_qs, urlsplit

            target = (parse_qs(urlsplit(self.path).query).get("url") or [""])[0]
            open_system_browser(target)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        data = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items()}
        present = {key.lower() for key in headers}
        for key, value in _viewer_headers(self.home).items():
            if key.lower() not in present:
                headers[key] = value
        cookie = os.environ.get("ROGMEM_HUB_COOKIE", "").strip()
        if cookie and "cookie" not in present:
            headers["Cookie"] = cookie
        self._pipe(data, headers)

    def _request_bytes(self, body: bytes | None, headers: dict[str, str], host_header: str) -> bytes:
        lines = [f"{self.command} {self.path} HTTP/1.1".encode("ascii"), f"Host: {host_header}".encode("ascii"), b"Connection: close"]
        for key, value in headers.items():
            if key.lower() in _DROP_REQUEST_HEADERS:
                continue
            lines.append(_header_bytes(key, value))
        if body is not None:
            lines.append(f"Content-Length: {len(body)}".encode("ascii"))
        raw = b"\r\n".join(lines) + b"\r\n\r\n"
        if body:
            raw += body
        return raw

    def _fail(self, status: int, exc: BaseException) -> None:
        msg = str(exc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)

    def _places(self):
        """Where this hub may answer. A cached binding is tried first and is not the entrance."""
        cls = type(self)
        cached = str(getattr(cls, "cached_origin", "") or "").strip()
        recorded = str(getattr(cls, "recorded_origin", "") or "").strip()
        if cached:
            yield cached, 0, True
        if recorded and recorded.rstrip("/") != cached.rstrip("/"):
            yield recorded, 0, False
        elif not recorded:
            yield "", int(getattr(cls, "recorded_target", 0) or 0), False

    def _forget_cache(self, origin: str) -> None:
        cls = type(self)
        if str(getattr(cls, "cached_origin", "") or "").strip() == origin:
            cls.cached_origin = ""

    def _dial(self, origin: str, target: int):
        """Open one candidate. Connecting uses the entrance budget so a silent hop cannot hide the next."""
        import socket
        from urllib.parse import urlsplit

        if not origin:
            sock = socket.create_connection(("127.0.0.1", int(target)), timeout=_ENTRANCE_TIMEOUT)
            return sock, f"127.0.0.1:{int(target)}"
        parsed = urlsplit(origin)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        sock = socket.create_connection((host, port), timeout=_ENTRANCE_TIMEOUT)
        if parsed.scheme == "https":
            import ssl

            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        if (parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80):
            visible = host
        else:
            visible = f"{host}:{port}"
        return sock, visible

    def _another_binding(self) -> str:
        """A local listener that is this same hub. It does not replace the entrance."""
        identity = (getattr(self, "hub", "") or "").strip()
        if not identity:
            return ""
        own = 0
        server = getattr(self, "server", None)
        if server is not None:
            try:
                own = int(server.server_address[1])
            except (TypeError, ValueError, OSError):
                own = 0
        try:
            return find_binding(identity, allow_door=False, skip={own}) or ""
        except Exception:
            return ""

    def _open_upstream(self):
        """Dial this hub. A binding that once answered is a cache; a failed dial leaves the entrance in place."""
        last: BaseException | None = None
        for origin, target, cached in self._places():
            try:
                return self._dial(origin, target)
            except Exception as exc:
                last = exc
                if cached:
                    self._forget_cache(origin)
        found = self._another_binding()
        if found:
            try:
                opened = self._dial(found, 0)
            except Exception:
                if last is not None:
                    raise last
                raise
            type(self).cached_origin = found
            return opened
        if last is not None:
            raise last
        raise OSError("没有找到这份中枢")

    def _copy_hop(self, origin: str, target: int, cached: bool, body: bytes | None, headers: dict[str, str]) -> None:
        """Stream one hop. A shortcut that accepts and stays silent is not a live door."""
        upstream, host_header = self._dial(origin, target)
        # The remembered entrance may think before its first byte. A cached
        # shortcut has to show a byte inside the entrance budget, or it is
        # the same as a hop that never connected.
        upstream.settimeout(_ENTRANCE_TIMEOUT if cached else 120)
        try:
            upstream.sendall(self._request_bytes(body, headers, host_header))
            first = upstream.recv(65536)
        except Exception:
            upstream.close()
            if cached:
                self._forget_cache(origin)
            raise
        if not first:
            upstream.close()
            if cached:
                self._forget_cache(origin)
            raise TimeoutError("silent hop")
        if cached:
            upstream.settimeout(120)
        try:
            self.wfile.write(first)
            self.wfile.flush()
            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:
            upstream.close()
            self.close_connection = True

    def _pipe(self, body: bytes | None, headers: dict[str, str]) -> None:
        """Copy the hub's response bytes. This machine only adds its own request headers."""
        last: BaseException | None = None
        for origin, target, cached in self._places():
            try:
                self._copy_hop(origin, target, cached, body, headers)
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
        found = self._another_binding()
        if found:
            try:
                self._copy_hop(found, 0, True, body, headers)
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
        self._fail(502, last or OSError("没有找到这份中枢"))

    def do_GET(self):  # noqa: N802
        self._forward()

    def do_POST(self):  # noqa: N802
        self._forward()

    def log_message(self, fmt, *args):
        return


def open_system_browser(url: str) -> bool:
    from urllib.parse import urlsplit

    parts = urlsplit(url or "")
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return False
    import webbrowser

    return bool(webbrowser.open(url))


def forward(listen: int, target: int, home: Path | None = None, hub: str = "") -> ThreadingHTTPServer:
    handler = type(
        "BoundProxy",
        (_Proxy,),
        {
            "target": target,
            "origin": "",
            "hub": (hub or "").strip(),
            "home": None if home is None else Path(home),
            "recorded_origin": "",
            "recorded_target": int(target),
            "cached_origin": "",
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", listen), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _loopback(origin: str) -> bool:
    """A loopback port lives only as long as the tunnel on this machine."""
    from urllib.parse import urlsplit

    return _loopback_host(urlsplit(origin or "").hostname or "")


def _door_origin(origin: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit((origin or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("门口只记录中枢地址")
    return parsed.geturl()


def door_stands_alone(origin: str) -> bool:
    """The login session can own the door only when the hub is not this machine's current tunnel."""
    if _loopback(origin):
        return False
    return bool(probe_origin(origin, "").get("ok"))


def _channel_command(executable: str, origin: str, hub: str = "") -> list[str]:
    command = [executable, "-m", "rogmem", "channel", "--session", "--origin", _door_origin(origin)]
    identity = (hub or "").strip()
    if identity:
        command.extend(["--hub", identity])
    return command


def session_service(origin: str, executable: str, workdir: str, hub: str = "") -> dict:
    """The fixed local door belongs to the login session. A closed terminal must not take it down."""
    return {
        "Label": "com.rogmem.channel",
        "ProgramArguments": _channel_command(executable, origin, hub),
        "WorkingDirectory": workdir,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
    }


def session_unit(system: str, origin: str, executable: str, workdir: str, hub: str = "") -> dict:
    """One door for the login session. The marker stops that process from installing another."""
    import shlex

    command = _channel_command(executable, origin, hub)
    if system == "Darwin":
        return {"kind": "launchd", "plist": session_service(origin, executable, workdir, hub)}
    if system == "Linux":
        text = "\n".join(
            [
                "[Unit]",
                "Description=rogmem local door",
                "[Service]",
                "ExecStart=" + " ".join(shlex.quote(part) for part in command),
                "WorkingDirectory=" + shlex.quote(workdir),
                "Restart=on-failure",
                "RestartSec=10",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        )
        return {"kind": "systemd", "path": ".config/systemd/user/rogmem-channel.service", "text": text}
    if system == "Windows":
        script = "\r\n".join(["@echo off", " ".join(f'"{part}"' for part in command), ""])
        return {"kind": "task", "name": "rogmem-channel", "path": ".rogmem/channel-service.cmd", "text": script}
    return {"kind": ""}


def write_session_unit(home: Path, unit: dict) -> Path:
    root = Path(home)
    if unit.get("kind") == "launchd":
        return write_session_service(root, unit["plist"])
    if unit.get("kind") in {"systemd", "task"}:
        path = root / unit["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(unit["text"], encoding="utf-8")
        return path
    raise ValueError("没有可以交给登录会话的门口")


def launchd_holding(text: str) -> bool:
    """The job is holding when its own state is running and it still has a pid."""
    state = pid = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not state and stripped.startswith("state = "):
            state = stripped.split(" = ", 1)[1].strip()
        elif not pid and stripped.startswith("pid = "):
            pid = stripped.split(" = ", 1)[1].strip()
    return state == "running" and pid.isdigit()


def _session_holding(unit: dict) -> bool:
    import os
    import subprocess

    if unit.get("kind") == "launchd":
        label = unit["plist"]["Label"]
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and launchd_holding(result.stdout)
    if unit.get("kind") == "systemd":
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "rogmem-channel.service"],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "active"
    if unit.get("kind") == "task":
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", unit["name"], "/FO", "LIST"],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and "Running" in result.stdout
    return False


def start_session_unit(unit: dict, path: Path) -> bool:
    """Spawn the session door and wait until that process is the one still running."""
    import subprocess

    if unit.get("kind") == "launchd":
        spawned = start_session_service(path)
    elif unit.get("kind") == "systemd":
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
        spawned = (
            subprocess.run(
                ["systemctl", "--user", "enable", "--now", "rogmem-channel.service"],
                check=False,
                capture_output=True,
            ).returncode
            == 0
        )
    elif unit.get("kind") == "task":
        task = str(path)
        if " " in task:
            task = f'"{task}"'
        created = subprocess.run(
            ["schtasks", "/Create", "/TN", unit["name"], "/SC", "ONLOGON", "/RL", "LIMITED", "/F", "/TR", task],
            check=False,
            capture_output=True,
        )
        spawned = created.returncode == 0 and subprocess.run(
            ["schtasks", "/Run", "/TN", unit["name"]],
            check=False,
            capture_output=True,
        ).returncode == 0
    else:
        return False
    if not spawned:
        return False
    door = f"http://127.0.0.1:{FIXED_FORWARD_PORT}"
    for _ in range(40):
        if _session_holding(unit) and probe_origin(door, "").get("ok"):
            return True
        time.sleep(0.2)
    return False


def write_session_service(home: Path, spec: dict) -> Path:
    import plistlib

    root = Path(home)
    log = root / "Library" / "Logs" / "rogmem-channel.log"
    recorded = dict(spec)
    recorded["StandardOutPath"] = str(log)
    recorded["StandardErrorPath"] = str(log)
    path = root / "Library" / "LaunchAgents" / f"{spec['Label']}.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(recorded, sort_keys=False))
    return path


def start_session_service(plist: Path) -> bool:
    import os
    import subprocess

    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist)], check=False, capture_output=True)
    result = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=False, capture_output=True)
    return result.returncode == 0


def forward_origin(listen: int, origin: str, home: Path | None = None, hub: str = "") -> ThreadingHTTPServer:
    """Present a remote hub as the local door. The browser session rides along when set."""
    handler = type(
        "BoundProxy",
        (_Proxy,),
        {
            "target": 0,
            "origin": origin.strip().rstrip("/"),
            "hub": (hub or "").strip(),
            "home": None if home is None else Path(home),
            "recorded_origin": origin.strip().rstrip("/"),
            "recorded_target": 0,
            "cached_origin": "",
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", listen), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


_CLOSED_PAGE = "<!doctype html><title>rogmem</title><p>极空间远程访问没开。不会在这台电脑上另存一份记忆。</p>"


class _Closed(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = _CLOSED_PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def _closed_server(listen: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", listen), _Closed)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def browser_command(entry: str) -> list[str]:
    """Open the admin in an app window. The user does not type an address."""
    system = platform.system()
    if system == "Windows":
        return ["cmd", "/c", "start", "", "chrome", f"--app={entry}"]
    if system == "Linux":
        return ["google-chrome", f"--app={entry}"]
    return ["open", "-na", "Google Chrome", "--args", f"--app={entry}"]


def client_launch(listen: int = FIXED_FORWARD_PORT, start: int = 10000, end: int = 10050, show: bool = False, home: Path | None = None) -> dict:
    """Open the existing admin through a local window. The user does not type an address."""
    root = Path.home() if home is None else Path(home)
    found = find_rogmem(start, end)
    if found is None:
        return {
            "ok": False,
            "health": "通道未开",
            "text": "极空间远程访问没开。不会在这台电脑上另存一份记忆。",
            "entry": f"http://127.0.0.1:{listen}/",
            "command": [],
            "server": _closed_server(listen),
        }
    entry = f"http://127.0.0.1:{listen}/admin/graph"
    command = browser_command(entry)
    if show:
        import subprocess

        subprocess.Popen(command)
    return {
        "ok": True,
        "health": "正常",
        "text": "",
        "entry": entry,
        "command": command,
        "server": forward(listen, found, home=root),
    }


def _visible_text(content) -> str | None:
    """Visible user or assistant text. Summaries, tools, and reasoning are not turns."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            if part.strip():
                parts.append(part.strip())
            continue
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type") or "")
        if kind in {"summary", "reasoning", "tool_use", "tool_result", "function_call", "input_image"}:
            continue
        value = part.get("text") or ""
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts).strip()


def _event_at(value) -> str:
    if isinstance(value, bool) or value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000.0 if abs(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    return str(value)


def _turn(
    session_id: str,
    seq: int,
    role: str,
    text: str,
    event_at: str,
    project: str = "",
    event_basis: str = "",
) -> dict:
    del seq
    spoken = "user" if role == "user" else "assistant"
    stable = stable_seq(session_id, spoken, text)
    item = {
        "session_id": session_id,
        "turn_id": f"{session_id}:{stable}",
        "seq": stable,
        "role": spoken,
        "text": text,
        "event_at": event_at,
        "event_basis": event_basis,
    }
    if project:
        item["project"] = project
    return item


def stable_turn(session_id: str, role: str, text: str, event_at: str = "") -> dict:
    """Public identity of one visible utterance. History and a later turn share it."""
    return _turn(session_id, 0, role, text, event_at)


_DELEGATED_SOURCES = frozenset({"cron", "subagent"})


def _read_hermes_ledger(path: Path) -> dict | None:
    """Hermes' own session ledger. A JSON export is not the transcript when this exists."""
    if not path.is_file():
        return None
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "messages" not in tables or "sessions" not in tables:
            return None
        msg_cols = {row[1] for row in con.execute("PRAGMA table_info(messages)")}
        sess_cols = {row[1] for row in con.execute("PRAGMA table_info(sessions)")}
        if not {"session_id", "role", "content", "timestamp"} <= msg_cols or "id" not in sess_cols:
            return None
        active_sql = "m.active" if "active" in msg_cols else "1"
        parent_sql = "s.parent_session_id" if "parent_session_id" in sess_cols else "''"
        source_sql = "s.source" if "source" in sess_cols else "''"
        cwd_sql = "s.cwd" if "cwd" in sess_cols else "''"
        repo_sql = "s.git_repo_root" if "git_repo_root" in sess_cols else "''"
        rows = con.execute(
            f"""SELECT m.session_id, m.role, m.content, m.timestamp,
                       {active_sql} AS active, {parent_sql} AS parent_session_id, {source_sql} AS source,
                       {cwd_sql} AS cwd, {repo_sql} AS git_repo_root
                FROM messages m LEFT JOIN sessions s ON s.id = m.session_id
                ORDER BY m.timestamp, m.session_id"""
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    turns: list[dict] = []
    skipped = 0
    projects: dict[tuple[str, str], str] = {}
    for session_id, role, content, timestamp, active, parent, source, cwd, recorded in rows:
        if int(active or 0) == 0 or str(parent or "").strip() or str(source or "") in _DELEGATED_SOURCES:
            skipped += 1
            continue
        if str(role or "") not in {"user", "assistant"}:
            skipped += 1
            continue
        visible = _visible_text(content)
        event_at = _event_at(timestamp)
        if not visible or not event_at:
            skipped += 1
            continue
        key = (str(cwd or ""), str(recorded or ""))
        if key not in projects:
            projects[key] = work_repository(key[0], key[1])
        turns.append(
            _turn(str(session_id or "Hermes"), 0, str(role), visible, event_at, projects[key], event_basis="utterance")
        )
    return {"turns": turns, "skipped": skipped}


_CLOCK_KEYS = ("timestamp", "event_at", "createdAt", "created_at")
_NOT_A_TURN = frozenset(
    {
        "session_meta",
        "turn_context",
        "event_msg",
        "reasoning",
        "function_call",
        "function_call_result",
        "file-history-snapshot",
        "ai-title",
        "custom-title",
        "summary",
        "compaction",
        "subagent",
        "subagent_message",
        "daily",
        "daily_note",
        "journal",
    }
)


def _record_clock(item: dict) -> str:
    """When this record was produced. Not when a file was copied or uploaded."""
    if not isinstance(item, dict):
        return ""
    nodes = [item]
    for key in ("message", "payload"):
        nested = item.get(key)
        if isinstance(nested, dict):
            nodes.append(nested)
    for node in nodes:
        for key in _CLOCK_KEYS:
            stamped = _event_at(node.get(key))
            if stamped:
                return stamped
    return ""


def _turn_clock(item: dict) -> str:
    """A clock on the utterance itself. A session header does not date the sentences."""
    if not isinstance(item, dict):
        return ""
    if str(item.get("type") or "") in _NOT_A_TURN or _subagent_record(item):
        return ""
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else None
    role = str(item.get("role") or "")
    if payload is not None:
        if str(payload.get("type") or "") != "message":
            return ""
        role = str(payload.get("role") or "")
    if item.get("compressed") or item.get("summary") is True or role in {"system", "developer", "tool"}:
        return ""
    if role not in {"user", "assistant"}:
        return ""
    return _record_clock(item)


def _file_clock(path: Path) -> str:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


# A transcript bigger than this is left out of one pass. Ordinary agent sessions sit well under it.
_SESSION_BYTES = 32_000_000


def extract_turns(program: str, text: str, *, file_at: str = "", session_id: str = "", keep: bool = True) -> dict:
    """Split a host transcript into original turns. Compressed summaries are counted as skipped.

    A visible utterance needs a time. If this material stamps its records, a record
    without a clock is not a turn. If none of the records carry a clock, the file's
    own time places every visible utterance.
    """
    turns: list[dict] = []
    skipped = 0
    raw = text.strip()
    if not raw:
        return {"program": program, "turns": [], "skipped": 0, "count": 0}

    records: list[dict] = []
    if raw.startswith("{") and "\n{" not in raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
            session_id = str(payload.get("session_id") or program)
            for msg in payload["messages"]:
                if isinstance(msg, dict):
                    msg = dict(msg)
                    msg.setdefault("session_id", session_id)
                    records.append(msg)
        elif isinstance(payload, dict):
            records.append(payload)
    else:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if isinstance(item, dict):
                records.append(item)

    clocks = any(_turn_clock(item) for item in records)
    fallback = "" if clocks else file_at
    counted = 0
    for item in records:
        kind = str(item.get("type") or "")
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else None
        if kind in _NOT_A_TURN:
            skipped += 1
            continue
        if _subagent_record(item):
            skipped += 1
            continue
        role = str(item.get("role") or "")
        content = item.get("content")
        named = str(item.get("session_id") or item.get("sessionId") or "").strip()
        if payload is not None:
            if payload.get("type") != "message":
                skipped += 1
                continue
            role = str(payload.get("role") or "")
            content = payload.get("content")
        own = _record_clock(item)
        if own:
            event_at, basis = own, "utterance"
        else:
            event_at, basis = fallback, ("file" if fallback else "")
        sid = named or session_id or program
        if item.get("compressed") or item.get("summary") is True or role in {"system", "developer", "tool"}:
            skipped += 1
            continue
        message = item.get("message")
        if isinstance(message, dict) and content is None:
            content = message.get("content")
        if role not in {"user", "assistant"}:
            skipped += 1
            continue
        if not event_at:
            skipped += 1
            continue
        visible = _visible_text(content)
        if not visible:
            skipped += 1
            continue
        counted += 1
        if keep:
            turns.append(_turn(sid, counted, role, visible, event_at, event_basis=basis))
    return {"program": program, "turns": turns, "skipped": skipped, "count": counted}


def _real_user_home(home: Path) -> bool:
    try:
        return home.expanduser().resolve() == Path.home().resolve()
    except OSError:
        return False


def _subagent_record(item: dict) -> bool:
    role = str(item.get("role") or "").lower()
    if role == "subagent" or item.get("subagent") is True or item.get("is_subagent") is True or item.get("isSubagent") is True:
        return True
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else None
    if payload is None:
        return False
    return payload.get("subagent") is True or str(payload.get("role") or "").lower() == "subagent"


_HISTORY_SKIP_PARTS = {"dreaming", "subagents", "daily", "daily-notes"}
_DAILY_NOTE_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[._-].*)?\.(?:json|jsonl|md)$", re.IGNORECASE)


def _skip_history_file(file: Path) -> bool:
    if file.name in {"MEMORY.md", "USER.md"}:
        return True
    if any(part in _HISTORY_SKIP_PARTS for part in file.parts):
        return True
    return _DAILY_NOTE_NAME.match(file.name) is not None


def _session_files(program: str, root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found: list[Path] = []
    seen: set[Path] = set()
    if program == "Hermes":
        candidates = list(root.glob("session_*.json")) + list(root.glob("*.json"))
    else:
        candidates = list(root.rglob("*.jsonl"))
    for file in candidates:
        if not file.is_file():
            continue
        if _skip_history_file(file):
            continue
        try:
            key = file.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        found.append(file)
    return found


def _history_roots(home: Path) -> dict[str, list[Path]]:
    return {
        "Cursor": [*_cursor_app_dirs(home), home / ".cursor" / "projects"],
        "Hermes": [hermes_home(home) / "sessions"],
        "Codex": [home / ".codex" / "sessions"],
        "WorkBuddy": [home / ".workbuddy" / "projects"],
        "CodeBuddy": [home / ".codebuddy" / "projects"],
        "DSH": [home / ".dsh" / "sessions"],
    }


def _stamp_turns(program: str, parsed_turns: list[dict]) -> list[dict]:
    stamped = []
    for turn in parsed_turns:
        item = dict(turn)
        item["program"] = program
        stamped.append(item)
    return stamped


def read_program_history(home: Path, program: str, allow_real: bool = False):
    """Yield one session at a time, newest file first.

    The caller can store a session before the next file is read. A real home
    is not read unless allow_real. Other programs' directories are not opened.
    """
    home = Path(home)
    if program not in SUPPORTED:
        return
    if _real_user_home(home) and not allow_real:
        return
    if program == "Hermes":
        ledger = _read_hermes_ledger(hermes_home(home) / "state.db")
        if ledger is not None:
            turns = _stamp_turns(program, ledger.get("turns") or [])
            if turns:
                yield turns
            return
    files: list[Path] = []
    for root in _history_roots(home).get(program) or []:
        files.extend(_session_files(program, root))

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    files.sort(key=_mtime, reverse=True)
    for file in files:
        try:
            if file.stat().st_size > _SESSION_BYTES:
                continue
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parsed = extract_turns(
            program,
            text,
            file_at=_file_clock(file),
            session_id=file.stem,
            keep=True,
        )
        turns = _stamp_turns(program, parsed.get("turns") or [])
        if turns:
            yield turns


def collect_history(
    home: Path,
    allow_real: bool = False,
    keep: bool = True,
    programs: "list[str] | None" = None,
) -> dict:
    """Read original turns from Cursor, Hermes, Codex, and WorkBuddy sessions under home.

    评审 R2 P2-5：当 `programs` 给定时，**只扫描**这些 program 对应的目录与账本，
    不触达其他宿主的目录。默认 `None` = 旧全局语义（CLI history preview/upload 仍沿用）。
    """
    home = Path(home)
    if _real_user_home(home) and not allow_real:
        return {"programs": [], "turns": [], "skipped": 0, "files": 0, "refused": "real-home"}
    allowed = set(programs) if programs else None
    roots = {
        name: paths
        for name, paths in _history_roots(home).items()
        if allowed is None or name in allowed
    }
    turns: list[dict] = []
    skipped = 0
    files = 0
    programs: list[str] = []
    by_program: list[dict] = []
    for program, paths in roots.items():
        program_turns = 0
        program_skipped = 0
        program_files = 0
        ledger_done = False
        if program == "Hermes":
            ledger = _read_hermes_ledger(hermes_home(home) / "state.db")
            if ledger is not None:
                ledger_done = True
                program_files += 1
                if keep:
                    for turn in ledger["turns"]:
                        stamped = dict(turn)
                        stamped["program"] = program
                        turns.append(stamped)
                program_turns += len(ledger["turns"])
                program_skipped += int(ledger["skipped"])
        if ledger_done:
            paths = []
        for root in paths:
            for file in _session_files(program, root):
                if file.stat().st_size > _SESSION_BYTES:
                    program_skipped += 1
                    continue
                program_files += 1
                parsed = extract_turns(
                    program,
                    file.read_text(encoding="utf-8", errors="replace"),
                    file_at=_file_clock(file),
                    session_id=file.stem,
                    keep=keep,
                )
                if keep:
                    for turn in parsed["turns"]:
                        stamped = dict(turn)
                        stamped["program"] = program
                        turns.append(stamped)
                program_turns += int(parsed["count"])
                program_skipped += int(parsed["skipped"])
        if program_files:
            programs.append(program)
            by_program.append(
                {"program": program, "importable": program_turns, "skipped": program_skipped, "files": program_files}
            )
            files += program_files
            skipped += program_skipped
    return {
        "programs": programs,
        "turns": turns,
        "skipped": skipped,
        "files": files,
        "by_program": by_program,
        "refused": "",
    }


def import_host_history(home: Path, program: str, confirm: bool = False, post=None, allow_real: bool = False) -> dict:
    """Import one host's turns only after that row is confirmed.

    A real user home is not read unless allow_real. A confirmed whole-home import uses import_history.
    """
    if _real_user_home(Path(home)) and not allow_real:
        return {"ok": False, "refused": "real-home", "posted": 0}
    if program not in SUPPORTED:
        return {"ok": False, "refused": "unknown", "posted": 0}
    if confirm is not True:
        return {"ok": False, "refused": "unconfirmed", "posted": 0}
    from rogmem.exchange import order_history_newest_first

    turns = order_history_newest_first(
        [turn for turn in (collect_history(home, allow_real=True).get("turns") or []) if turn.get("program") == program]
    )
    if post is None:
        return {"ok": False, "refused": "no-target", "posted": 0}
    return {"ok": True, "refused": "", "posted": len(turns), "result": post(turns)}


def import_history(home: Path, confirm: bool = False, post=None) -> dict:
    """Post turns only after an explicit confirm. An unconfirmed real home is not read."""
    if _real_user_home(Path(home)) and confirm is not True:
        return {"ok": False, "refused": "real-home", "posted": 0}
    if confirm is not True:
        return {"ok": False, "refused": "unconfirmed", "posted": 0}
    from rogmem.exchange import order_history_newest_first

    turns = order_history_newest_first(collect_history(home, allow_real=True).get("turns") or [])
    if post is None:
        return {"ok": False, "refused": "no-target", "posted": 0}
    return {"ok": True, "refused": "", "posted": len(turns), "result": post(turns)}


def confirm_summary(home: Path) -> str:
    """Chinese counts for a confirm screen. Does not import or write configs."""
    preview = history_preview(home)
    if preview.get("refused") == "real-home":
        return "真实用户主目录不会导入。"
    lines = ["这些原文会送到极空间。确认之前不会导入。"]
    for item in preview.get("programs") or []:
        lines.append(f"{item['program']}：可导入 {item['importable']} 条，跳过 {item['skipped']} 条。")
    if len(lines) == 1:
        lines.append("没有找到可导入的会话。")
    return "\n".join(lines)


def history_preview(home: Path, allow_real: bool = False) -> dict:
    """Count importable and skipped turns. Do not import them or write configs."""
    collected = collect_history(home, allow_real=allow_real, keep=False)
    programs = collected.get("by_program") or []
    return {
        "refused": collected.get("refused") or "",
        "programs": programs,
        "importable": sum(int(item.get("importable") or 0) for item in programs),
        "skipped": collected.get("skipped") or 0,
    }


def _merge_mcp_json(path: Path, url: str, token: str = "", device_id: str = "") -> None:
    data: dict = {}
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
        if isinstance(loaded, dict):
            data = loaded
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    current = servers.get("rogmem")
    if not isinstance(current, dict):
        current = {}
    current["url"] = url
    if token:
        headers = current.get("headers")
        if not isinstance(headers, dict):
            headers = {}
        headers["Authorization"] = f"Bearer {token}"
        if device_id:
            headers["X-Rogmem-Device"] = device_id
        current["headers"] = headers
    servers["rogmem"] = current
    data["mcpServers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _toml_token(line: str) -> str:
    _, _, value = line.strip().partition("=")
    return _usable_token(value.strip().strip("\"'"))


def _merge_toml_mcp(path: Path, url: str, token: str = "") -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    header = "[mcp_servers.rogmem]"
    supplied = token
    held = ""
    if header not in text:
        token = _carry_token(supplied, held)
        extra = [f'url = "{url}"']
        if token:
            extra.append(f'bearer_token = "{token}"')
        block = header + "\n" + "\n".join(extra) + "\n"
        if text and not text.endswith("\n"):
            text += "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + ("\n" if text else "") + block, encoding="utf-8")
        return
    lines = text.splitlines()
    out = []
    index = 0
    while index < len(lines):
        if lines[index].strip() != header:
            out.append(lines[index])
            index += 1
            continue
        out.append(header)
        extra_at = len(out)
        index += 1
        while index < len(lines) and not lines[index].startswith("["):
            stripped = lines[index].strip()
            if stripped.startswith("bearer_token "):
                held = held or _toml_token(stripped)
            if stripped.startswith("url ") or stripped.startswith("bearer_token "):
                index += 1
                continue
            out.append(lines[index])
            index += 1
        token = _carry_token(supplied, held)
        extra = [f'url = "{url}"']
        if token:
            extra.append(f'bearer_token = "{token}"')
        for offset, line in enumerate(extra):
            out.insert(extra_at + offset, line)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _yaml_token(lines: list[str]) -> str:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("bearer_token:"):
            return _usable_token(stripped.split(":", 1)[1].strip().strip("\"'"))
    return ""


def _yaml_entry(url: str, token: str, device_id: str) -> list[str]:
    entry = ["  rogmem:", f"    url: {url}"]
    if token:
        entry.append(f"    bearer_token: {token}")
        entry.append("    headers:")
        entry.append(f"      Authorization: Bearer {token}")
        if device_id:
            entry.append(f"      X-Rogmem-Device: {device_id}")
    return entry


def _merge_yaml_mcp(path: Path, url: str, token: str = "", device_id: str = "") -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = [line if line.strip() != "mcp_servers: {}" else "mcp_servers:" for line in text.splitlines()]
    start = next((i for i, line in enumerate(lines) if line.strip() == "mcp_servers:"), None)
    held = ""
    if start is None:
        token = _carry_token(token, held)
        entry = _yaml_entry(url, token, device_id)
        if text and not text.endswith("\n"):
            text += "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + ("\n" if text else "") + "mcp_servers:\n" + "\n".join(entry) + "\n", encoding="utf-8")
        return
    end = start + 1
    while end < len(lines) and (lines[end].startswith(" ") or lines[end].strip() == ""):
        end += 1
    body = lines[start + 1 : end]
    kept: list[str] = []
    index = 0
    while index < len(body):
        if body[index].strip() in {"rogmem:", "rogmem"}:
            index += 1
            block: list[str] = []
            while index < len(body) and (body[index].startswith("    ") or body[index].strip() == ""):
                block.append(body[index])
                index += 1
            held = held or _yaml_token(block)
            continue
        kept.append(body[index])
        index += 1
    entry = _yaml_entry(url, _carry_token(token, held), device_id)
    lines = lines[: start + 1] + kept + entry + lines[end:]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _usable_token(token: str) -> str:
    text = (token or "").strip()
    if not text or any(ch.isspace() or ch in "\"'`" for ch in text):
        return ""
    return text


def _carry_token(supplied: str, held: str) -> str:
    """A rewrite that does not bring a credential leaves the one already there."""
    return _usable_token(supplied) or _usable_token(held)


def _memory_provider_names(plugins: Path) -> set[str]:
    """Plugin directories Hermes would load as a memory provider."""
    names: set[str] = set()
    if not plugins.is_dir():
        return names
    for child in plugins.iterdir():
        manifest = child / "plugin.yaml"
        if not child.is_dir() or not manifest.is_file():
            continue
        text = manifest.read_text(encoding="utf-8", errors="replace")
        if "runtime: hermes_memory_provider" in text:
            names.add(child.name)
    return names


def _configured_memory_provider(text: str) -> str:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == "memory:" and not line.startswith(" ")), None)
    if start is None:
        return ""
    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):
            break
        if line.startswith("  provider:"):
            return line.split(":", 1)[1].strip()
    return ""


def _strip_mcp_server(lines: list[str], name: str) -> list[str]:
    """Drop one mcp_servers entry. Other servers in that block stay."""
    start = next((i for i, line in enumerate(lines) if line.strip() == "mcp_servers:"), None)
    if start is None:
        return lines
    end = start + 1
    while end < len(lines) and (lines[end].startswith(" ") or lines[end].strip() == ""):
        end += 1
    body = lines[start + 1 : end]
    kept: list[str] = []
    index = 0
    while index < len(body):
        line = body[index]
        key = line.strip()[:-1] if line.startswith("  ") and not line.startswith("   ") and line.strip().endswith(":") else ""
        if key == name:
            index += 1
            while index < len(body):
                nxt = body[index]
                if nxt.startswith("  ") and not nxt.startswith("   ") and nxt.strip().endswith(":"):
                    break
                index += 1
            continue
        kept.append(line)
        index += 1
    return lines[: start + 1] + kept + lines[end:]


def _hermes_config_paths(root: Path) -> list[Path]:
    paths = []
    main = root / "config.yaml"
    if main.is_file():
        paths.append(main)
    profiles = root / "profiles"
    if profiles.is_dir():
        paths.extend(sorted(path for path in profiles.glob("*/config.yaml") if path.is_file()))
    return paths


def _retire_other_memory_accounts(root: Path, keep: str = "rogmem") -> None:
    """One Hermes home has one memory account.

    The account is the memory provider plus the MCP server of the same name.
    Another memory-provider plugin, or the provider being replaced, is a
    second account and is removed. Servers that are not memory providers stay.
    """
    homes = [root]
    profiles = root / "profiles"
    if profiles.is_dir():
        homes.extend(sorted(path for path in profiles.iterdir() if path.is_dir()))
    for home in homes:
        retiring = _memory_provider_names(home / "plugins") - {keep}
        config = home / "config.yaml"
        text = config.read_text(encoding="utf-8") if config.is_file() else ""
        previous = _configured_memory_provider(text)
        if previous and previous != keep:
            retiring.add(previous)
        for name in retiring:
            plugin = home / "plugins" / name
            if plugin.is_dir():
                shutil.rmtree(plugin)
        if not config.is_file():
            continue
        lines = text.splitlines()
        for name in sorted(retiring):
            lines = _strip_mcp_server(lines, name)
        config.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _set_memory_provider(path: Path, name: str) -> None:
    """Point Hermes' memory slot at one provider. Other top-level blocks stay."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line == "memory:" or line.strip() == "memory:" and not line.startswith(" ")), None)
    if start is None:
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(["memory:", f"  provider: {name}"])
    else:
        end = start + 1
        while end < len(lines) and (lines[end].startswith(" ") or lines[end].strip() == ""):
            end += 1
        replaced = False
        for index in range(start + 1, end):
            if lines[index].startswith("  provider:"):
                lines[index] = f"  provider: {name}"
                replaced = True
                break
        if not replaced:
            lines.insert(start + 1, f"  provider: {name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _install_hermes_memory(home: Path, url: str = "", token: str = "", device_id: str = "") -> None:
    """The memory slot Hermes loads is the same module the installer uses for turn identity.

    Profiles under the same Hermes home are the same account. Each one points
    at this provider, and any other memory-provider plugin is removed.
    """
    source = Path(__file__).resolve().parents[1] / "rogmem" / "hermes_memory.py"
    plugin_text = source.read_text(encoding="utf-8")
    manifest = (
        "name: rogmem\nversion: 1.0.0\ndescription: ROGMEM memory for Hermes\nprotocol_version: 1\nruntime: hermes_memory_provider\n"
    )
    root = hermes_home(home)
    _retire_other_memory_accounts(root)
    homes = [root]
    profiles = root / "profiles"
    if profiles.is_dir():
        homes.extend(sorted(path for path in profiles.iterdir() if (path / "config.yaml").is_file()))
    for profile_home in homes:
        dest = profile_home / "plugins" / "rogmem"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "__init__.py").write_text(plugin_text, encoding="utf-8")
        (dest / "plugin.yaml").write_text(manifest, encoding="utf-8")
        config = profile_home / "config.yaml"
        _set_memory_provider(config, "rogmem")
        if url and token:
            _merge_yaml_mcp(config, url, token, device_id)


def write_configs(home: Path, base_url: str, token: str = "", programs: list[str] | None = None, allow_real: bool = False) -> list[str]:
    """Point detected programs at rogmem. A real user home is not written unless allow_real."""
    if _real_user_home(Path(home)) and not allow_real:
        return []
    url = base_url.rstrip("/") + "/mcp"
    supplied = _usable_token(token)
    device_id = _device_id(home)
    targets = {
        "Cursor": lambda: _merge_mcp_json(home / ".cursor" / "mcp.json", url, supplied, device_id),
        "Trae": lambda: _merge_mcp_json(trae_mcp_path(home), url, supplied, device_id),
        "WorkBuddy": lambda: _merge_mcp_json(home / ".workbuddy" / "mcp.json", url, supplied, device_id),
        "CodeBuddy": lambda: _merge_mcp_json(home / ".codebuddy" / "mcp.json", url, supplied, device_id),
        "DSH": lambda: _merge_dsh_mcp(home, url, supplied, device_id),
        "Codex": lambda: _merge_toml_mcp(home / ".codex" / "config.toml", url, supplied),
        "Hermes": lambda: _merge_yaml_mcp(hermes_home(home) / "config.yaml", url, supplied, device_id),
    }
    chosen = chosen_hosts(home, programs)
    written = []
    for program in chosen:
        write = targets.get(program)
        if write is None:
            continue
        write()
        written.append(program)
    if supplied and "Hermes" in written:
        _install_hermes_memory(home, url, supplied, device_id)
    install_global_rules(home, allow_real=allow_real, programs=chosen)
    install_host_hooks(home, programs=chosen, allow_real=allow_real)
    return written
