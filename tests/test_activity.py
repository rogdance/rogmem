"""模型活动读数：队列状态页要能说清「系统此刻在不在调用模型」。

队列行只是萃取作业。向量补齐和词条整合不写 ``queue``，却同样在调模型；
只读 ``queue`` 的页面因此会在系统忙的时候报「没有任务在进行中」，和成本明细对不上。
这里钉住：模型调用一进一出都登记，页面据此报出此刻和刚刚的调用。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from rogmem import activity
from rogmem.activity import RECENT_SECONDS


def _open(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_SKIP_SCHEDULER", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


class _Answer:
    """最小的 urlopen 替身：一次读完的正文。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def test_a_call_is_open_while_it_runs_then_recent():
    activity.reset()
    with activity.track("身份合并"):
        opening = activity.snapshot()["open"]
        assert [item["worker"] for item in opening] == ["身份合并"]
        assert activity.snapshot()["recent"] == []
    after = activity.snapshot()
    assert after["open"] == []
    assert [item["worker"] for item in after["recent"]] == ["身份合并"]


def test_two_of_the_same_worker_are_one_open_reading():
    activity.reset()
    with activity.track("向量嵌入"):
        with activity.track("向量嵌入"):
            assert len(activity.snapshot()["open"]) == 1
        assert len(activity.snapshot()["open"]) == 1
    assert activity.snapshot()["open"] == []


def test_a_failed_call_still_clears():
    activity.reset()
    try:
        with activity.track("并页"):
            raise RuntimeError("掉线")
    except RuntimeError:
        pass
    assert activity.snapshot()["open"] == []
    assert [item["worker"] for item in activity.snapshot()["recent"]] == ["并页"]


def test_the_recent_window_expires():
    activity.reset()
    with activity.track("专名核对"):
        pass
    later = time.monotonic() + RECENT_SECONDS + 1
    assert activity.snapshot(now=later)["recent"] == []


def test_a_chat_call_is_visible_while_it_runs(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    activity.reset()
    seen: dict = {}

    def fake_urlopen(_req, timeout=None):
        seen["snapshot"] = activity.snapshot()
        return _Answer('{"choices":[{"message":{"content":"甲"},"finish_reason":"stop"}]}'.encode())

    monkeypatch.setattr(engine.urllib.request, "urlopen", fake_urlopen)
    from rogmem.cost import bind_worker

    with bind_worker("身份合并"):
        answer, _reason = engine._complete_chat(
            "http://example/",
            "k",
            {"model": "m", "messages": [{"role": "user", "content": "甲"}]},
            role="merge",
        )

    assert answer == "甲"
    assert [item["worker"] for item in seen["snapshot"]["open"]] == ["身份合并"]
    assert activity.snapshot()["open"] == []


def test_an_embedding_call_is_visible_while_it_runs(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    activity.reset()
    seen: dict = {}

    def fake_urlopen(_req, timeout=None):
        seen["snapshot"] = activity.snapshot()
        return _Answer('{"data":[{"index":0,"embedding":[0.5,0.5]}]}'.encode())

    monkeypatch.setattr(engine.urllib.request, "urlopen", fake_urlopen)
    vectors = engine._remote_embed_call({"base_url": "http://example/", "api_key": "k"}, "embed", ["甲"])

    assert vectors == [[0.5, 0.5]]
    assert [item["worker"] for item in seen["snapshot"]["open"]] == ["向量嵌入"]
    assert activity.snapshot()["open"] == []


def test_the_queue_page_reports_the_model_activity(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    moment = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
    from rogmem.queue_board import view
    from rogmem.ui import queue_page

    activity.reset()
    quiet = view("24h", "all", 1, moment)
    assert quiet["activity"]["open"] == []
    assert "此刻没有模型调用" in queue_page(quiet)

    with activity.track("身份合并"):
        busy = view("24h", "all", 1, moment)
    assert busy["activity"]["open"][0]["worker"] == "身份合并"
    page = queue_page(busy)
    assert "此刻在调用模型" in page
    assert "身份合并" in page
