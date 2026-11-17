"""The distill loop must hand the interpreter back between units.

A lease succeeds whenever the queue is deep, so an unbounded "more work -> go
straight round again" made the worker a spin: it held the GIL, kept re-taking
engine.LOCK, and every request thread queued behind it until the console looked
hung. The loop has to pause like the probe and reconcile loops do.
"""

from __future__ import annotations

import pytest

import rogmem.app as app


def test_the_distill_loop_pauses_even_when_work_keeps_coming(monkeypatch):
    monkeypatch.setattr(app.engine, "pump_one", lambda: "done")
    slept: list[float] = []

    def fake_sleep(seconds):
        slept.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(app.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        app._distill_loop()

    assert slept, "泵出一件活之后必须让出一次，否则就是忙循环"
    assert slept[0] > 0, slept


def test_a_unit_that_raises_does_not_take_the_worker_with_it(monkeypatch):
    """一个单元的异常不该让 worker 线程静默死掉：并发从 3 掉到 2 没人会知道。"""
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("一个单元崩了")
        return "done"

    monkeypatch.setattr(app.engine, "pump_one", flaky)
    slept: list[float] = []

    def fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(app.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        app._distill_loop()

    assert len(calls) >= 2, "崩过一次之后还要继续泵下一件"
    assert slept, "每一轮都要让出"
