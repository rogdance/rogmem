"""A detached door must not flash a terminal window on Windows.

Regression for the console-subprocess storm: an unreachable hub made the door
re-read the listen table every one or two seconds, and each netstat call became
a visible conhost / OpenConsole / WindowsTerminal -Embedding window.
"""

from __future__ import annotations

from installer import rogmem_installer as inst
from rogmem import plugin_cli, win_console

_TABLE = b"  TCP    127.0.0.1:18780    0.0.0.0:0    LISTENING    4242\n"


class _Result:
    def __init__(self, stdout=b""):
        self.stdout = stdout


def _capture(monkeypatch, *, windows: bool = True, stdout: bytes = _TABLE) -> list:
    calls: list = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _Result(stdout)

    monkeypatch.setattr(win_console, "is_windows", lambda: windows)
    monkeypatch.setattr(win_console.subprocess, "run", fake_run)
    monkeypatch.setattr(inst, "_netstat_snapshot", None)
    return calls


def test_netstat_probe_is_windowless_on_windows(monkeypatch):
    calls = _capture(monkeypatch, windows=True)
    text = inst._netstat_text(force=True)
    assert "18780" in text
    assert len(calls) == 1
    kwargs = calls[0][1]
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 3
    assert kwargs.get("creationflags", 0) & win_console.CREATE_NO_WINDOW


def test_netstat_probe_is_unchanged_on_posix(monkeypatch):
    calls = _capture(monkeypatch, windows=False)
    inst._netstat_text(force=True)
    assert len(calls) == 1
    assert "creationflags" not in calls[0][1]
    assert calls[0][0] == ["netstat", "-an"]


def test_netstat_snapshot_is_reused_within_ttl(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(inst.time, "monotonic", lambda: clock["now"])
    calls = _capture(monkeypatch, windows=True)
    first = inst._netstat_text()
    second = inst._netstat_text()
    assert first == second
    assert len(calls) == 1
    clock["now"] += inst._NETSTAT_TTL + 1
    inst._netstat_text()
    assert len(calls) == 2


def test_door_pid_is_windowless_on_windows(monkeypatch):
    calls = _capture(monkeypatch, windows=True)
    assert plugin_cli._door_pid() == 4242
    assert len(calls) == 1
    assert calls[0][1].get("creationflags", 0) & win_console.CREATE_NO_WINDOW


def test_binding_discovery_is_cooldowned_when_the_hub_is_unreachable(monkeypatch):
    clock = {"now": 200.0}
    monkeypatch.setattr(inst.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(inst, "_binding_snapshot", None)
    scans: list = []

    def fake_find_binding(*args, **kwargs):
        scans.append(args)
        return ""

    monkeypatch.setattr(inst, "find_binding", fake_find_binding)
    proxy = object.__new__(inst._Proxy)
    proxy.hub = "rh_test"
    proxy.server = None
    assert proxy._another_binding() == ""
    assert proxy._another_binding() == ""
    assert len(scans) == 1
    clock["now"] += inst._BINDING_TTL + 1
    assert proxy._another_binding() == ""
    assert len(scans) == 2


def test_binding_discovery_reuses_a_found_entrance(monkeypatch):
    clock = {"now": 300.0}
    monkeypatch.setattr(inst.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(inst, "_binding_snapshot", None)
    scans: list = []

    def fake_find_binding(*args, **kwargs):
        scans.append(args)
        return "http://127.0.0.1:18080"

    monkeypatch.setattr(inst, "find_binding", fake_find_binding)
    proxy = object.__new__(inst._Proxy)
    proxy.hub = "rh_test"
    proxy.server = None
    assert proxy._another_binding() == "http://127.0.0.1:18080"
    assert proxy._another_binding() == "http://127.0.0.1:18080"
    assert len(scans) == 1
