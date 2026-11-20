"""队列状态页：时段里的完成量，和此刻整条队列。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from rogmem.ui import queue_page


def _open(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _put(engine, sid: str, *, kind: str, lane: str, state: str, ingested_at: str, attempts: int = 0, filename: str = "", body: str = "一句正文"):
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,filename,pipeline)
           VALUES(?,?,?,?,?,'已向量化待萃取',?,1,?, '解析:ok~向量:ok')""",
        (sid, kind, ingested_at, sid, body, lane, filename),
    )
    engine.db().execute(
        """INSERT INTO queue(source_id,lane,state,attempts,enqueued_at,started_at,finished_at)
           VALUES(?,?,?,?,?,?,?)""",
        (
            sid,
            lane,
            state,
            attempts,
            ingested_at,
            ingested_at if state in {"leased", "done"} else None,
            ingested_at if state == "done" else None,
        ),
    )
    engine.db().commit()


def test_a_long_call_may_stay_quiet_longer_and_a_stream_is_one_answer():
    from rogmem.engine import CHAT_SILENCE_CAP, CHAT_TIMEOUT_SEC, _assemble_chat, _chat_silence_seconds

    assert _chat_silence_seconds({"messages": [{"content": "hi"}]}) == CHAT_TIMEOUT_SEC
    long = _chat_silence_seconds({"messages": [{"content": "甲" * 200_000}]})
    assert CHAT_TIMEOUT_SEC < long <= CHAT_SILENCE_CAP
    streamed = "\n".join(
        [
            'data: {"choices":[{"delta":{"content":"甲"}}]}',
            'data: {"choices":[{"delta":{"content":"在"},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )
    answer, reason, _raw = _assemble_chat(streamed)
    assert answer == "甲在"
    assert reason == "stop"
    _empty, _finish, raw = _assemble_chat(
        '{"choices":[{"message":{"content":"完整一句"},"finish_reason":"stop"}]}'
    )
    assert raw["choices"][0]["message"]["content"] == "完整一句"


def test_queue_page_lists_the_live_line_and_the_window(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    now = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
    fresh = now.isoformat()
    older = (now - timedelta(hours=5)).isoformat()
    yesterday = (now - timedelta(days=2)).isoformat()
    _put(engine, "fresh", kind="user", lane="p0", state="queued", ingested_at=fresh, filename="", body="刚进来的一句，还没有试过。")
    _put(engine, "retry", kind="user", lane="p0", state="queued", ingested_at=older, attempts=2, body="更早的一句，已经超时过。")
    _put(engine, "file", kind="file", lane="p2", state="queued", ingested_at=older, filename="说明文档.md", body="文件正文")
    _put(engine, "ran", kind="file", lane="p3", state="leased", ingested_at=fresh, filename="正在跑.txt", body="跑")
    hour_ago = (now - timedelta(hours=1)).isoformat()
    _put(engine, "old-done", kind="file", lane="p3", state="done", ingested_at=yesterday, filename="前天完成.txt", body="完成了的正文")
    _put(engine, "new-done", kind="file", lane="p3", state="done", ingested_at=hour_ago, filename="一小时前完成.txt", body="刚刚完成的正文")

    from rogmem.queue_board import view

    board = view("24h", "queued", 1, now)
    names = [row["name"] for row in board["rows"]]
    assert names == ["刚进来的一句，还没有试过。", "更早的一句，已经超时过。", "说明文档.md"]
    assert board["summary"]["queued_tasks"] == 3
    assert board["summary"]["running"] == 1
    assert board["summary"]["done_tasks"] == 1
    done_names = [row["name"] for row in view("24h", "done", 1, now)["rows"]]
    assert done_names == ["一小时前完成.txt"]
    assert board["rows"][0]["priority"] == "P0"
    assert board["rows"][0]["stage"] == "草稿"

    page = queue_page(board)
    assert "队列状态" in page
    side = page.split("<nav class='side'>", 1)[1].split("</nav>", 1)[0]
    assert "class='on' href='/admin/settings/queue'>队列状态" in side
    assert "成本明细" in side and "运行日志" in side
    assert "基础设置" not in side
    assert "任务概览" in page
    assert "入列时间" in page
    assert "完成耗时" in page
    assert "data-page-size='20'" in page
    long = "字" * 40
    assert long not in page
    clipped = queue_page(view("all", "all", 1, now))
    assert "前天完成.txt" in clipped
    assert "未跑完" in page or "进行中" in page
