"""Release metadata manifest schema v1.

The publisher writes one JSON file; the about page reads it. Schema is
versioned (`schema: "rogmem-releases/<n>"`) so the reader refuses what
it does not understand, rather than guessing.

Contract (v1):

    {
      "schema": "rogmem-releases/1",
      "generated_at": "2026-11-12T04:30:00Z",
      "channels": {
        "stable":      { "latest": "V1-...-PUB", "released_at": "..Z" },
        "prerelease":  { "latest": "V1-...-PRE", "released_at": "..Z" }   # optional
      },
      "releases": [
        { "tag": "V1-...-PUB|PRE", "channel": "stable|prerelease",
          "released_at": "..Z" },
        ...
      ]
    }

Only the fields above are approved for publication. Unknown keys are
ignored by the reader. Oversize bodies are rejected before JSON decode.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SCHEMA_V1 = "rogmem-releases/1"
MAX_RESPONSE_BYTES = 256 * 1024
MAX_RELEASE_ENTRIES = 500
MAX_URL_LENGTH = 2048

_TAG_PUB = re.compile(r"^V([1-9]\d*)-(\d{10})-PUB$")
_TAG_ANY = re.compile(r"^V([1-9]\d*)-(\d{10})-(PUB|PRE)$")
_UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def _tag_sort_key(tag: str) -> tuple[int, int] | None:
    match = _TAG_PUB.fullmatch(tag) or _TAG_ANY.fullmatch(tag)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))


class ManifestError(Exception):
    """A manifest read that failed with a classified kind."""

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message or kind)
        self.kind = kind


@dataclass(frozen=True)
class ReleaseEntry:
    tag: str
    channel: str
    released_at: str


@dataclass(frozen=True)
class Manifest:
    schema: str
    generated_at: str
    stable_latest: str
    stable_released_at: str
    prerelease_latest: str
    prerelease_released_at: str
    releases: tuple[ReleaseEntry, ...]


def _utc(value: Any, label: str) -> str:
    """Accept only RFC3339 UTC strings that denote a real calendar instant.

    The regex just checks shape; `datetime.fromisoformat` then rejects
    nonexistent dates (e.g. 2026-13-40 or Feb 30) and invalid times.
    Only the UTC `Z` suffix is allowed; any other offset is refused in
    the regex above, so this stays a tight contract across publisher
    and reader.
    """
    if not isinstance(value, str) or not _UTC_Z.fullmatch(value):
        raise ManifestError("schema", f"{label} 必须是 UTC Z 后缀时间串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ManifestError("schema", f"{label} 不是真实的日历时间：{value!r}") from error
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) != timezone.utc.utcoffset(parsed):
        raise ManifestError("schema", f"{label} 必须是 UTC 零偏移时间")
    return value


def _pub_tag(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _TAG_PUB.fullmatch(value):
        raise ManifestError("schema", f"{label} 必须是 V<major>-<10 位>-PUB 格式")
    return value


def _any_tag(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _TAG_ANY.fullmatch(value):
        raise ManifestError("schema", f"{label} 必须是 V<major>-<10 位>-(PUB|PRE) 格式")
    return value


def _channel(data: Any, label: str) -> tuple[str, str]:
    if not isinstance(data, dict):
        raise ManifestError("schema", f"{label} 必须是对象")
    return (
        _pub_tag(data.get("latest"), f"{label}.latest")
        if label == "channels.stable"
        else _any_tag(data.get("latest"), f"{label}.latest"),
        _utc(data.get("released_at"), f"{label}.released_at"),
    )


def _release_entry(raw: Any, index: int) -> ReleaseEntry:
    if not isinstance(raw, dict):
        raise ManifestError("schema", f"releases[{index}] 必须是对象")
    tag = _any_tag(raw.get("tag"), f"releases[{index}].tag")
    channel = raw.get("channel")
    if not isinstance(channel, str) or not channel:
        raise ManifestError("schema", f"releases[{index}].channel 必须是非空字符串")
    released_at = _utc(raw.get("released_at"), f"releases[{index}].released_at")
    return ReleaseEntry(tag=tag, channel=channel, released_at=released_at)


def parse_manifest(body: bytes) -> Manifest:
    if not isinstance(body, (bytes, bytearray)):
        raise ManifestError("parse", "响应体必须是字节串")
    if len(body) > MAX_RESPONSE_BYTES:
        raise ManifestError("too_large", f"响应体 {len(body)} 字节超过上限 {MAX_RESPONSE_BYTES}")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError("parse", "响应体不是合法 JSON") from error
    if not isinstance(data, dict):
        raise ManifestError("schema", "清单顶层必须是对象")
    schema = data.get("schema")
    if schema != SCHEMA_V1:
        raise ManifestError("schema", f"未知清单 schema: {schema!r}")
    generated_at = _utc(data.get("generated_at"), "generated_at")
    channels = data.get("channels")
    if not isinstance(channels, dict) or "stable" not in channels:
        raise ManifestError("schema", "channels.stable 必须存在")
    stable_latest, stable_time = _channel(channels.get("stable"), "channels.stable")
    prerelease_latest = ""
    prerelease_time = ""
    if isinstance(channels.get("prerelease"), dict):
        prerelease_latest, prerelease_time = _channel(
            channels["prerelease"], "channels.prerelease"
        )
    releases_raw = data.get("releases")
    if not isinstance(releases_raw, list) or not releases_raw:
        raise ManifestError("schema", "releases 必须是非空数组")
    if len(releases_raw) > MAX_RELEASE_ENTRIES:
        raise ManifestError(
            "schema", f"releases 数量 {len(releases_raw)} 超过上限 {MAX_RELEASE_ENTRIES}"
        )
    releases = tuple(_release_entry(raw, idx) for idx, raw in enumerate(releases_raw))
    _check_channel_consistency("stable", stable_latest, _TAG_PUB, releases)
    if prerelease_latest:
        _check_channel_consistency(
            "prerelease", prerelease_latest, re.compile(r"^V([1-9]\d*)-(\d{10})-PRE$"), releases
        )
    return Manifest(
        schema=schema,
        generated_at=generated_at,
        stable_latest=stable_latest,
        stable_released_at=stable_time,
        prerelease_latest=prerelease_latest,
        prerelease_released_at=prerelease_time,
        releases=releases,
    )


def _check_channel_consistency(
    channel: str,
    latest: str,
    pattern: re.Pattern[str],
    releases: tuple["ReleaseEntry", ...],
) -> None:
    """Producer and reader must agree: the channel's `latest` is one of
    the entries in `releases`, AND it is the highest-ranked tag of the
    right kind.  Breaking this contract means truncation or misordering
    silently moved the "latest" pointer — the reader must refuse.
    """
    tags_in_releases = {entry.tag for entry in releases}
    if latest not in tags_in_releases:
        raise ManifestError(
            "schema",
            f"channels.{channel}.latest ({latest!r}) 不在 releases 列表中",
        )
    best_tag = ""
    best_key: tuple[int, int] | None = None
    for entry in releases:
        if not pattern.fullmatch(entry.tag):
            continue
        key = _tag_sort_key(entry.tag)
        if key is None:
            continue
        if best_key is None or key > best_key:
            best_key = key
            best_tag = entry.tag
    if best_tag and best_tag != latest:
        raise ManifestError(
            "schema",
            f"channels.{channel}.latest 应为 {best_tag!r}，但清单给了 {latest!r}",
        )
