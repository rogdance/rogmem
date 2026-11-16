"""维护时钟不许每一圈都跑一遍整表。

维护时钟按 ``_poll_seconds()``（默认 0.2 秒）转圈，而每一步都要走一张表、取
``engine.LOCK``。实测每一步一圈时锁被占掉近一半时间（每 10 秒 11-13 次、每次
约 0.35 秒），管理台的每次读都在锁后面排队。每一步有自己的最小间隔。
"""

from __future__ import annotations

from pathlib import Path

from rogmem import app

SCHEMA = Path(__file__).resolve().parents[1] / "rogmem" / "schema.sql"


def _install(monkeypatch, counts: dict):
    def steps():
        return tuple((name, (lambda name=name: counts.__setitem__(name, counts[name] + 1))) for name in counts)

    monkeypatch.setattr(app, "_maintenance_steps", steps)
    monkeypatch.setattr(app, "_MAINTENANCE_LAST", {})
    monkeypatch.setattr(app, "_MAINTENANCE_EVERY", {name: 15.0 for name in counts})


def test_a_second_pass_of_the_same_tick_runs_nothing(monkeypatch):
    counts = {"a": 0, "b": 0}
    _install(monkeypatch, counts)

    app.run_maintenance_tick()
    assert counts == {"a": 1, "b": 1}

    app.run_maintenance_tick()
    app.run_maintenance_tick()
    assert counts == {"a": 1, "b": 1}, "同一个时钟窗口里不该再走一遍整表"


def test_each_step_is_due_again_after_its_own_interval(monkeypatch):
    counts = {"a": 0}
    _install(monkeypatch, counts)
    clock = {"now": 1000.0}
    monkeypatch.setattr(app.time, "monotonic", lambda: clock["now"])

    app.run_maintenance_tick()
    assert counts["a"] == 1

    clock["now"] += 14.9
    app.run_maintenance_tick()
    assert counts["a"] == 1

    clock["now"] += 0.2
    app.run_maintenance_tick()
    assert counts["a"] == 2


def test_every_maintenance_step_has_an_interval():
    for name, _fn in app._maintenance_steps():
        assert name in app._MAINTENANCE_EVERY, name
        assert app._MAINTENANCE_EVERY[name] >= 5.0, name


def test_the_host_job_sweep_walks_an_index():
    text = SCHEMA.read_text(encoding="utf-8")
    for needed in ("host_jobs_due", "host_jobs_lease", "host_jobs_attempts"):
        assert needed in text, needed
