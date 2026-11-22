"""`rogmem plugin`: see and update this computer's rogmem plugin against the hub's.

status  what this computer runs, what the hub serves, and each host's hook really running
update  take the hub's plugin and put it on every host of this computer, then prove it runs

The hub is the source. An update downloads /host-client.whl from the door this computer
already uses (the same identity check the bootstrap does), installs it into the Python
that runs the hooks, then runs the *new* code to rewrite rules, hooks, the Hermes provider
and the door launcher. No key is minted, no config is reset, no memory data moves.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

DOOR = "http://127.0.0.1:18780"


def _get(url: str, timeout: float = 10) -> bytes:
    opener = build_opener(ProxyHandler({}))
    with opener.open(Request(url, headers={"Cache-Control": "no-store"}), timeout=timeout) as resp:
        return resp.read()


def hub_plugin(door: str = DOOR) -> dict:
    """What the hub serves. Empty when the door is closed or the hub predates plugin versions."""
    try:
        data = json.loads(_get(door.rstrip("/") + "/plugin.json").decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _hub_identity(door: str) -> str:
    try:
        data = json.loads(_get(door.rstrip("/") + "/healthz", 5).decode("utf-8"))
    except Exception:
        return ""
    return str(data.get("hub") or "") if isinstance(data, dict) else ""


def status(home: Path, door: str = DOOR, verify: bool = True) -> dict:
    from rogmem import plugin

    local = plugin.installed(home)
    hub = hub_plugin(door)
    latest = str(hub.get("version") or "")
    installed = str(local.get("version") or "")
    verdict = plugin.compare(installed, latest) if latest else "hub-unknown"
    report = {
        "ok": True,
        "command": "plugin.status",
        "name": plugin.NAME,
        "installed": installed,
        "latest": latest,
        "status": verdict,
        "programs": local.get("programs") or [],
        "installed_at": local.get("installed_at") or "",
        "update": plugin.UPDATE_COMMAND,
    }
    if verify:
        from installer import rogmem_installer as inst

        rows = inst.verify_host_hooks(home)
        report["hooks"] = [
            {"program": row["program"], "ok": row["ok"], "last_event": inst.hook_heartbeat(home, row["program"])}
            for row in rows
        ]
        report["broken"] = sum(1 for row in rows if not row["ok"])
    report["ok"] = verdict in {plugin.CURRENT, "hub-unknown"} and not report.get("broken")
    if verdict == plugin.CURRENT:
        report["text"] = f"{plugin.NAME}是最新的（{installed}）。"
    elif verdict == "hub-unknown":
        report["text"] = "本机门口没有回答中枢的插件版本：门口没开，或中枢还是旧版本。"
    else:
        report["text"] = plugin.notice(verdict, installed, latest).split("回答末尾", 1)[0] + f"运行 {plugin.UPDATE_COMMAND} 更新。"
    return report


def _install_wheel(wheel: bytes, python: str) -> tuple[bool, str]:
    folder = Path(tempfile.mkdtemp(prefix="rogmem-plugin-"))
    path = folder / "rogmem_host-1.0.0-py3-none-any.whl"
    path.write_bytes(wheel)
    in_venv = subprocess.run(
        [python, "-c", "import sys; print(int(sys.prefix != sys.base_prefix))"],
        capture_output=True,
        text=True,
    ).stdout.strip() == "1"
    base = [python, "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "--upgrade", "--force-reinstall", str(path)]
    attempts = [base] if in_venv else [base + ["--user"], base + ["--user", "--break-system-packages"]]
    last = ""
    for command in attempts:
        done = subprocess.run(command, capture_output=True, text=True)
        if done.returncode == 0:
            return True, ""
        last = (done.stderr or done.stdout or "").strip().splitlines()[-1:] or [""]
        last = last[0]
    return False, last


def _door_command(home: Path, python: str) -> list[str]:
    hub = ""
    try:
        hub = (Path(home) / ".rogmem" / "hub").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    command = [python, "-m", "rogmem", "channel", "--session"]
    if hub:
        command.extend(["--hub", hub])
    windowless = Path(python).with_name("pythonw.exe")
    if os.name == "nt" and windowless.is_file():
        command[0] = str(windowless)
    return command


STARTUP_LAUNCHER = "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/rogmem-channel.vbs"


def write_door_launcher(home: Path, python: str) -> Path | None:
    """The next login starts the door from the plugin just installed, not from wherever it came before.

    Windows only: the per-user Startup folder (no Task Scheduler, no admin, no console window).
    macOS and Linux keep the launchd / systemd unit `channel` writes; their command already
    names the interpreter, which the update just refreshed in place.
    """
    if os.name != "nt":
        return None
    line = " ".join(f'""{part}""' for part in _door_command(home, python))
    text = "\r\n".join(
        [
            "' rogmem local door (127.0.0.1:18780). Written by the rogmem plugin. No credentials here.",
            "Option Explicit",
            "Dim sh",
            'Set sh = CreateObject("WScript.Shell")',
            f'sh.CurrentDirectory = "{home}"',
            f'sh.Run "{line}", 0, False',
            "",
        ]
    )
    path = Path(home) / STARTUP_LAUNCHER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _restart_door(home: Path, python: str) -> bool:
    """Put the new code behind 18780. The old door process keeps the old code until it is replaced."""
    from installer import rogmem_installer as inst

    pid = _door_pid()
    if pid:
        _stop(pid)
        for _ in range(50):
            if not inst.probe_origin(DOOR, "").get("ok"):
                break
            time.sleep(0.1)
    command = _door_command(home, python)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        command,
        cwd=str(home),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
        start_new_session=os.name != "nt",
    )
    for _ in range(100):
        if inst.probe_origin(DOOR, "").get("ok"):
            return True
        time.sleep(0.2)
    return False


def _door_pid() -> int:
    """The process listening on 18780, if this machine can tell."""
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True).stdout.decode("ascii", "replace")
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[1].endswith(":18780") and parts[3].upper() == "LISTENING":
                    return int(parts[4])
        else:
            out = subprocess.run(["lsof", "-tiTCP:18780", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
            return int(out.split()[0]) if out.strip() else 0
    except (OSError, ValueError, IndexError):
        return 0
    return 0


def _stop(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    try:
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
            if handle:
                ctypes.windll.kernel32.TerminateProcess(handle, 0)
                ctypes.windll.kernel32.CloseHandle(handle)
        else:
            os.kill(pid, 15)
    except OSError:
        pass


def update(home: Path, door: str = DOOR, restart_door: bool = True) -> dict:
    from rogmem import plugin

    before = str(plugin.installed(home).get("version") or "")
    hub = hub_plugin(door)
    latest = str(hub.get("version") or "")
    identity = _hub_identity(door)
    remembered = ""
    try:
        remembered = (Path(home) / ".rogmem" / "hub").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if not identity or (remembered and identity != remembered):
        return {"ok": False, "command": "plugin.update", "refused": "door-not-this-hub",
                "text": "本机门口没有回答这份中枢的身份，不从它取插件。先确认本机门口开着。"}
    try:
        wheel = _get(door.rstrip("/") + "/host-client.whl", 120)
    except Exception as exc:
        return {"ok": False, "command": "plugin.update", "refused": "download-failed", "text": f"下载插件失败：{type(exc).__name__}"}
    if not wheel.startswith(b"PK"):
        return {"ok": False, "command": "plugin.update", "refused": "not-a-wheel", "text": "门口返回的不是插件包。"}
    from installer import rogmem_installer as inst

    python = inst._hook_interpreter()
    ok, why = _install_wheel(wheel, python)
    if not ok:
        return {"ok": False, "command": "plugin.update", "refused": "pip-failed", "text": f"安装插件失败：{why}"}
    # The rewrite runs in the freshly installed code, not in this process's old modules.
    rewrite = subprocess.run(
        [python, "-m", "rogmem", "hosts", "update", "--confirm"],
        capture_output=True,
        cwd=str(home),
        env={**os.environ, "PYTHONPATH": ""},
    )
    try:
        written = json.loads(rewrite.stdout.decode("utf-8", "replace"))
    except ValueError:
        written = {}
    door_ok = None
    launcher = None
    if restart_door:
        launcher = write_door_launcher(home, python)
        door_ok = _restart_door(home, python)
    after = str(plugin.installed(home).get("version") or written.get("version") or "")
    verify = written.get("verify") or {}
    done = bool(written.get("ok")) and (not latest or after == latest) and not verify.get("broken")
    return {
        "ok": done,
        "command": "plugin.update",
        "from": before,
        "to": after,
        "latest": latest,
        "programs": written.get("programs") or [],
        "hooks": [{"program": row.get("program"), "ok": row.get("ok")} for row in verify.get("hooks") or []],
        "door_restarted": door_ok,
        "door_launcher": str(launcher) if launcher else "",
        "human_actions": verify.get("human_actions") or [],
        "text": (
            f"{plugin.NAME}已从 {before or '未知'} 更新到 {after}，本机 {len(written.get('programs') or [])} 个宿主的钩子都能跑。"
            if done
            else "更新没有完成：" + (written.get("text") or "钩子复核没过")
        ),
    }
