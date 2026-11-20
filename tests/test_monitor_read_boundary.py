"""监控页的读取边界。

切换到队列状态、以及离开成本明细之后，请求不能再把整本账折进这次绘制。
概况是聚合，明细是一页，没人在看的成本汇总不再占着进程。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rogmem import cost

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


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


def test_queue_page_reads_cards_for_one_page_only(tmp_path, monkeypatch):
    """活队列再长，带预览和阶段的那次读取也只取当前页。"""

    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    recent = (NOW - timedelta(minutes=5)).isoformat()
    rows = []
    for i in range(120):
        sid = f"q{i}"
        rows.append((sid, recent))
    con.executemany(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,filename,pipeline)
           VALUES(?, 'file', ?, ?, '正文', '已向量化待萃取', 'p1', 1, ?, '解析:ok~向量:ok')""",
        [(sid, recent, sid, f"{sid}.md") for sid, recent in rows],
    )
    con.executemany(
        """INSERT INTO queue(source_id,lane,state,attempts,enqueued_at)
           VALUES(?, 'p1', 'queued', 0, ?)""",
        rows,
    )
    con.commit()

    seen: list[str] = []

    def trace(statement: str) -> None:
        if statement:
            seen.append(" ".join(statement.split()))

    con.set_trace_callback(trace)
    from rogmem.queue_board import PAGE_SIZE, view

    board = view("24h", "queued", 1, NOW)
    con.set_trace_callback(None)

    assert board["summary"]["queued_tasks"] == 120
    assert board["total"] == 120
    assert len(board["rows"]) == PAGE_SIZE
    card_reads = [sql for sql in seen if "body_preview" in sql.lower() or "pipeline" in sql.lower()]
    assert card_reads, seen
    assert all("limit" in sql.lower() for sql in card_reads), card_reads


def test_chart_fold_does_not_materialize_each_call(tmp_path, monkeypatch):
    """成本走势按组加总。不能先把窗口里每一次调用装进 Python 再加。"""

    _open(tmp_path, monkeypatch)
    moment = datetime.now(timezone.utc)
    for i in range(3):
        cost.record(
            at=(moment - timedelta(hours=i + 1)).isoformat(),
            worker="草稿",
            provider_id="p",
            model="m",
            usage=(1_000_000, 0, 0),
            prompt="甲",
            answer="乙",
        )

    def explode(*_args, **_kwargs):
        raise AssertionError("走势仍在逐条装载调用")

    monkeypatch.setattr(cost, "_calls", explode)
    outcome = cost._refresh_board_snap({"now": moment, "span": "24h", "detail": "30d", "page": 1})
    assert outcome == "ok"
    board, _prices = cost.board_and_prices(now=moment, span="24h", detail="30d", page=1)
    assert board["summary"]["tokens"] == 3_000_000


def test_a_ledger_change_does_not_schedule_a_fold_nobody_is_reading():
    """调用记下来只让已有读数过期。没人打开成本页时不因此再折一遍。"""

    cost._clear_observations()
    cost._BOARD_WAKE.clear()
    cost._bump_inputs()
    assert not cost._BOARD_WAKE.is_set()


def test_status_probe_does_not_wait_out_the_gather(tmp_path, monkeypatch):
    """进度探询只读当前观测。它不能占着绘制下一次切换的工人干等汇总。"""

    _open(tmp_path, monkeypatch)
    cost._clear_observations()
    monkeypatch.setattr(cost, "VIEW_WAIT_SECONDS", 2)
    monkeypatch.setattr(cost, "_refresh_board_snap", lambda target=None: None)
    from starlette.testclient import TestClient

    from rogmem.app import build_app

    client = TestClient(build_app())
    started = time.perf_counter()
    response = client.get("/admin/costs", params={"span": "24h", "detail": "30d", "observe": "1"})
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    assert response.json()["state"] == "pending"
    assert elapsed < 0.5
