"""闭合区间快照：和全量现算一致，缺的桶只补那一桶，当前队列不进快照。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from rogmem import cost
from rogmem.closed_span import hour_key


NOW = datetime(2026, 11, 15, 4, 30, tzinfo=timezone.utc)


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_SKIP_SCHEDULER", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _call(at: str, worker: str, model: str, usage):
    cost.record(at=at, worker=worker, provider_id="厂", model=model, usage=usage, prompt="问", answer="答")


def _put_queue(engine, sid: str, state: str, when: str, body: str):
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,filename,pipeline)
           VALUES(?,?,?,?,?,'已向量化待蒸馏','p1',1,?, '解析:ok~向量:ok')""",
        (sid, "file", when, sid, body, f"{sid}.md"),
    )
    engine.db().execute(
        """INSERT INTO queue(source_id,lane,state,attempts,enqueued_at,started_at,finished_at)
           VALUES(?,?,?,0,?,?,?)""",
        (sid, "p1", state, when, when if state in {"leased", "done"} else None, when if state == "done" else None),
    )
    engine.db().commit()


def test_closed_hours_plus_tail_match_a_full_fold(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    cost.save_book("7", [{"provider": "厂", "model": "甲", "currency": "CNY", "input": "2", "output": "4", "cache": "1"}])
    _call("2026-11-14T15:30:00+00:00", "速写", "甲", (1_000_000, 0, 0))
    _call("2026-11-14T16:30:00+00:00", "并页", "甲", (2_000_000, 0, 0))
    _call("2026-11-15T04:10:00+00:00", "速写", "甲", (3_000_000, 0, 0))
    folded = cost.overview(now=NOW, span="all", detail="all")
    with cost._store() as con:
        scanned = cost._group_sums(con, start=datetime(2026, 11, 14, 15, 30, tzinfo=timezone.utc), end=NOW, grain="day", worker="", model="")
    assert folded["summary"]["tokens"] == 6_000_000
    by_label = {item["label"]: item["tokens"] for item in folded["series"]}
    assert by_label["11-14"] == 1_000_000
    assert by_label["11-15"] == 5_000_000
    assert sum(int(row["input_tokens"] or 0) for row in scanned) == folded["summary"]["input"]
    warmed = cost.overview(now=NOW, span="all", detail="all", worker="速写")
    assert warmed["summary"]["tokens"] == 4_000_000
    with cost._store() as con:
        seen: list[str] = []
        con.set_trace_callback(lambda sql: seen.append(" ".join(sql.split())))
        cost._window_groups(
            con,
            start=datetime(2026, 11, 14, 15, 30, tzinfo=timezone.utc),
            end=NOW,
            grain="day",
            worker="",
            model="",
        )
    groups = [sql for sql in seen if "GROUP BY" in sql and "model_calls" in sql]
    assert groups
    assert all("at_jd" in sql for sql in groups)
    assert any("call_bucket" in sql for sql in seen)


def test_a_late_write_unseals_only_that_hour(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    _call("2026-11-13T00:00:00+00:00", "速写", "甲", (1, 0, 0))
    _call("2026-11-14T16:30:00+00:00", "速写", "甲", (100, 0, 0))
    cost.overview(now=NOW, span="all")
    key = hour_key(datetime(2026, 11, 14, 16, 30, tzinfo=timezone.utc))
    with cost._store() as con:
        assert con.execute("SELECT 1 FROM call_bucket_seal WHERE bucket=?", (key,)).fetchone()
    _call("2026-11-14T16:40:00+00:00", "速写", "甲", (50, 0, 0))
    with cost._store() as con:
        assert con.execute("SELECT 1 FROM call_bucket_seal WHERE bucket=?", (key,)).fetchone() is None
        seen: list[str] = []
        con.set_trace_callback(lambda sql: seen.append(" ".join(sql.split())))
        cost._window_groups(con, start=datetime(2026, 11, 14, 16, 0, tzinfo=timezone.utc), end=NOW, grain="hour", worker="", model="")
    refill = [sql for sql in seen if "GROUP BY" in sql and "model_calls" in sql and "at_jd" in sql]
    assert len(refill) == 2
    again = cost.overview(now=NOW, span="all")
    assert again["summary"]["tokens"] == 151


def test_sealing_one_hour_twice_is_idempotent(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    _call("2026-11-14T16:30:00+00:00", "速写", "甲", (80, 20, 0))
    key = hour_key(datetime(2026, 11, 14, 16, 30, tzinfo=timezone.utc))
    barrier = threading.Barrier(2)

    def once():
        barrier.wait()
        with cost._store(write=True) as con:
            cost._seal_call_hour(con, key)

    threads = [threading.Thread(target=once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with cost._store() as con:
        seals = con.execute("SELECT COUNT(*) FROM call_bucket_seal WHERE bucket=?", (key,)).fetchone()[0]
        tokens = con.execute("SELECT SUM(input_tokens) FROM call_bucket WHERE bucket=?", (key,)).fetchone()[0]
    assert seals == 1
    assert tokens == 80


def test_queue_page_does_not_seal_on_the_request(tmp_path, monkeypatch):
    """Admin reads with sync=False. Closed hours seal in the background, not on the switch."""

    engine = _open(tmp_path, monkeypatch)
    from rogmem import queue_board
    from rogmem.queue_board import view

    _put_queue(engine, "完成甲", "done", "2026-11-14T16:30:00+00:00", "已经写完")
    _put_queue(engine, "排队乙", "queued", "2026-11-15T04:00:00+00:00", "还在排队")
    sealed: list[list[str]] = []
    scheduled: list[list[str]] = []

    def boom(con, keys):
        sealed.append(list(keys))
        raise AssertionError("请求路径不该去封存闭合小时")

    monkeypatch.setattr(queue_board, "ensure_done_seals", boom)
    monkeypatch.setattr(queue_board, "_schedule_done_seals", lambda keys: scheduled.append(list(keys)))
    board = view("24h", "all", 1, NOW, sync=False)
    assert sealed == []
    assert scheduled
    assert board["summary"]["queued_tasks"] == 1
    assert board["summary"]["done_tasks"] == 1


def test_queue_depth_stays_live_and_done_hours_match(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem.queue_board import _series_scan, view

    _put_queue(engine, "排队甲", "queued", "2026-11-15T04:00:00+00:00", "排队的句子")
    _put_queue(engine, "在途乙", "leased", "2026-11-15T03:00:00+00:00", "在途")
    _put_queue(engine, "失败丙", "failed", "2026-11-15T02:00:00+00:00", "失败")
    _put_queue(engine, "完成丁", "done", "2026-11-14T16:30:00+00:00", "已经写完的一段")
    _put_queue(engine, "完成戊", "done", "2026-11-15T04:10:00+00:00", "还在这一小时")
    board = view("all", "all", 1, NOW)
    con = engine.db()
    con.execute("BEGIN")
    try:
        earliest = __import__("rogmem.queue_board", fromlist=["_earliest"])._earliest(con)
        from rogmem.cost import span_bounds

        start, grain = span_bounds("all", NOW, earliest)
        scanned, _untimed_tasks, _untimed_chars = _series_scan(con, "all", start, NOW, grain)
    finally:
        con.rollback()
    assert [(item["tasks"], item["chars"]) for item in board["series"]] == [
        (item["tasks"], item["chars"]) for item in scanned
    ]
    assert board["summary"]["queued_tasks"] == 1
    assert board["summary"]["running"] == 1
    assert board["summary"]["stopped"] == 1
    _put_queue(engine, "排队己", "queued", "2026-11-15T04:20:00+00:00", "又来一个")
    again = view("all", "all", 1, NOW)
    assert again["summary"]["queued_tasks"] == 2
    assert again["summary"]["done_tasks"] == board["summary"]["done_tasks"]
    engine.db().execute("UPDATE sources SET body=? WHERE id=?", ("改过的正文比原来长很多", "完成丁"))
    engine.db().commit()
    changed = view("all", "all", 1, NOW)
    assert changed["summary"]["done_chars"] != board["summary"]["done_chars"]
    assert changed["summary"]["done_chars"] == again["summary"]["done_chars"] - len("已经写完的一段") + len("改过的正文比原来长很多")


def test_named_lists_use_twenty_and_the_nav_does_not(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    for index in range(25):
        engine.db().execute(
            """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready)
               VALUES(?,?,?,'h',?,'已蒸馏','fast',1)""",
            (f"s{index}", "user", f"2026-04-01T00:{index:02d}:00+00:00", f"会话{index}"),
        )
        engine.db().execute(
            """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,filename)
               VALUES(?,?,?,'h',?,'已蒸馏','fast',1,?)""",
            (f"f{index}", "file", f"2026-04-02T00:{index:02d}:00+00:00", f"文件{index}", f"文件{index}.md"),
        )
    for index in range(60):
        engine.db().execute(
            "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES(?,?,?,'active','{}','{}')",
            (f"c{index:02d}", "人物", f"词条{index:02d}"),
        )
    engine.db().commit()
    assert engine.list_sources("session")["page_size"] == 20
    assert len(engine.list_sources("session")["rows"]) == 20
    assert engine.list_sources("file")["page_size"] == 20
    assert len(engine.crystal_nav_slice("", "active", 0)["items"]) == 50
    from rogmem.queue_board import PAGE_SIZE

    assert PAGE_SIZE == 20
    assert cost.PAGE == 20


def test_paging_refreshes_the_list_without_the_cards(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    moment = datetime(2026, 11, 15, 4, 0, tzinfo=timezone.utc)
    for index in range(25):
        _call(f"2026-11-14T03:{index:02d}:00+00:00", "速写", "甲", (1, 0, 0))
        _put_queue(engine, f"q{index}", "queued", f"2026-11-14T03:{index:02d}:00+00:00", f"队列{index}")
    from starlette.testclient import TestClient

    from rogmem.app import build_app

    client = TestClient(build_app())

    def explode(*_args, **_kwargs):
        raise AssertionError("翻页不该去折卡片")

    assert cost.view_key(span="24h", page=1) == cost.view_key(span="24h", page=2)
    from rogmem import ui

    assert 'searchParams.delete("page")' in ui._COST_PENDING_SCRIPT
    monkeypatch.setattr(cost, "board_and_prices", explode)
    calls = client.get("/admin/costs", params={"part": "calls", "page": "2", "detail": "all", "dworker": "速写", "span": "24h", "worker": "并页"})
    assert calls.status_code == 200
    assert "成本概览" not in calls.text
    assert "data-page='2'" in calls.text
    body = calls.text.split("</tbody>", 1)[0]
    assert body.count("<tr>") == 5
    assert "并页" not in body
    jumped = client.get("/admin/costs", params={"part": "calls", "page": "9", "detail": "all", "dworker": "速写"})
    assert "data-page='2'" in jumped.text
    monkeypatch.setattr("rogmem.queue_board._folded_done", explode)
    listed = client.get("/admin/settings/queue", params={"part": "queue", "page": "2", "span": "24h", "tab": "queued"})
    assert listed.status_code == 200
    assert "任务概览" not in listed.text
    assert "data-page='2'" in listed.text
    assert listed.text.count("<tr>") == 5
    assert "span=24h" in listed.text and "tab=queued" in listed.text
