"""The rogmem plugin: everything the hub installs on a computer, under one version.

The plugin is the hook script, the global rules, the Hermes memory provider, the DSH
plugin, the local client (`python -m rogmem`) and the local door's autostart. They ship
together in /host-client.whl and are installed together by `rogmem plugin update`, so
they carry one version.

Two names for one plugin build:

- The **version** people read is ``P-V1-<pack time>-<channel>``, e.g. ``P-V1-2611221408-DEV``.
  The pack time is the server pack in which the plugin's content last changed, so a
  server-only release keeps the same P- version and no computer is asked to update.
  The hub assigns it (see ``engine.plugin_label_of``); it is never computed on a computer.
- The **id** machines compare is ``<series>.<12 hex>``: a digest of exactly the files in
  the wheel. It moves by itself on any change. Hooks report it, the hub stores it, the
  update compares it. The series is bumped by hand only when an older plugin must not
  keep running against this hub, and ``MIN_SERIES`` says which series still work.

The hub is the only authority. A computer whose plugin id differs from the hub's is
behind, even if it was built from a newer checkout: it updates to what the hub serves.

Stdlib only. This module ships in the wheel and is read by the hook's installer.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

NAME = "rogmem 插件"
SERIES = 2
MIN_SERIES = 2
HEADER = "X-Rogmem-Plugin"
VERSION_FILE = "PLUGIN_VERSION"
UPDATE_COMMAND = "python -m rogmem plugin update"

CURRENT = "current"
OUTDATED = "outdated"
UNREPORTED = "unreported"
INCOMPATIBLE = "incompatible"

_cached: str | None = None


def version() -> str:
    """The plugin **id** this code is (``<series>.<12 hex>``). For the P- label, ask the hub."""
    global _cached
    if _cached is not None:
        return _cached
    stamped = Path(__file__).resolve().parent / VERSION_FILE
    try:
        text = stamped.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if not text:
        try:
            # A source checkout has the packer; a client install never needs it.
            packer = importlib.import_module("rogmem.host_client")
            text = packer.plugin_version_of(packer.client_files())
        except Exception:
            text = ""
    _cached = text
    return text


def series_of(value: str) -> int:
    head = (value or "").split(".", 1)[0]
    return int(head) if head.isdigit() else 0


def compare(reported: str, latest: str) -> str:
    """Where a host stands against the hub's plugin."""
    reported = (reported or "").strip()
    latest = (latest or "").strip()
    if not reported:
        return UNREPORTED
    if series_of(reported) < MIN_SERIES:
        return INCOMPATIBLE
    if latest and reported != latest:
        return OUTDATED
    return CURRENT


def short(value: str) -> str:
    return (value or "").strip() or "—"


def manifest_path(home: Path) -> Path:
    return Path(home) / ".rogmem" / "plugin.json"


def installed(home: Path) -> dict:
    """What `rogmem plugin update` / `hosts update` last put on this computer."""
    for path in (manifest_path(home), Path(home) / ".rogmem" / "host-bundle.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            found = str(data.get("version") or data.get("rev") or "").strip()
            if found:
                return {**data, "version": found}
    return {}


def record_installed(home: Path, programs: list[str], python: str = "") -> dict:
    """Write the local manifest. The hook reads it and reports the version on every call."""
    current = installed(home)
    merged = sorted({*(current.get("programs") or []), *programs}) if current.get("version") == version() else sorted(set(programs))
    data = {
        "name": NAME,
        "version": version(),
        "label": str(current.get("label") or "") if current.get("version") == version() else "",
        "series": SERIES,
        "programs": merged,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "python": python,
    }
    path = manifest_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / (path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    # The older marker name, for a client that still reads it.
    (Path(home) / ".rogmem" / "host-bundle.json").write_text(
        json.dumps({"rev": data["version"], "programs": merged}, ensure_ascii=False), encoding="utf-8"
    )
    return data


OLD_LABEL = "旧版本"


def notice(status: str, reported: str, latest: str, program: str = "") -> str:
    """What the agent tells the person when this host is behind. Empty when it is current.

    ``reported`` and ``latest`` are P- labels; a plugin the hub never packed reads 旧版本.
    """
    who = f"{program} 的" if program else "本机"
    reported = reported or OLD_LABEL
    if status == CURRENT:
        return ""
    if status == UNREPORTED:
        head = f"{who} {NAME}没有上报版本：多半是旧插件，或钩子在这台电脑上没跑起来。最新是 {latest}。"
    elif status == INCOMPATIBLE:
        head = f"{who} {NAME}是 {reported}，已经不再兼容中枢的 {latest}，必须更新。"
    else:
        head = f"{who} {NAME}是 {reported}，中枢最新是 {latest}。"
    return (
        head
        + f"回答末尾用一句话提醒用户更新；用户同意就在这台电脑上运行 `{UPDATE_COMMAND}`，"
        "它只更新本机插件，不换钥匙、不动记忆数据。"
    )


def hub_manifest(hub: str = "", label: str = "", known: dict | None = None) -> dict:
    """What the hub publishes at /plugin.json. ``version`` is the P- label, ``id`` the digest."""
    return {
        "name": NAME,
        "version": label,
        "id": version(),
        "known": dict(known or {}),
        "series": SERIES,
        "min_series": MIN_SERIES,
        "hub": hub,
        "header": HEADER,
        "update": UPDATE_COMMAND,
    }


def remember_label(home: Path, label: str) -> None:
    """Write the hub's P- label for the installed id into the local manifest."""
    path = manifest_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    data["label"] = label
    temp = path.parent / (path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
