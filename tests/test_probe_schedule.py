"""Provider probes stay scheduled when another maintenance step fails or sticks.

The probe loop is its own thread. A raised step is caught, then that step waits
out a quiet window. The other steps still run on the next tick.
"""

from __future__ import annotations

import threading
import time

from rogmem import app, engine


def _silence_maintenance(monkeypatch, seen: list[str] | None = None) -> None:
    def mark(name):
        def run(*_args, **_kwargs):
            if seen is not None:
                seen.append(name)

        return run

    monkeypatch.setattr(engine, "scan_dir", mark("scan"))
    monkeypatch.setattr(engine, "probe_release_if_due", mark("release"))
    monkeypatch.setattr(engine, "run_scheduled_sweep", mark("sweep"))
    monkeypatch.setattr(engine, "run_scheduled_reconcile", mark("reconcile"))
    monkeypatch.setattr(engine, "sweep_host_import_jobs", mark("jobs"))
    monkeypatch.setattr(engine, "maintain", mark("maintain"))


def test_a_failed_step_does_not_skip_the_rest_of_the_tick(monkeypatch):
    seen: list[str] = []

    def boom(*_args, **_kwargs):
        seen.append("scan")
        raise RuntimeError("database is locked")

    _silence_maintenance(monkeypatch, seen)
    monkeypatch.setattr(engine, "scan_dir", boom)
    app.run_maintenance_tick({})
    assert seen == ["scan", "release", "sweep", "jobs", "maintain"]


def test_reconcile_tick_is_not_behind_scan(monkeypatch):
    seen: list[str] = []

    def stuck(*_args, **_kwargs):
        seen.append("scan")
        raise AssertionError("scan must not run on the reconcile tick")

    _silence_maintenance(monkeypatch, seen)
    monkeypatch.setattr(engine, "scan_dir", stuck)
    monkeypatch.setattr(engine, "run_scheduled_reconcile", lambda: seen.append("reconcile"))
    app.run_maintenance_tick({})
    assert "reconcile" not in seen
    app.run_reconcile_tick({})
    assert seen[-1] == "reconcile"


def test_probe_tick_is_not_inside_maintenance(monkeypatch):
    seen: list[str] = []
    _silence_maintenance(monkeypatch, seen)
    monkeypatch.setattr(engine, "probe_providers_if_due", lambda: seen.append("probe"))
    app.run_maintenance_tick({})
    assert "probe" not in seen
    app.run_probe_tick({})
    assert seen[-1] == "probe"


def test_a_failed_probe_waits_before_the_next_try(monkeypatch):
    calls: list[int] = []

    def boom():
        calls.append(1)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(engine, "probe_providers_if_due", boom)
    monkeypatch.setattr(app, "_schedule_backoff", lambda: 60.0)
    quiet: dict[str, float] = {}
    app.run_probe_tick(quiet)
    app.run_probe_tick(quiet)
    assert calls == [1]
    quiet["probe"] = time.monotonic() - 1
    app.run_probe_tick(quiet)
    assert calls == [1, 1]


def test_a_finished_probe_thread_is_started_again(monkeypatch):
    monkeypatch.setattr(app, "_probe_loop", lambda stop=None: None)
    app._PROBE_THREAD = None
    try:
        first = app.start_probe_loop()
        first.join(1)
        assert not first.is_alive()
        second = app.start_probe_loop()
        second.join(1)
        assert second is not first
        assert not second.is_alive()
    finally:
        app._PROBE_THREAD = None


def test_probe_loop_continues_after_a_failure(monkeypatch):
    calls = {"n": 0}
    done = threading.Event()

    def fake():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        done.set()

    monkeypatch.setattr(engine, "probe_providers_if_due", fake)
    monkeypatch.setattr(app, "_schedule_backoff", lambda: 0.02)
    monkeypatch.setattr(app, "_poll_seconds", lambda: 0.01)
    stop = threading.Event()
    thread = app.start_probe_loop(stop)
    assert done.wait(2)
    stop.set()
    thread.join(1)
    assert not thread.is_alive()
    assert calls["n"] >= 2
