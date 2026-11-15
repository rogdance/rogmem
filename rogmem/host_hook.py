#!/usr/bin/env python3
"""One host hook for every supported host, including DSH via its plugin.

Fail open. A missing hub, a slow call, or a bad payload still lets the agent continue.
The script uses only the standard library so the installed copy does not need this repo.
Bytes another program wrote are UTF-8. The ANSI code page is not an encoding of those bytes.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

PACKET_MARK = "【rogmem 历史包】"

_QUIET_SECONDS = 600
_TIMEOUT = 2
# A read answers inside the host's hook deadline. A turn upload embeds the turn
# on the hub and takes seconds, so the one the hook makes inline is short and
# best-effort; the resident channel has no host deadline and waits for the hub.
_INLINE_UPLOAD_TIMEOUT = 2
_DRAIN_UPLOAD_TIMEOUT = 30
_FLUSH_LIMIT = 20


def stable_seq(session_id: str, role: str, text: str) -> int:
    digest = hashlib.sha256(f"{session_id}\n{role}\n{text}".encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


_PROGRAMS = {"Cursor", "Trae", "Hermes", "Codex", "WorkBuddy", "CodeBuddy", "DSH"}


def program_of(script: Path, explicit: str = "") -> str:
    named = (explicit or "").strip()
    if named in _PROGRAMS:
        return named
    parts = set(script.resolve().parts)
    if ".cursor" in parts:
        return "Cursor"
    if ".trae" in parts:
        return "Trae"
    if ".hermes" in parts:
        return "Hermes"
    if ".codex" in parts:
        return "Codex"
    if ".workbuddy" in parts:
        return "WorkBuddy"
    if ".codebuddy" in parts:
        return "CodeBuddy"
    if ".dsh" in parts:
        return "DSH"
    return ""


def home_of(script: Path) -> Path:
    resolved = script.resolve()
    parts = resolved.parts
    for name in (".cursor", ".trae", ".hermes", ".codex", ".workbuddy", ".codebuddy", ".dsh"):
        if name in parts:
            return Path(*parts[: parts.index(name)])
    return resolved.parents[2]


def _event_name(payload: dict) -> str:
    return str(payload.get("hook_event_name") or payload.get("event") or "").strip()


def _session_id(payload: dict) -> str:
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    for key in ("session_id", "conversation_id", "sessionId"):
        value = str(payload.get(key) or extra.get(key) or "").strip()
        if value:
            return value
    return "session"


def _first_turn(payload: dict) -> bool:
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    if "is_first_turn" in extra:
        return bool(extra.get("is_first_turn"))
    return True


def _text_from_content(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_text_from_content(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        return _text_from_content(value.get("text") or value.get("content") or "")
    return ""


def _assistant_from_transcript(path: str) -> str:
    """Final reply when a host's stop event points at a transcript instead of the text."""
    file = Path(path) if path else None
    if file is None or not file.is_file():
        return ""
    try:
        size = file.stat().st_size
        with file.open("rb") as handle:
            handle.seek(max(0, size - 256_000))
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = raw.splitlines()
    if size > 256_000 and lines:
        lines = lines[1:]
    for line in reversed(lines[-200:]):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
        if not isinstance(payload, dict):
            continue
        kind = str(payload.get("type") or "")
        if kind == "task_complete":
            text = str(payload.get("last_agent_message") or "").strip()
            if text:
                return text
        if kind == "agent_message":
            text = str(payload.get("message") or "").strip()
            if text:
                return text
        role = str(payload.get("role") or obj.get("type") or "")
        if role == "assistant":
            text = _text_from_content(payload.get("content") or payload.get("message") or payload.get("text"))
            if text:
                return text
    return ""


def _visible_turns(payload: dict, program: str) -> list[tuple[str, str]]:
    event = _event_name(payload)
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    found: list[tuple[str, str]] = []
    if event in {"beforeSubmitPrompt", "UserPromptSubmit"}:
        text = str(payload.get("prompt") or payload.get("text") or extra.get("user_message") or "").strip()
        if text:
            found.append(("user", text))
    elif event in {"afterAgentResponse", "stop", "Stop"}:
        text = str(
            payload.get("text")
            or payload.get("response")
            or payload.get("last_assistant_message")
            or extra.get("assistant_response")
            or ""
        ).strip()
        if not text:
            text = _assistant_from_transcript(str(payload.get("transcript_path") or ""))
        if text:
            found.append(("assistant", text))
    elif event == "post_llm_call":
        user = str(extra.get("user_message") or payload.get("user_message") or "").strip()
        assistant = str(extra.get("assistant_response") or payload.get("assistant_response") or "").strip()
        if user:
            found.append(("user", user))
        if assistant:
            found.append(("assistant", assistant))
    return found


def _device_id(home: Path) -> str:
    try:
        return (home / ".rogmem" / "device_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _mcp_endpoint(path: Path, home: Path) -> tuple[str, str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", "", ""
    server = (data.get("mcpServers") or {}).get("rogmem") if isinstance(data, dict) else None
    if not isinstance(server, dict):
        return "", "", ""
    headers = server.get("headers") if isinstance(server.get("headers"), dict) else {}
    auth = str(headers.get("Authorization") or "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    device = str(headers.get("X-Rogmem-Device") or "").strip() or _device_id(home)
    return str(server.get("url") or "").strip(), token, device


def _toml_value(text: str, key: str) -> str:
    match = re.search(rf'(?m)^{re.escape(key)}\s*=\s*"([^"]*)"', text)
    return match.group(1).strip() if match else ""


def _toml_rogmem(path: Path, home: Path) -> tuple[str, str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "", "", ""
    start = next((i for i, line in enumerate(lines) if line.strip() == "[mcp_servers.rogmem]"), None)
    if start is None:
        return "", "", ""
    block = []
    for line in lines[start + 1 :]:
        if line.startswith("["):
            break
        block.append(line)
    body = "\n".join(block)
    return _toml_value(body, "url"), _toml_value(body, "bearer_token"), _device_id(home)


def _trae_mcp_path(home: Path) -> Path:
    """Trae keeps user-level MCP in its VS Code-style User dir, not in ~/.trae."""
    if sys.platform.startswith("win"):
        base = home / "AppData" / "Roaming" / "Trae"
    elif sys.platform.startswith("linux"):
        base = home / ".config" / "Trae"
    else:
        base = home / "Library" / "Application Support" / "Trae"
    return base / "User" / "mcp.json"


def _endpoint(home: Path, program: str) -> tuple[str, str, str]:
    if program == "Cursor":
        return _mcp_endpoint(home / ".cursor" / "mcp.json", home)
    if program == "Trae":
        return _mcp_endpoint(_trae_mcp_path(home), home)
    if program == "WorkBuddy":
        return _mcp_endpoint(home / ".workbuddy" / "mcp.json", home)
    if program == "CodeBuddy":
        return _mcp_endpoint(home / ".codebuddy" / "mcp.json", home)
    if program == "DSH":
        path = home / ".rogmem" / "hosts" / "DSH.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "", "", ""
        if not isinstance(data, dict):
            return "", "", ""
        device = str(data.get("device") or "").strip() or _device_id(home)
        return str(data.get("url") or "").strip(), str(data.get("token") or "").strip(), device
    if program == "Codex":
        return _toml_rogmem(home / ".codex" / "config.toml", home)
    path = home / ".hermes" / "config.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "", "", ""
    block = _yaml_block(text, "rogmem")
    url = _yaml_value(block, "url")
    token = _yaml_value(block, "bearer_token")
    device = ""
    match = re.search(r"X-Rogmem-Device:[ \t]*(\S+)", block)
    if match:
        device = match.group(1).strip().strip('"')
    return url, token, device or _device_id(home)


def _yaml_block(text: str, name: str) -> str:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == f"{name}:"), None)
    if start is None:
        return ""
    base = len(lines[start]) - len(lines[start].lstrip())
    picked = []
    for line in lines[start + 1 :]:
        if line.strip() and (len(line) - len(line.lstrip())) <= base:
            break
        picked.append(line)
    return "\n".join(picked)


def _yaml_value(text: str, key: str) -> str:
    match = re.search(rf"(?m)^[ \t]+{re.escape(key)}:[ \t]*(\S+)", text)
    if not match:
        return ""
    return match.group(1).strip().strip('"')


def _decode_rpc(raw: str) -> dict:
    text = (raw or "").strip()
    if not text:
        return {}
    if text.startswith("{") or text.startswith("["):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            loaded = None
        return loaded if isinstance(loaded, dict) else {}
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            loaded = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict) and ("result" in loaded or "error" in loaded):
            return loaded
    return {}


def _outside_text(data: bytes) -> str:
    """Bytes another program wrote. UTF-8, including a leading BOM. Not the ANSI code page."""
    return data.decode("utf-8-sig")


def _outside_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    data = _outside_bytes(json.dumps(payload, ensure_ascii=False))
    request = Request(url, data=data, headers=headers, method="POST")
    with urlopen(request, timeout=timeout) as resp:
        raw = _outside_text(resp.read())
    return _decode_rpc(raw)


def _tool_text(payload: dict) -> str:
    result = payload.get("result") if isinstance(payload, dict) else None
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        parts = [str(item.get("text") or "") for item in content if isinstance(item, dict)]
        text = "\n".join(part for part in parts if part).strip()
        if text:
            return text
    if isinstance(result, str):
        return result.strip()
    return ""


def _mcp(url: str, token: str, device: str, name: str, arguments: dict, timeout: float | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if device:
        headers["X-Rogmem-Device"] = device
    return _post_json(
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers,
        _TIMEOUT if timeout is None else timeout,
    )


def _directories(payload: dict) -> list[str]:
    """Directories the host names as where this session is working."""
    found: list[str] = []

    def take(value) -> None:
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if text not in found:
                found.append(text)
        elif isinstance(value, list):
            for item in value:
                take(item)

    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    for source in (payload, extra):
        for key in ("cwd", "workspace_root", "workspace_roots"):
            take(source.get(key))
    return found


def _remote_from_dir(directory: str) -> str:
    """The remote of a checkout. A path that is not a repository is not a project."""
    if not directory or not Path(directory).is_dir():
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", directory, "remote", "get-url", "origin"],
            capture_output=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    try:
        remote = _outside_text(proc.stdout or b"").strip()
    except UnicodeError:
        return ""
    if "://" in remote:
        scheme, rest = remote.split("://", 1)
        if "@" in rest:
            rest = rest.split("@", 1)[1]
        return f"{scheme}://{rest}".rstrip("/")
    if remote.startswith("git@"):
        return remote
    return ""


def _remote_project(payload: dict) -> str:
    for directory in _directories(payload):
        remote = _remote_from_dir(directory)
        if remote:
            return remote
    return ""


def _session_project(home: Path, program: str, session_id: str, payload: dict) -> str:
    """One repository for the session. A later event reuses it when the host omits the directory."""
    found = _remote_project(payload)
    path = _marker(home, program, session_id, "project")
    if found:
        _remember(path, found + "\n")
        return found
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _core_text(home: Path, program: str, project: str = "") -> str:
    url, token, device = _endpoint(home, program)
    if not url or not token.startswith("rm_"):
        return ""
    arguments = {}
    if project:
        arguments["project"] = project
    body = _mcp(url, token, device, "core", arguments)
    return _tool_text(body)


def _marker(home: Path, program: str, session_id: str, kind: str) -> Path:
    return home / ".rogmem" / "hook-core" / f"{program}-{session_id}.{kind}"


def _remember(path: Path, text: str) -> None:
    """Local bookkeeping is best-effort. A state dir the host refuses must not cost the packet."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        return


def _quiet(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime < _QUIET_SECONDS
    except OSError:
        return False


def _outbox(home: Path) -> Path:
    return home / ".rogmem" / "outbox.jsonl"


def _turn(program: str, session_id: str, role: str, text: str, project: str = "") -> dict:
    seq = stable_seq(session_id, role, text)
    item = {
        "program": program,
        "session_id": session_id,
        "turn_id": f"{session_id}:{seq}",
        "seq": seq,
        "role": role,
        "text": text,
        "event_at": datetime.now(timezone.utc).isoformat(),
        "event_basis": "utterance",
    }
    if project:
        item["project"] = project
    return item


def _read_outbox(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("text"):
            rows.append(item)
    return rows


def _write_outbox(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.unlink(missing_ok=True)
        return
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows), encoding="utf-8")


class _OutboxLock:
    """One writer at a time. The hook and the channel both drain the same file."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = (self.path.parent / (self.path.name + ".lock")).open("a")
        try:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        return self

    def __exit__(self, *args):
        if self.handle is None:
            return
        try:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except ImportError:
            pass
        self.handle.close()


def _turn_key(item: dict) -> str:
    turn_id = str(item.get("turn_id") or "").strip()
    if turn_id:
        return turn_id
    return f"{item.get('session_id')}:{item.get('seq')}:{item.get('role')}"


def _submit_ok(payload: dict) -> bool:
    text = _tool_text(payload)
    if not text:
        return False
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False
    return bool(isinstance(data, dict) and data.get("ok"))


def _flush(home: Path, program: str, rows: list[dict], timeout: float = _DRAIN_UPLOAD_TIMEOUT) -> list[dict]:
    """Hand turns through the same MCP door as core. The admin door does not take a host key."""
    url, token, device = _endpoint(home, program)
    if not url or not token.startswith("rm_"):
        return rows
    pending = list(rows)
    sent = 0
    while pending and sent < _FLUSH_LIMIT:
        turn = pending[0]
        role = "assistant" if turn.get("role") == "assistant" else "user"
        arguments = {
            "session_id": str(turn.get("session_id") or ""),
            "turn_id": str(turn.get("turn_id") or ""),
            "seq": int(turn.get("seq") or 0),
            "role": role,
            "text": str(turn.get("text") or ""),
            "event_at": str(turn.get("event_at") or ""),
            "event_basis": str(turn.get("event_basis") or ""),
        }
        project = str(turn.get("project") or "")
        if project:
            arguments["project"] = project
        try:
            ok = _submit_ok(_mcp(url, token, device, "submit_turn", arguments, timeout))
        except Exception:
            return pending
        if not ok:
            return pending
        pending = pending[1:]
        sent += 1
    return pending


def drain_outbox(home: Path, timeout: float = _DRAIN_UPLOAD_TIMEOUT) -> int:
    """Hand the local gap to the hub. A turn stays here until the hub accepts it."""
    path = _outbox(home)
    with _OutboxLock(path):
        rows = _read_outbox(path)
    if not rows:
        return 0
    sent: set[str] = set()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("program") or ""), []).append(row)
    for program, items in grouped.items():
        if program not in _PROGRAMS:
            continue
        try:
            left = _flush(home, program, items, timeout)
        except Exception:
            continue
        pending = {_turn_key(item) for item in left}
        for item in items:
            key = _turn_key(item)
            if key and key not in pending:
                sent.add(key)
    if not sent:
        return 0
    with _OutboxLock(path):
        current = _read_outbox(path)
        _write_outbox(path, [row for row in current if _turn_key(row) not in sent])
    return len(sent)


def _capture(home: Path, program: str, payload: dict, project: str = "", *, drain: bool = True) -> None:
    session_id = _session_id(payload)
    fresh = [
        _turn(program, session_id, role, text, project)
        for role, text in _visible_turns(payload, program)
    ]
    path = _outbox(home)
    if fresh:
        with _OutboxLock(path):
            _write_outbox(path, _read_outbox(path) + fresh)
    if not drain:
        return
    try:
        drain_outbox(home, _INLINE_UPLOAD_TIMEOUT)
    except Exception:
        return


def _context_response(event: str, packet: str) -> dict:
    """One host event has one context field. A second field is a second copy or a drop."""
    if event == "pre_llm_call":
        return {"context": packet}
    if event in {"SessionStart", "UserPromptSubmit"}:
        return {"hookSpecificOutput": {"additionalContext": packet}}
    return {"additional_context": packet}


def handle(payload: dict, *, home: Path, program: str) -> dict:
    """One event. Empty means the agent should continue with what it already has."""
    if program not in _PROGRAMS:
        return {}
    event = _event_name(payload)
    session_id = _session_id(payload)
    project = _session_project(home, program, session_id, payload)
    opening = event in {"sessionStart", "SessionStart", "pre_llm_call", "UserPromptSubmit"}
    if opening and program == "Hermes" and event == "pre_llm_call" and not _first_turn(payload):
        opening = False
    packet = ""
    if opening:
        ok_mark = _marker(home, program, session_id, "ok")
        down_mark = _marker(home, program, session_id, "down")
        if not ok_mark.is_file() and not _quiet(down_mark):
            try:
                text = _core_text(home, program, project)
            except Exception:
                text = ""
            if not text:
                _remember(down_mark, "down\n")
            else:
                packet = f"{PACKET_MARK}\n{text}"
                _remember(ok_mark, "ok\n")
    result = _context_response(event, packet) if packet else {}
    try:
        # The opening response has to leave before the host's deadline.
        # Uploading saved turns is later work; doing it here can kill the process
        # after the session is already marked delivered.
        _capture(home, program, payload, project, drain=not packet)
    except Exception:
        return result
    return result


def invocation(script: Path, argv: list[str] | None = None) -> tuple[Path, str]:
    """Install path selects the host. A plugin can name the host and home with arguments."""
    args = list(sys.argv[1:] if argv is None else argv)
    program = ""
    home_text = ""
    index = 0
    while index < len(args):
        if args[index] == "--program" and index + 1 < len(args):
            program = args[index + 1].strip()
            index += 2
        elif args[index] == "--home" and index + 1 < len(args):
            home_text = args[index + 1].strip()
            index += 2
        else:
            index += 1
    if not program:
        program = program_of(script)
    home = Path(home_text) if home_text else home_of(script)
    return home, program


def _channel_name(stream) -> str:
    return (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")


def _utf8_channel(stream, *, write: bool):
    """A text stream whose bytes are UTF-8. The previous wrapper's code page is detached from this buffer.

    A stream that is already Unicode, with no binary buffer, is returned as it is.
    A stream that is already UTF-8 is left in place so its owner keeps the wrapper.
    """
    current = _channel_name(stream)
    if write and current == "utf8":
        return stream
    if not write and current in {"utf8", "utf8sig"}:
        return stream
    detach = getattr(stream, "detach", None)
    if detach is None:
        return stream
    try:
        raw = detach()
    except (AttributeError, OSError, ValueError):
        return stream
    encoding = "utf-8" if write else "utf-8-sig"
    try:
        return io.TextIOWrapper(raw, encoding=encoding, errors="strict", newline="\n", line_buffering=write)
    except Exception:
        return io.TextIOWrapper(raw, encoding="utf-8", newline="\n")


def _bind_stdio(old, new) -> None:
    if new is old:
        return
    for name in ("stdin", "stdout", "stderr"):
        if getattr(sys, name) is old:
            setattr(sys, name, new)
            return


def adopt_stdio() -> None:
    """Standard streams speak UTF-8 for the rest of this process."""
    for name, write in (("stdin", False), ("stdout", True), ("stderr", True)):
        current = getattr(sys, name)
        _bind_stdio(current, _utf8_channel(current, write=write))


def read_outside(stream) -> str:
    """Read text another program wrote. A code page is never the decoder."""
    channel = _utf8_channel(stream, write=False)
    _bind_stdio(stream, channel)
    name = _channel_name(channel)
    if name in {"utf8", "utf8sig"}:
        return channel.read()
    try:
        data = channel.buffer.read()
    except (AttributeError, OSError, ValueError) as exc:
        if not name:
            return channel.read()
        raise UnicodeError("outside text has no utf-8 bytes") from exc
    return "" if not data else _outside_text(data)


def write_outside(stream, text: str) -> None:
    """Write text for another program to read as UTF-8. A code page is never the encoder."""
    channel = _utf8_channel(stream, write=True)
    _bind_stdio(stream, channel)
    name = _channel_name(channel)
    if name in {"utf8", "utf8sig"} or not name:
        channel.write(text)
        channel.flush()
        return
    channel.buffer.write(_outside_bytes(text))
    channel.buffer.flush()


def main() -> int:
    try:
        raw = read_outside(sys.stdin)
    except (UnicodeError, OSError):
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    script = Path(__file__)
    try:
        home, program = invocation(script)
        result = handle(payload, home=home, program=program)
    except Exception:
        result = {}
    write_outside(sys.stdout, json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        adopt_stdio()
        raise SystemExit(main())
    except Exception:
        try:
            write_outside(sys.stdout, "{}")
        except Exception:
            pass
        raise SystemExit(0)
