"""Hermes memory slot for the rogmem hub.

History and a later turn share one identity: session, role, and visible text.
This file is copied into the Hermes plugin directory, so it stays on the
standard library plus the optional MemoryProvider base.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

try:
    from agent.memory_provider import MemoryProvider
except ImportError:  # running inside the rogmem repo, or before Hermes imports it
    class MemoryProvider:  # type: ignore[no-redef]
        pass


def _plugin_version() -> str:
    """The rogmem plugin this provider was installed with. Copied into Hermes, so read from the manifest."""
    try:
        data = json.loads((Path.home() / ".rogmem" / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("version") or "").strip() if isinstance(data, dict) else ""


def stable_seq(session_id: str, role: str, text: str) -> int:
    """Same utterance, same seq, wherever it is read from."""
    digest = hashlib.sha256(f"{session_id}\n{role}\n{text}".encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


_REMOTE = re.compile(
    r"(?:(?:https?|ssh)://(?:[^@/\s]+@)?(?P<host>[^/\s:]+)(?::\d+)?[/:](?P<path>[^\s]+))"
    r"|(?:git@(?P<githost>[^:\s]+):(?P<gitpath>[^\s]+))",
    re.I,
)


def _public_origin(url: str) -> str:
    """Drop credentials from a remote URL. A local path is returned unchanged."""
    text = (url or "").strip()
    if "://" not in text:
        return text.rstrip("/")
    scheme, rest = text.split("://", 1)
    if "@" in rest:
        rest = rest.split("@", 1)[1]
    return f"{scheme}://{rest}".rstrip("/")


def repository_key(text: str) -> str:
    """One key for every spelling of the same remote. Empty when the text names no remote."""
    found = _REMOTE.search(text or "")
    if not found:
        return ""
    host = (found.group("host") or found.group("githost") or "").lower()
    path = (found.group("path") or found.group("gitpath") or "").strip().strip("/")
    path = path.rstrip(").,，。")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not host or not path:
        return ""
    return f"{host}/{path}"


# A page under a repository is not a second repository.
_REPO_TAIL = frozenset(
    {
        "releases",
        "release",
        "blob",
        "tree",
        "commit",
        "commits",
        "issues",
        "issue",
        "pull",
        "pulls",
        "wiki",
        "actions",
        "tags",
        "tag",
        "compare",
        "archive",
        "raw",
        "src",
        "branch",
        "branches",
        "stargazers",
        "network",
        "security",
        "pulse",
        "projects",
        "packages",
        "graphs",
        "forks",
        "watchers",
        "activity",
    }
)
_PUBLIC_FORGE = frozenset({"github.com", "gitlab.com", "bitbucket.org"})


def _code_host(host: str) -> bool:
    return host in _PUBLIC_FORGE or host.startswith("git.")


def _one_repository_slug(url: str, *, declared: bool) -> str:
    """host/owner/name. A release page, a .git suffix, and wrapping marks are the same repository."""
    raw = (url or "").strip().strip("`").rstrip(").,，。；;、")
    if not raw:
        return ""
    had_git = raw.lower().endswith(".git")
    key = repository_key(raw)
    if not key:
        return ""
    host, _, path = key.partition("/")
    if host.startswith("www."):
        host = host[4:]
    if host == "gist.github.com":
        return ""
    if not declared and not _code_host(host) and not had_git:
        return ""
    kept: list[str] = []
    for part in (piece for piece in path.split("/") if piece):
        if part.lower() in _REPO_TAIL:
            break
        kept.append(part)
    if host in _PUBLIC_FORGE:
        kept = kept[:2]
    if len(kept) < 2:
        return ""
    return host + "/" + "/".join(kept)


def repository_slugs(text: str, *, declared: bool = False) -> list[str]:
    """Every repository named in text, in order, without duplicates."""
    raw = (text or "").replace("`", " ")
    found: list[str] = []
    for match in _REMOTE.finditer(raw):
        slug = _one_repository_slug(match.group(0), declared=declared)
        if slug and slug not in found:
            found.append(slug)
    return found


def repository_slug(text: str, *, declared: bool = False) -> str:
    """The first repository in text. Empty when the text names no repository."""
    found = repository_slugs(text, declared=declared)
    return found[0] if found else ""


def canonical_remote(url: str) -> str:
    """The remote written on a session: https, no credentials, no .git. A path stays a path."""
    text = _public_origin(url)
    key = repository_key(text)
    if key:
        return f"https://{key}"
    return text


def remote_slug(url: str) -> tuple[str, str, str]:
    """(forge host, owner, repository name). The name is the stable part; the owner can move."""
    parts = repository_key(url).split("/")
    if len(parts) < 3:
        return "", "", ""
    return parts[0], "/".join(parts[1:-1]), parts[-1]


def choose_project_ref(stored: str, incoming: str) -> str:
    """Keep a stored project unless the upload is the same repository with its owner repaired."""
    stored = (stored or "").strip()
    incoming = (incoming or "").strip()
    if not incoming:
        return stored
    if not stored:
        return incoming
    if repository_key(stored) and repository_key(stored) == repository_key(incoming):
        return canonical_remote(incoming)
    host, _, name = remote_slug(stored)
    other_host, _, other_name = remote_slug(incoming)
    if host and host == other_host and name and name == other_name:
        return canonical_remote(incoming)
    return stored


def _gh_view(slug: str) -> tuple[str, str]:
    """('url', current) when the forge answers, ('missing', '') when the slug is gone, else unavailable."""
    if not slug or slug.startswith("-") or ".." in slug:
        return "unavailable", ""
    try:
        result = subprocess.run(
            ["gh", "repo", "view", slug, "--json", "url", "--jq", ".url"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
            env=_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable", ""
    found = (result.stdout or "").strip()
    if result.returncode == 0 and found.startswith("http"):
        return "url", found
    err = (result.stderr or "").lower()
    if "could not resolve" in err or "not found" in err:
        return "missing", ""
    return "unavailable", ""


def _gh_login() -> str:
    """The account this host is logged into. Empty when that cannot be read."""
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
            env=_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    login = (result.stdout or "").strip()
    if result.returncode != 0 or not login or "/" in login or login.startswith("-"):
        return ""
    return login


def _github_redirect(url: str) -> str:
    """Where a public GitHub URL lands now. Empty when it does not resolve."""
    if not url.startswith("https://github.com/"):
        return ""
    try:
        request = Request(url, method="GET", headers={"User-Agent": "rogmem"})
        with urlopen(request, timeout=4) as response:
            return str(response.geturl() or "")
    except Exception:
        return ""


def _ask_forge(url: str) -> str:
    """The URL this host can resolve for a recorded remote. Empty when it should be left alone."""
    host, owner, name = remote_slug(url)
    if host != "github.com" or not owner or not name:
        return ""
    state, found = _gh_view(f"{owner}/{name}")
    if state == "url":
        return found
    if state == "missing":
        login = _gh_login()
        if login and login.lower() != owner.lower():
            state, found = _gh_view(f"{login}/{name}")
            if state == "url":
                return found
        return ""
    return _github_redirect(url)


_REMOTE_CACHE: dict[str, str] = {}


def current_remote(url: str, *, ask=None) -> str:
    """The forge's current URL for this repository.

    An account rename keeps the repository name and changes the owner. The
    uploading host asks the forge, and if the old owner is gone, the account
    it is logged into. A different repository name is not a repair.
    """
    recorded = canonical_remote(url)
    host, _, name = remote_slug(recorded)
    if not host or not name:
        return recorded
    if ask is None and recorded in _REMOTE_CACHE:
        return _REMOTE_CACHE[recorded]
    finder = _ask_forge if ask is None else ask
    try:
        found = finder(recorded) or ""
    except Exception:
        found = ""
    repaired = canonical_remote(found) if found else ""
    other_host, _, other_name = remote_slug(repaired)
    result = repaired if other_host == host and other_name == name else recorded
    if ask is None:
        _REMOTE_CACHE[recorded] = result
    return result


def _git_env() -> dict[str, str]:
    """Git answers about the directory we pass, not about GIT_DIR in the parent process."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git_line(cwd: str, args: list[str]) -> str:
    if not cwd or cwd.startswith("-") or "\x00" in cwd or not Path(cwd).is_dir():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            env=_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _git_repository(path: str) -> tuple[str, str] | None:
    """(origin url, work tree) for a checkout that has a remote or a commit.

    An empty ``git init`` is not a project. ``None`` means this path is not one.
    """
    top = _git_line(path, ["rev-parse", "--show-toplevel"])
    if not top:
        return None
    origin = _git_line(top, ["remote", "get-url", "origin"])
    if origin or _git_line(top, ["rev-parse", "--verify", "HEAD"]):
        return origin, top
    return None


def work_repository(cwd: str = "", recorded_root: str = "", *, resolve=None) -> str:
    """The repository a session was already working in.

    The host records the directory. On that same computer, git names the
    repository: its remote when it has one, otherwise a work tree that has
    history. An empty git directory is not a project. A recorded path that
    can no longer be opened is kept, because that is the checkout the host
    named. Nothing is read out of the transcript.
    """
    recorded = (recorded_root or "").strip()
    place = (cwd or "").strip() or recorded
    if place and "://" not in place and not place.startswith("git@"):
        finder = _git_repository if resolve is None else resolve
        try:
            found = finder(place)
        except Exception:
            found = None
        if found:
            origin, toplevel = found
            public = canonical_remote(str(origin or ""))
            if repository_key(public):
                return current_remote(public)
            top = str(toplevel or "").strip().rstrip("/")
            if top:
                return top
    if recorded and ("://" in recorded or recorded.startswith("git@")):
        return current_remote(canonical_remote(recorded))
    if place and Path(place).is_dir():
        if recorded and recorded != place:
            return work_repository("", recorded, resolve=resolve)
        return ""
    if recorded and recorded != place:
        return work_repository("", recorded, resolve=resolve)
    return recorded


def session_repository(db_path: Path, session_id: str) -> str:
    """Repository recorded on one Hermes session. Empty when the ledger has none."""
    sid = (session_id or "").strip()
    if not sid or not db_path.is_file():
        return ""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cols = {row[1] for row in con.execute("PRAGMA table_info(sessions)")}
            if "id" not in cols:
                return ""
            cwd_sql = "cwd" if "cwd" in cols else "''"
            root_sql = "git_repo_root" if "git_repo_root" in cols else "''"
            row = con.execute(
                f"SELECT {cwd_sql}, {root_sql} FROM sessions WHERE id=?",
                (sid,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ""
    if not row:
        return ""
    return work_repository(str(row[0] or ""), str(row[1] or ""))


def _core_project(project: str) -> str:
    """Only a remote address or a root commit may scope the history packet."""
    text = (project or "").strip()
    if "://" in text or text.startswith("git@"):
        return text
    if len(text) == 40 and all(ch in "0123456789abcdefABCDEF" for ch in text):
        return text
    return ""


def _tool_text(payload: dict) -> str:
    result = payload.get("result") if isinstance(payload, dict) else None
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        parts = [str(item.get("text") or "") for item in content if isinstance(item, dict)]
        text = "\n".join(part for part in parts if part).strip()
        if text:
            return text
    if isinstance(payload, dict) and payload.get("error"):
        return json.dumps(payload, ensure_ascii=False)
    return json.dumps(payload, ensure_ascii=False)


def _post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode()
    request = Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urlopen(request, timeout=30) as resp:
            loaded = json.loads(resp.read().decode())
    except Exception:
        return {"ok": False}
    return loaded if isinstance(loaded, dict) else {"ok": False}


def _config_text(home: Path) -> str:
    path = home / "config.yaml"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _endpoint(home: Path) -> tuple[str, str, str]:
    """MCP url, bearer token, and device id from the rogmem server block."""
    lines = _config_text(home).splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == "rogmem:" and line.startswith(" ")), None)
    if start is None:
        return "", "", ""
    url = ""
    token = ""
    device = ""
    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):
            break
        stripped = line.strip()
        if stripped.startswith("url:"):
            url = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("bearer_token:"):
            token = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("X-Rogmem-Device:"):
            device = stripped.split(":", 1)[1].strip()
    if url.endswith("/mcp"):
        url = url[: -len("/mcp")]
    return url, token, device


class RogmemMemoryProvider(MemoryProvider):
    """Recall and later turns use the hub the installer just enrolled."""

    def __init__(self) -> None:
        self._session_id = ""
        self._home = Path(os.path.expanduser("~/.hermes"))
        self._hub_override = ""
        self._packet_session = ""
        self._packet = ""

    @property
    def name(self) -> str:
        return "rogmem"

    def is_available(self) -> bool:
        _url, token, _device = _endpoint(self._home)
        return token.startswith("rm_")

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id or ""
        home = kwargs.get("hermes_home") or ""
        if home:
            self._home = Path(home)
        override = kwargs.get("hub_url") or ""
        if override:
            self._hub_override = str(override).rstrip("/")

    def get_tool_schemas(self) -> list[dict]:
        return [
            {
                "name": "core",
                "description": "装入历史包。本会话已经装过就不要再调，除非 before 是上次的续读位置。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "project": {"type": "string"},
                        "before": {"type": "string"},
                        "budget": {"type": "integer"},
                    },
                },
            },
            {
                "name": "search",
                "description": "按一个具体问题检索。不代替历史包，也不代替按时间续读。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
        ]

    def system_prompt_block(self) -> str:
        path = self._home / "rogmem-rule.md"
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text:
            return text
        from rogmem.host_rules import hermes_rule

        return hermes_rule().strip()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """One history packet per session. Later turns reuse it instead of searching again."""
        del query
        sid = (session_id or self._session_id or "").strip()
        if sid and sid == self._packet_session and self._packet:
            return self._packet
        project = ""
        if sid:
            project = _core_project(session_repository(self._home / "state.db", sid))
        arguments = {"project": project} if project else {}
        text = _tool_text(self._call_tool("core", arguments))
        if sid and text and "未授权" not in text:
            self._packet_session = sid
            self._packet = text
        return text

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list | None = None,
    ) -> None:
        del messages
        sid = session_id or self._session_id or "hermes"
        project = session_repository(self._home / "state.db", sid)
        turns = []
        now = datetime.now(timezone.utc).isoformat()
        for role, text in (("user", user_content), ("assistant", assistant_content)):
            visible = str(text or "").strip()
            if not visible:
                continue
            seq = stable_seq(sid, role, visible)
            item = {
                "session_id": sid,
                "turn_id": f"{sid}:{seq}",
                "seq": seq,
                "role": role,
                "text": visible,
                "event_at": now,
                "event_basis": "utterance",
                "program": "Hermes",
            }
            if project:
                item["project"] = project
            turns.append(item)
        if turns:
            self._post_history(turns)

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        del kwargs
        args = args or {}
        if tool_name == "search":
            query = str(args.get("query") or "")
            limit = int(args.get("limit") or 5)
            return _tool_text(self._call_tool("search", {"query": query, "limit": limit}))
        if tool_name == "core":
            arguments = {}
            project = _core_project(str(args.get("project") or ""))
            if project:
                arguments["project"] = project
            before = str(args.get("before") or "").strip()
            if before:
                arguments["before"] = before
            if args.get("budget"):
                arguments["budget"] = int(args["budget"])
            return _tool_text(self._call_tool("core", arguments))
        return json.dumps({"ok": False, "error": "unknown_tool"}, ensure_ascii=False)

    def _hub(self) -> tuple[str, str, str]:
        url, token, device = _endpoint(self._home)
        if self._hub_override:
            url = self._hub_override
        return url.rstrip("/"), token, device

    def _post_history(self, turns: list[dict]) -> dict:
        url, _token, _device = self._hub()
        if not url:
            return {"ok": False}
        return _post_json(f"{url}/admin/api/history", {"program": "Hermes", "turns": turns})

    def _call_tool(self, name: str, arguments: dict) -> dict:
        url, token, device = self._hub()
        if not url or not token.startswith("rm_"):
            return {"ok": False, "error": "未授权"}
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }
        if device:
            headers["X-Rogmem-Device"] = device
        plugin = _plugin_version()
        if plugin:
            headers["X-Rogmem-Plugin"] = plugin
        return _post_json(
            f"{url}/mcp",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers,
        )
