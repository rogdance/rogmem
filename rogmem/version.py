"""One product version for the server and the admin pages.

They ship in the same image, so a second client number would only repeat the
same pack time. V1 moves only when the product owner asks for an architecture
bump. The ten digits are the pack time in Shanghai, YYMMDDHHmm.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAJOR = "V1"
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


def product_version() -> str:
    stamp = pack_stamp()
    if not stamp:
        return f"{MAJOR}-{UNPACKED}"
    channel = release_channel()
    if not channel:
        return f"{MAJOR}-{stamp}"
    return f"{MAJOR}-{stamp}-{channel}"
