"""Pure release-tag parser, comparator, and about-page view builder.

No network, no DB, no HTML. Converts stored state (last successful tag,
last check times, last error kind) into the dict the UI renders.
"""

from __future__ import annotations

import re

RELEASE_TAG = re.compile(r"^V([1-9]\d*)-(\d{10})-PUB$")
LOCAL_TAG = re.compile(r"^V([1-9]\d*)-(\d{10})(?:-(?:DEV|PUB|PRE))?$")

ERROR_MESSAGES = {
    "ok": "",
    "not_modified": "",
    "never_checked": "",
    "config_missing": "尚未配置发布核对源",
    "config_invalid": "发布核对源地址非法",
    "not_found": "核对源不存在",
    "rate_limited": "核对源请求过频，稍后再试",
    "server_error": "核对源暂时不可用",
    "timeout": "核对超时",
    "network": "核对源无法连通",
    "parse": "核对源返回了无法解析的内容",
    "schema": "核对源内容格式不被支持",
    "too_large": "核对源返回数据过大",
    "unknown": "核对没有完成",
}

TRANSIENT_KINDS = {"ok", "not_modified", "never_checked", ""}


def _key(pattern: re.Pattern[str], text: str) -> tuple[int, int] | None:
    match = pattern.fullmatch(text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def relation(local: str, latest: str) -> str:
    """How the local version compares to the last successful remote tag."""
    left = _key(LOCAL_TAG, local)
    right = _key(RELEASE_TAG, latest)
    if left is None or right is None:
        return ""
    if left == right:
        return "已是最新"
    if left < right:
        return "有新版本"
    return "本地较新"


def select_latest_from_tags(tags) -> str:
    """Pick the highest formal PUB tag from an iterable of candidates."""
    best = ""
    best_key = None
    for tag in tags or []:
        key = _key(RELEASE_TAG, str(tag or ""))
        if key is None:
            continue
        if best_key is None or key > best_key:
            best_key = key
            best = str(tag)
    return best


def error_text(kind: str, fallback: str = "") -> str:
    """Public, no-secret text for an `error_kind`."""
    return ERROR_MESSAGES.get(kind or "", fallback)


def present(
    local: str,
    *,
    latest: str = "",
    last_success_at: str = "",
    last_fail_at: str = "",
    error_kind: str = "",
    error_message: str = "",
    source_url: str = "",
) -> dict:
    """Assemble the dict the UI renders.

    `freshness` says whether the information shown describes the current
    probe's outcome (`"fresh"`), the last known good value with a newer
    failure overlaid (`"stale"`), or nothing at all (`"none"`).  A stale
    view MUST NOT display `已是最新/有新版本/本地较新` — those words
    describe *right now*, and right now the probe failed.  The UI shows
    the historical tag explicitly as `上次成功 V1-...`.

    Invariants:
    - A failure never says "已是最新" or masquerades as success.
    - The last successful tag is preserved across a failure, but only as
      `last_success_tag` and a historical display, never as a current
      `latest` with a live relation.
    - `never_checked`, `config_missing`, `config_invalid` are distinct
      states with their own displays.
    """
    local = local or ""
    latest = latest or ""
    last_success_at = last_success_at or ""
    last_fail_at = last_fail_at or ""
    error_kind = error_kind or ""
    source_url = source_url or ""
    friendly = error_text(error_kind, error_message)
    base = {
        "display": "",
        "relation": "",
        "detail": "",
        "error_kind": error_kind or "never_checked",
        "latest": "",
        "last_success_tag": latest,
        "checked_at": last_success_at,
        "last_success_at": last_success_at,
        "last_fail_at": last_fail_at,
        "source_url": source_url,
        "fail_detail": "",
        "freshness": "none",
    }
    if error_kind == "config_missing":
        base.update(display="尚未配置发布核对源", error_kind="config_missing")
        return base
    if error_kind == "config_invalid":
        base.update(display="发布核对源地址非法", error_kind="config_invalid")
        return base
    if error_kind in ("ok", "not_modified") and latest:
        base["display"] = latest
        base["latest"] = latest
        base["relation"] = relation(local, latest)
        base["freshness"] = "fresh"
        return base
    if error_kind and error_kind not in TRANSIENT_KINDS:
        if latest:
            base["display"] = f"上次成功 {latest}"
            base["fail_detail"] = friendly or error_message or "核对没有完成"
            base["freshness"] = "stale"
            return base
        base["display"] = f"请求失败，{friendly or error_message or '核对没有完成'}"
        base["freshness"] = "none"
        return base
    base["display"] = "未核对"
    base["error_kind"] = "never_checked"
    base["freshness"] = "none"
    return base
