"""Version labels a person can read: S- for the server, P- for the rogmem plugin.

    S-V1-2611221408-DEV   the server (hub, admin pages, MCP, distill) in this image
    P-V1-2611221408-DEV   the rogmem plugin installed on each computer

- S / P says which of the two it is.
- V1 moves only when the product owner asks for an architecture bump.
- The ten digits are a pack time in Shanghai, YYMMDDHHmm. For S it is this image's
  pack. For P it is the pack in which the plugin's content last changed, so P stays
  put across server-only releases and no computer is told to update for nothing.
- The suffix is where it runs: DEV (private-repo production), PUB (public-repo
  production), PRE (pre-release). It comes from ROGMEM_RELEASE at run time.

Tags registered before the S- prefix existed (V1-...) are still read as server tags.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAJOR = "V1"
SERVER_PREFIX = "S"
PLUGIN_PREFIX = "P"
SHANGHAI = timezone(timedelta(hours=8))
STAMP_RE = re.compile(r"\d{10}")
UNPACKED = "未打包"


def shanghai_stamp(moment: datetime | None = None) -> str:
    when = moment or datetime.now(SHANGHAI)
    if when.tzinfo is None:
        when = when.replace(tzinfo=SHANGHAI)
    else:
        when = when.astimezone(SHANGHAI)
    return when.strftime("%y%m%d%H%M")


def resolve_pack_stamp(candidate: str = "", moment: datetime | None = None) -> str:
    """Decide the pack identity once, at build time.

    The release owner declares it explicitly (PACK_STAMP / --build-arg).
    Only when it is absent or malformed do we fall back to the wall clock,
    which is exactly the behaviour the old inline Dockerfile script had.
    Keeping this a domain function makes the build-time decision testable
    without a Docker daemon.
    """
    value = (candidate or os.environ.get("PACK_STAMP", "") or "").strip()
    if STAMP_RE.fullmatch(value):
        return value
    return shanghai_stamp(moment)


def pack_stamp() -> str:
    override = os.environ.get("ROGMEM_BUILD_STAMP", "").strip()
    if STAMP_RE.fullmatch(override):
        return override
    path = Path(__file__).with_name("BUILD_STAMP")
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if STAMP_RE.fullmatch(text):
            return text
    return ""


def repository_slug(repository: str) -> str:
    text = (repository or "").strip()
    if text.endswith(".git"):
        text = text[:-4]
    text = text.rstrip("/")
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text


def production_suffix(repository: str) -> str:
    """Private repo rogmem-dev stamps DEV. Public repo rogmem stamps PUB."""
    slug = repository_slug(repository)
    if slug == "rogmem-dev":
        return "DEV"
    if slug == "rogmem":
        return "PUB"
    raise ValueError(f"仓库 {repository!r} 没有正式版本后缀")


def checkout_origin(root: Path) -> str:
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    url = (result.stdout or "").strip()
    if result.returncode != 0 or not url:
        raise ValueError("读不到 origin，无法决定正式版本后缀")
    return url


def release_channel() -> str:
    """DEV is private-repo production. PUB is public-repo production.

    PRE is the pre-release stack. Anything else is not a release.
    """
    raw = os.environ.get("ROGMEM_RELEASE", "").strip().upper()
    if raw in {"DEV", "PUB", "PRE"}:
        return raw
    return ""


def version_label(prefix: str, stamp: str, channel: str = "") -> str:
    """`<prefix>-V1-<stamp>[-<channel>]`. An empty stamp is a build that was never packed."""
    body = stamp if STAMP_RE.fullmatch(stamp or "") else UNPACKED
    label = f"{prefix}-{MAJOR}-{body}"
    return f"{label}-{channel}" if channel and body != UNPACKED else label


def product_version() -> str:
    """The server version this process runs."""
    return version_label(SERVER_PREFIX, pack_stamp(), release_channel())


def plugin_label(stamp: str) -> str:
    """The plugin version for a pack time, on the channel this process runs."""
    return version_label(PLUGIN_PREFIX, stamp, release_channel())
