"""队列柱图：把时段和数量交给坐标轴，而不是只画一排矩形。

几何、虚线、刻度防碰撞走浏览器测（tests/test_queue_chart_browser.py）。
这里只守 payload：窗口、粒度和职责函数必须进页面。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from rogmem.ui import queue_page


_PAYLOAD_RE = re.compile(r"var QUEUE = (\{.*?\});", re.DOTALL)


def _payload(html: str) -> dict:
    match = _PAYLOAD_RE.search(html)
    assert match, "queue_page 应该在脚本里注入 `var QUEUE = {...};`"
    return json.loads(match.group(1))


def _series() -> list[dict]:
    start = datetime(2026, 11, 12, 2, tzinfo=timezone.utc)
    return [
        {
            "at": (start + timedelta(hours=i)).isoformat(),
            "label": "unused",
            "tasks": i,
            "chars": i * 100,
        }
        for i in range(8)
    ]


def _board(**changes) -> dict:
    start = datetime(2026, 11, 12, 2, tzinfo=timezone.utc)
    board = {
        "span": "24h",
        "tab": "all",
        "page": 1,
        "pages": 1,
        "page_size": 50,
        "total": 0,
        "rows": [],
        "counts": {"all": 0, "done": 0, "queued": 0, "leased": 0},
        "summary": {
            "done_tasks": 0,
            "done_chars": 0,
            "queued_tasks": 0,
            "queued_chars": 0,
            "per_hour_tasks": 0,
            "per_hour_chars": 0,
            "running": 0,
            "stopped": 0,
            "untimed_tasks": 0,
        },
        "series": _series(),
        "window": {
            "start": start.isoformat(),
            "end": (start + timedelta(hours=8)).isoformat(),
            "grain": "hour",
            "tz": "UTC+8",
        },
    }
    board.update(changes)
    return board


def test_payload_carries_window_grain_and_series_clocks():
    html = queue_page(_board())
    data = _payload(html)
    assert data["grain"] == "hour"
    assert data["window"]["start"].startswith("2026-11-12T02:00:00")
    assert data["window"]["end"].startswith("2026-11-12T10:00:00")
    assert data["window"]["tz"] == "UTC+8"
    assert data["series"][0]["at"].startswith("2026-11-12T02:00:00")
    assert data["series"][3]["tasks"] == 3
    assert data["series"][3]["chars"] == 300


def test_chart_script_names_the_scale_and_axis_duties():
    html = queue_page(_board())
    for name in ("buildYScale", "buildXTicks", "renderTooltip"):
        assert name in html
    assert 'id="q-svg"' in html
    assert 'aria-describedby="q-caption"' in html
    assert 'id="q-caption"' in html
    assert 'preserveAspectRatio="none"' not in html
    assert "preserveAspectRatio='none'" not in html


def test_live_board_window_is_the_one_the_chart_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine
    from rogmem.queue_board import view

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    now = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
    board = view("24h", "all", 1, now)
    data = _payload(queue_page(board))
    assert data["window"]["start"] == board["window"]["start"]
    assert data["window"]["end"] == board["window"]["end"]
    assert data["grain"] == board["window"]["grain"]
    assert data["window"]["tz"] == "UTC+8"
    assert len(data["series"]) == len(board["series"])
