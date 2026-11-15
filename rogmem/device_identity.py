"""Device display names: hostname default, no relative labels like 本机."""

from __future__ import annotations

import platform
import sqlite3
from urllib.parse import quote, unquote

BANNED_LABELS = frozenset({"本机", "localhost", "local"})
UNNAMED_LABEL = "未命名设备"
FALLBACK_NAME = "device"


def default_device_name() -> str:
    host = (platform.node() or "").strip()
    if "." in host:
        host = host.split(".", 1)[0].strip()
    return host or FALLBACK_NAME


def _is_banned(name: str) -> bool:
    text = (name or "").strip()
    if not text:
        return True
    return text.lower() in BANNED_LABELS


def given_device_name(name: str | None) -> str:
    """A name a person or their computer supplied. The hub's own hostname is not one."""
    text = "" if name is None else str(name).strip()
    if _is_banned(text):
        return ""
    return text


def normalize_device_name(name: str | None) -> str:
    text = "" if name is None else str(name).strip()
    if not _is_banned(text):
        return text
    host = default_device_name()
    if _is_banned(host):
        return FALLBACK_NAME
    return host


def display_device_name(name: str | None) -> str:
    """NAS-side outward label. Historical 本机 is unnamed, not this host's name."""
    text = "" if name is None else str(name).strip()
    if not text or text == "本机":
        return UNNAMED_LABEL
    for sep in (" · ", "·"):
        if text.startswith("本机" + sep):
            return UNNAMED_LABEL + text[len("本机") :]
    return text


LOCAL_SUFFIX = "（本机）"
_HEADER_NAME = "utf-8''"


def header_device_name(name: str | None) -> str:
    """A device name that can travel in an HTTP header. Non-latin-1 is percent-encoded."""
    text = "" if name is None else str(name)
    try:
        text.encode("latin-1")
    except UnicodeEncodeError:
        return _HEADER_NAME + quote(text, safe="")
    return text


def read_device_name_header(value: str | None) -> str:
    """Undo header_device_name. A plain latin-1 name is returned as-is."""
    text = "" if value is None else str(value).strip()
    if text.lower().startswith(_HEADER_NAME):
        return unquote(text[len(_HEADER_NAME) :])
    return text


def device_label(name: str | None, local: bool = False) -> str:
    """Display label. （本机） is added for the viewing computer and is never stored."""
    base = display_device_name(name)
    if local and not base.endswith(LOCAL_SUFFIX):
        return base + LOCAL_SUFFIX
    return base


def migrate_local_device_labels(con: sqlite3.Connection, name: str | None = None) -> int:
    """Optional installer-local rewrite of 本机. Do not run blindly on NAS."""
    label = normalize_device_name(name)
    changed = 0
    cur = con.execute("UPDATE sources SET device_name=? WHERE device_name=?", (label, "本机"))
    changed += max(cur.rowcount or 0, 0)
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "agents" in tables:
        cur = con.execute("UPDATE agents SET device_name=? WHERE device_name=?", (label, "本机"))
        changed += max(cur.rowcount or 0, 0)
        cur = con.execute(
            "UPDATE agents SET display_name=? || substr(display_name, length('本机') + 1) "
            "WHERE display_name LIKE ?",
            (label, "本机 · %"),
        )
        changed += max(cur.rowcount or 0, 0)
    con.commit()
    return changed
