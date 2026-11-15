"""队列读模型：概况、tab、列表是同一次分桶。

根因（改代码前由本文件的红测钉住；不能访问用户库）：

可证明。概况、走势和当前页在同一次事务里算完。带文件名和阶段的列只有这一页。
活队列不看时段，所以时段外或时钟坏掉的排队行仍在明细里。已完成才看时段；
无法解析的完成时间在「历史至今」里计入总数，不编进走势的某一格。
tab、列表和「全部」里的排队行只看 ``state == 'queued'``。进行中只看 ``leased``。
截图 302 = 1 + 298 + 3 能自洽，同时概况仍是 191。重试、挂起、解析失败只要还是 queued，两边状态谓词相同，
本身不造成分叉。页大小 50 只切当前页。191 和 298 都不是截断边界。

假设。用户那 107 条是近 24 小时以外、或入列时钟无法解析的 queued 行。代码只有这一条分叉；
具体是哪 107 条，没有库就不能点名。

不变量。活状态（queued / leased / failed，以及仍在队里的未识别状态）不看时段。
已完成、走势、平均每小时只看所选时段。概况排队项数 = 概况排队字数所加的那一组 = tab 排队数 = 排队列表总条数。
换页、换 tab 不改变这组数。换时段不改变活队列，可以改变已完成。0 行是成功的空快照。读取失败不得渲染成 0。
同一次返回内部自洽；下一次读取看到已提交的新状态，不把两次混进一个返回。

边界。不改分页器，不加缓存。字数用 ``source_cards.body_chars``（入库时 SQLite ``length``，对 TEXT 是字符数）。
不把未跑完并进排队，不把时段外的已完成并进「全部」。
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rogmem.queue_board import PAGE_SIZE, view
from rogmem.ui import queue_page

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
INSIDE = 191
DAYS3 = 100
DAYS40 = 6
BAD_CLOCK = 1
LIVE = INSIDE + DAYS3 + DAYS40 + BAD_CLOCK
LEASED = 3
SUSPEND_UNTIL = "9999-01-01T00:00:00+00:00"


def open_engine(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_SKIP_SCHEDULER", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _insert(engine, rows: list[dict]) -> None:
    con = engine.db()
    for row in rows:
        con.execute(
            """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,filename,pipeline)
               VALUES(?,?,?,?,?,'已向量化待蒸馏',?,1,?,?)""",
            (
                row["sid"],
                row.get("kind", "file"),
                row["ingested_at"],
                row["sid"],
                row["body"],
                row.get("lane", "p1"),
                row.get("filename", ""),
                row.get("pipeline", "解析:ok~向量:ok"),
            ),
        )
        con.execute(
            """INSERT INTO queue(source_id,lane,state,attempts,last_error,next_at,enqueued_at,started_at,finished_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                row["sid"],
                row.get("lane", "p1"),
                row["state"],
                row.get("attempts", 0),
                row.get("last_error"),
                row.get("next_at"),
                row.get("enqueued_at", row["ingested_at"]),
                row.get("started_at"),
                row.get("finished_at"),
            ),
        )
    con.commit()


def _row(sid: str, *, state: str, when: str, body: str, **extra) -> dict:
    item = {"sid": sid, "state": state, "ingested_at": when, "enqueued_at": when, "body": body}
    if state in {"leased", "done"}:
        item["started_at"] = when
    if state == "done":
        item["finished_at"] = when
    item.update(extra)
    return item


def _at(delta: timedelta) -> str:
    return (NOW - delta).isoformat()


def seed_screenshot(engine) -> dict:
    """191 条在近 24 小时内，107 条在窗外，另有 3 条进行中和 3 条完成时间不同的已完成。

    107 = 100 条 3 天前 + 6 条 40 天前 + 1 条时钟无法解析。
    3 天前和 40 天前里夹着重试、挂起和解析失败，用来证明分叉不靠状态名。
    """

    recent = _at(timedelta(minutes=30))
    days3 = _at(timedelta(days=3))
    days40 = _at(timedelta(days=40))
    rows: list[dict] = []
    number = 0

    def add(count: int, body: str, when: str, **extra) -> None:
        nonlocal number
        for _ in range(count):
            number += 1
            rows.append(_row(f"q{number}", state="queued", when=when, body=body, **extra))

    add(170, "a" * 10, recent)
    add(10, "a" * 10, recent, attempts=4, next_at=SUSPEND_UNTIL, last_error="这一次没有返回")
    add(11, "a" * 10, recent, pipeline="解析:fail", filename="解析失败.txt")
    add(90, "b" * 4, days3)
    add(5, "b" * 4, days3, attempts=3, next_at=SUSPEND_UNTIL, last_error="还在退避")
    add(5, "b" * 4, days3, pipeline="解析:fail")
    add(4, "c" * 3, days40)
    add(1, "c" * 3, days40, attempts=2, next_at=SUSPEND_UNTIL)
    add(1, "c" * 3, days40, enqueued_at="", ingested_at=days40)
    rows.append(
        _row(
            "q-bad-clock",
            state="queued",
            when="not-a-clock",
            body="队列",
            enqueued_at="not-a-clock",
            ingested_at="still-not",
        )
    )
    for index in range(LEASED):
        rows.append(_row(f"run-{index}", state="leased", when=recent, body="f"))
    rows.append(_row("done-recent", state="done", when=recent, body="e" * 58, filename="刚刚完成.txt"))
    rows.append(_row("done-3d", state="done", when=days3, body="d" * 10))
    rows.append(_row("done-40d", state="done", when=days40, body="g" * 7))
    _insert(engine, rows)
    return {
        "inside": INSIDE,
        "days3": DAYS3,
        "days40": DAYS40,
        "bad_clock": BAD_CLOCK,
        "live": LIVE,
        "live_chars": 191 * 10 + 100 * 4 + 6 * 3 + len("队列"),
        "leased": LEASED,
        "done_24h": 1,
        "done_24h_chars": 58,
        "done_7d": 2,
        "done_7d_chars": 68,
        "done_30d": 2,
        "done_all": 3,
        "done_all_chars": 75,
    }


def _sql_queued(engine) -> tuple[int, int, set[int]]:
    found = engine.db().execute(
        """SELECT q.id AS qid, COALESCE(c.body_chars, 0) AS chars
           FROM queue q LEFT JOIN source_cards c ON c.id = q.source_id
           WHERE q.state = 'queued'"""
    ).fetchall()
    return len(found), sum(int(row["chars"] or 0) for row in found), {int(row["qid"]) for row in found}


def _walk(span: str, tab: str, now: datetime) -> tuple[dict, list[dict]]:
    first = view(span, tab, 1, now)
    collected = list(first["rows"])
    for page in range(2, int(first["pages"]) + 1):
        board = view(span, tab, page, now)
        collected.extend(board["rows"])
    return first, collected


def test_rows_inside_the_window_already_agree(tmp_path, monkeypatch):
    """对照：全部落在 24 小时内时，概况和明细本来就一致。分叉不是页大小，也不是状态名。"""

    engine = open_engine(tmp_path, monkeypatch)
    recent = _at(timedelta(minutes=5))
    rows = [_row(f"in-{i}", state="queued", when=recent, body="a") for i in range(51)]
    rows.append(_row("run", state="leased", when=recent, body="b"))
    rows.append(_row("stop", state="failed", when=recent, body="c", attempts=5, last_error="停了"))
    rows.append(_row("fin", state="done", when=recent, body="d" * 4))
    _insert(engine, rows)

    board = view("24h", "queued", 2, NOW)
    listed = board["rows"]
    assert board["summary"]["queued_tasks"] == board["counts"]["queued"] == board["total"] == 51
    assert board["summary"]["queued_chars"] == 51
    assert len(listed) == 1
    assert board["page"] == 2
    assert board["pages"] == 2
    assert board["summary"]["running"] == board["counts"]["leased"] == 1
    assert board["summary"]["stopped"] == 1
    assert board["summary"]["done_tasks"] == board["counts"]["done"] == 1
    assert view("24h", "all", 1, NOW)["counts"]["all"] == 54
    clamped = view("nope", "nope", 0, NOW)
    assert clamped["span"] == "24h" and clamped["tab"] == "all" and clamped["page"] == 1


def test_screenshot_scale_queued_count_is_the_live_line(tmp_path, monkeypatch):
    engine = open_engine(tmp_path, monkeypatch)
    seeded = seed_screenshot(engine)
    live_n, live_chars, live_ids = _sql_queued(engine)
    assert live_n == seeded["live"] == LIVE
    assert live_chars == seeded["live_chars"]
    assert seeded["inside"] + seeded["days3"] + seeded["days40"] + seeded["bad_clock"] == LIVE

    ladder = {}
    listed_ids: set[int] = set()
    for span, done_n, done_chars, all_n in (
        ("24h", seeded["done_24h"], seeded["done_24h_chars"], LIVE + LEASED + seeded["done_24h"]),
        ("7d", seeded["done_7d"], seeded["done_7d_chars"], LIVE + LEASED + seeded["done_7d"]),
        ("30d", seeded["done_30d"], seeded["done_7d_chars"], LIVE + LEASED + seeded["done_30d"]),
        ("all", seeded["done_all"], seeded["done_all_chars"], LIVE + LEASED + seeded["done_all"]),
    ):
        for tab in ("all", "done", "queued", "leased"):
            board = view(span, tab, 1, NOW)
            assert board["counts"]["queued"] == LIVE
            assert board["summary"]["done_tasks"] == board["counts"]["done"] == done_n
            assert board["summary"]["done_chars"] == done_chars
            assert board["counts"]["leased"] == board["summary"]["running"] == LEASED
            assert board["summary"]["stopped"] == 0
            assert board["counts"]["all"] == all_n
            assert sum(point["tasks"] for point in board["series"]) == done_n
            assert sum(point["chars"] for point in board["series"]) == done_chars
            if tab == "queued":
                ladder[span] = (
                    board["summary"]["queued_tasks"],
                    board["summary"]["queued_chars"],
                    board["counts"]["queued"],
                    board["total"],
                )
        queued, rows = _walk(span, "queued", NOW)
        assert queued["total"] == LIVE
        assert queued["pages"] == 6
        assert len(rows) == LIVE
        assert len({row["qid"] for row in rows}) == LIVE
        assert sum(int(row["chars"]) for row in rows) == live_chars
        beyond = view(span, "queued", 99, NOW)
        assert beyond["page"] == 6
        assert beyond["total"] == LIVE
        assert len(beyond["rows"]) == LIVE - 5 * PAGE_SIZE
        if span == "24h":
            listed_ids = {int(row["qid"]) for row in rows}
            hour = view("24h", "done", 1, NOW)
            assert abs(hour["summary"]["per_hour_tasks"] - 1 / 24) < 1e-9
            assert abs(hour["summary"]["per_hour_chars"] - 58 / 24) < 1e-9
    assert listed_ids == live_ids
    expect = (LIVE, live_chars, LIVE, LIVE)
    assert ladder == {span: expect for span in ("24h", "7d", "30d", "all")}, (
        f"各时段(概况项,概况字,tab,列表) {ladder}，期望每格 {expect}。"
        f"种子：24h 内 {INSIDE} 条共 {INSIDE * 10} 字，3 天前 {DAYS3} 条共 {DAYS3 * 4} 字，"
        f"40 天前 {DAYS40} 条共 {DAYS40 * 3} 字，时钟无法解析 {BAD_CLOCK} 条共 {len('队列')} 字。"
        "tab 和翻页后的列表已经是活队列。修前概况读数是 24h=(191,1910)、7d=(291,2310)、"
        "30d=(291,2310)、all=(298,2330)：7 天比 24 小时多出的 100 条 400 字正是 3 天前那一桶；"
        "40 天前的 18 字和无法解析的 2 字只在历史至今出现。30 天不会比 7 天多，因为那 6 条已经超过 30 天。"
    )


def test_odd_states_stay_in_one_bucket(tmp_path, monkeypatch):
    engine = open_engine(tmp_path, monkeypatch)
    recent = _at(timedelta(hours=2))
    old = _at(timedelta(days=9))
    rows = [
        _row("wait", state="queued", when=recent, body="甲"),
        _row("retry", state="queued", when=old, body="乙", attempts=4, next_at=SUSPEND_UNTIL, last_error="退避"),
        _row("parse", state="queued", when=old, body="丙", pipeline="解析:fail"),
        _row("bad", state="queued", when="not-a-clock", body="丁", enqueued_at="??", ingested_at="??"),
        _row("blank", state="queued", when=old, body="戊", enqueued_at=""),
        _row("run", state="leased", when=old, body="己"),
        _row("stop", state="failed", when=recent, body="庚", attempts=5, last_error="停了"),
        _row("fin", state="done", when=recent, body="辛" * 3),
        _row("old-fin", state="done", when=old, body="壬"),
        _row("held", state="held", when=recent, body="癸"),
        _row("dup-src", state="queued", when=old, body="双"),
    ]
    _insert(engine, rows)
    engine.db().execute(
        "INSERT INTO queue(source_id,lane,state,attempts,enqueued_at) VALUES('dup-src','p1','queued',0,?)",
        (old,),
    )
    engine.db().execute("DELETE FROM source_cards WHERE id='blank'")
    engine.db().commit()

    board = view("24h", "all", 1, NOW)
    by_state: dict[str, list[dict]] = {}
    for row in board["rows"]:
        by_state.setdefault(row["state"], []).append(row)
    # blank 的卡片被删掉，任务还在，字数是 0。dup-src 有两行。
    assert board["summary"]["queued_tasks"] == board["counts"]["queued"] == 7
    assert board["summary"]["queued_chars"] == len("甲") + len("乙") + len("丙") + len("丁") + len("双") * 2
    assert board["counts"]["leased"] == board["summary"]["running"] == 1
    assert board["summary"]["stopped"] == 1
    assert board["summary"]["done_tasks"] == 1
    assert board["counts"]["all"] == 7 + 1 + 1 + 1 + 1
    assert {row["source_id"] for row in by_state["queued"]} == {"wait", "retry", "parse", "bad", "blank", "dup-src"}
    assert sum(1 for row in by_state["queued"] if row["source_id"] == "dup-src") == 2
    assert by_state["held"][0]["state_label"] == "held"
    assert "old-fin" not in {row["source_id"] for row in board["rows"]}
    week = view("7d", "done", 1, NOW)
    assert week["summary"]["done_tasks"] == 1
    assert week["summary"]["queued_tasks"] == 7
    month = view("30d", "done", 1, NOW)
    assert {row["source_id"] for row in month["rows"]} == {"fin", "old-fin"}


def test_done_window_reapplies_after_the_sql_slack(tmp_path, monkeypatch):
    engine = open_engine(tmp_path, monkeypatch)
    sliver = _at(timedelta(days=30, hours=2))
    _insert(engine, [_row("sliver", state="done", when=sliver, body="x" * 5)])
    board = view("30d", "done", 1, NOW)
    assert board["summary"]["done_tasks"] == 0
    assert board["counts"]["done"] == 0
    assert board["rows"] == []
    assert view("all", "done", 1, NOW)["summary"]["done_tasks"] == 1


def test_empty_snapshot_is_a_real_zero(tmp_path, monkeypatch):
    open_engine(tmp_path, monkeypatch)
    board = view("24h", "all", 1, NOW)
    assert board["summary"]["queued_tasks"] == 0
    assert board["summary"]["queued_chars"] == 0
    assert board["summary"]["done_tasks"] == 0
    assert board["counts"] == {"all": 0, "done": 0, "queued": 0, "leased": 0}
    assert board["rows"] == []
    assert board["pages"] == 1
    page = queue_page(board)
    assert "这一页还没有任务" in page
    assert page.count(">0 项<") == 3
    for span in ("24h", "7d", "30d", "all"):
        assert view(span, "queued", 1, NOW)["summary"]["queued_tasks"] == 0


def test_read_failure_is_not_rendered_as_zero(tmp_path, monkeypatch):
    open_engine(tmp_path, monkeypatch)

    def explode(*_args, **_kwargs):
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr("rogmem.queue_board._fetch", explode)
    with pytest.raises(RuntimeError, match="snapshot failed"):
        view("24h", "all", 1, NOW)

    from starlette.testclient import TestClient

    from rogmem.app import build_app

    response = TestClient(build_app(), raise_server_exceptions=False).get("/admin/settings/queue")
    assert response.status_code >= 500
    assert "任务概览" not in response.text
    assert "0 项" not in response.text


def test_http_page_matches_the_live_line(tmp_path, monkeypatch):
    engine = open_engine(tmp_path, monkeypatch)
    seeded = seed_screenshot(engine)
    live_chars = seeded["live_chars"]
    from starlette.testclient import TestClient

    from rogmem.app import build_app

    client = TestClient(build_app())
    response = client.get("/admin/settings/queue", params={"span": "24h", "tab": "queued", "page": "2"})
    assert response.status_code == 200
    html = response.text
    clock = datetime.now(timezone.utc)
    expected = view("24h", "queued", 2, clock)
    tasks, chars = _figure(html, "排队中")
    done_tasks, done_chars = _figure(html, "已完成")
    problems = []
    if tasks != f"{LIVE:,} 项":
        problems.append(f"概况排队显示 {tasks}，活队列是 {LIVE} 项")
    if chars != f"{live_chars:,} 字":
        problems.append(f"概况排队字数显示 {chars}，库内合计 {live_chars}")
    if done_tasks != f"{expected['summary']['done_tasks']} 项" or done_chars != f"{expected['summary']['done_chars']} 字":
        problems.append(f"已完成显示 {done_tasks} / {done_chars}")
    tab = _tab_count(html, "queued")
    if tab != f"{LIVE:,}":
        problems.append(f"tab 排队显示 {tab}")
    if "data-total='298'" not in html and f"data-total='{LIVE}'" not in html:
        problems.append("列表总数没有跟着活队列")
    if "data-page='2'" not in html:
        problems.append("第 2 页没有保住")
    body = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    if body.count("<tr>") != PAGE_SIZE:
        problems.append(f"第 2 页行数 {body.count('<tr>')}，页大小 {PAGE_SIZE}")
    if "排队、进行中和未跑完是此刻的整条队列" not in html:
        problems.append("明细说明丢了")
    week = client.get("/admin/settings/queue", params={"span": "7d", "tab": "all", "page": "abc"})
    week_expected = view("7d", "all", 1, clock)
    week_tasks, _week_chars = _figure(week.text, "排队中")
    week_done, _week_done_chars = _figure(week.text, "已完成")
    if week_tasks != f"{LIVE:,} 项":
        problems.append(f"切到 7 天后概况排队变成 {week_tasks}")
    if week_done != f"{week_expected['summary']['done_tasks']} 项":
        problems.append(f"7 天已完成应是 {week_expected['summary']['done_tasks']} 项，实际 {week_done}")
    if "data-page='1'" not in week.text:
        problems.append("非法页码没有回到第 1 页")
    assert not problems, "\n".join(problems)


def test_state_moves_are_separate_snapshots(tmp_path, monkeypatch):
    engine = open_engine(tmp_path, monkeypatch)
    recent = _at(timedelta(minutes=10))
    old = _at(timedelta(days=5))
    _insert(
        engine,
        [
            _row("old", state="queued", when=old, body="旧"),
            _row("fresh", state="queued", when=recent, body="新"),
        ],
    )
    first = view("24h", "queued", 1, NOW)
    assert first["summary"]["queued_tasks"] == first["counts"]["queued"] == first["total"] == 2

    con = engine.db()
    con.execute("UPDATE queue SET state='leased', started_at=? WHERE source_id='fresh'", (recent,))
    con.commit()
    second = view("24h", "all", 1, NOW)
    assert first["summary"]["queued_tasks"] == 2
    assert second["summary"]["queued_tasks"] == second["counts"]["queued"] == 1
    assert second["summary"]["running"] == second["counts"]["leased"] == 1
    assert {row["source_id"] for row in second["rows"] if row["state"] == "queued"} == {"old"}

    con.execute(
        "INSERT INTO queue(source_id,lane,state,enqueued_at) VALUES('fresh','p1','queued',?)",
        (recent,),
    )
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,pipeline)
           VALUES('born','file',?,'born','生','已向量化待蒸馏','p1',1,'解析:ok~向量:ok')""",
        (recent,),
    )
    con.execute(
        "INSERT INTO queue(source_id,lane,state,enqueued_at) VALUES('born','p1','queued',?)",
        (recent,),
    )
    con.commit()
    third = view("24h", "queued", 1, NOW)
    assert third["summary"]["queued_tasks"] == 3
    assert {row["source_id"] for row in third["rows"]} == {"old", "fresh", "born"}

    con.execute(
        "UPDATE queue SET state='done', finished_at=? WHERE source_id='old'",
        (old,),
    )
    con.commit()
    fourth = view("24h", "all", 1, NOW)
    assert fourth["summary"]["queued_tasks"] == 2
    assert fourth["summary"]["done_tasks"] == 0
    assert "old" not in {row["source_id"] for row in fourth["rows"]}
    week = view("7d", "done", 1, NOW)
    assert week["summary"]["done_tasks"] == 1
    assert week["summary"]["queued_tasks"] == 2


def test_concurrent_reads_do_not_mix_a_snapshot(tmp_path, monkeypatch):
    engine = open_engine(tmp_path, monkeypatch)
    recent = _at(timedelta(minutes=15))
    old = _at(timedelta(days=12))
    _insert(engine, [_row("old", state="queued", when=old, body="稳")] + [
        _row(f"hold-{i}", state="queued", when=recent, body="稳") for i in range(8)
    ])
    flipped = int(engine.db().execute("SELECT id FROM queue WHERE source_id='hold-0'").fetchone()["id"])
    seen: list[str] = []
    problems: list[str] = []
    stop = threading.Event()

    def flip() -> None:
        con = engine.db()
        state = "leased"
        while not stop.is_set():
            con.execute("UPDATE queue SET state=? WHERE id=?", (state, flipped))
            con.commit()
            state = "queued" if state == "leased" else "leased"

    def read() -> None:
        local = engine.db()
        del local
        # A read is now a bounded query, so a fixed 40 iterations can finish
        # before the writer flips. Keep going until both states have been seen.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not {"queued", "leased"} <= set(seen):
            board = view("24h", "all", 1, NOW)
            rows = board["rows"]
            queued_ids = [int(row["qid"]) for row in rows if row["state"] == "queued"]
            leased_ids = [int(row["qid"]) for row in rows if row["state"] == "leased"]
            if board["summary"]["queued_tasks"] != board["counts"]["queued"] or board["counts"]["queued"] != len(queued_ids):
                problems.append(
                    f"排队概况 {board['summary']['queued_tasks']} tab {board['counts']['queued']} 列表 {len(queued_ids)}"
                )
            if board["summary"]["running"] != board["counts"]["leased"] or board["counts"]["leased"] != len(leased_ids):
                problems.append("进行中和列表不一致")
            if set(queued_ids) & set(leased_ids):
                problems.append(f"同一行同时在排队和进行中 {set(queued_ids) & set(leased_ids)}")
            if "old" not in {row["source_id"] for row in rows if row["state"] == "queued"}:
                problems.append("窗外的排队行从这一次快照里消失了")
            if flipped in queued_ids:
                seen.append("queued")
            elif flipped in leased_ids:
                seen.append("leased")
            else:
                problems.append("被迁移的行从快照里丢了")

    writer = threading.Thread(target=flip)
    reader = threading.Thread(target=read)
    writer.start()
    reader.start()
    reader.join()
    stop.set()
    writer.join()
    assert not problems, problems[:5]
    assert "queued" in seen and "leased" in seen


def test_finished_clocks_are_not_dropped_by_the_sql_bound(tmp_path, monkeypatch):
    """字符串上界不是分类的超集：+08:00、同一瞬间的小数秒、小写 t 都在窗口里。"""

    engine = open_engine(tmp_path, monkeypatch)
    shanghai = timezone(timedelta(hours=8))
    one_hour_before = (NOW - timedelta(hours=1)).astimezone(shanghai).isoformat()
    same_instant = NOW.isoformat().replace("+00:00", ".000000+00:00")
    lower_t = (NOW - timedelta(minutes=30)).isoformat().replace("T", "t")
    window_start = (NOW - timedelta(hours=24)).astimezone(shanghai).isoformat()
    just_after = (NOW + timedelta(seconds=1)).isoformat()
    just_before = (NOW - timedelta(hours=24, seconds=1)).isoformat()
    ancient = _at(timedelta(days=40))
    _insert(
        engine,
        [
            _row("offset", state="done", when=one_hour_before, body="甲", finished_at=one_hour_before),
            _row("fraction", state="done", when=same_instant, body="乙", finished_at=same_instant),
            _row("lowert", state="done", when=lower_t, body="丙", finished_at=lower_t),
            _row("edge", state="done", when=window_start, body="丁", finished_at=window_start),
            _row("after", state="done", when=just_after, body="戊", finished_at=just_after),
            _row("before", state="done", when=just_before, body="己", finished_at=just_before),
            _row("ancient", state="done", when=ancient, body="庚", finished_at=ancient),
        ],
    )
    board = view("24h", "done", 1, NOW)
    assert {row["source_id"] for row in board["rows"]} == {"offset", "fraction", "lowert", "edge"}
    assert board["summary"]["done_tasks"] == board["counts"]["done"] == 4
    assert board["summary"]["done_chars"] == 4
    assert sum(point["tasks"] for point in board["series"]) == 4
    assert board["total"] == 4


def test_undated_done_stays_a_remainder(tmp_path, monkeypatch):
    """没有完成时间的已完成计入总数，不编进走势的某一格。"""

    engine = open_engine(tmp_path, monkeypatch)
    dated = _at(timedelta(hours=2))
    _insert(
        engine,
        [
            _row("dated", state="done", when=dated, body="甲乙丙"),
            _row("blank", state="done", when=dated, body="丁戊", finished_at=""),
            _row("garbage", state="done", when=dated, body="己", finished_at="没有时钟"),
        ],
    )
    board = view("all", "done", 1, NOW)
    placed = sum(point["tasks"] for point in board["series"])
    placed_chars = sum(point["chars"] for point in board["series"])
    assert board["summary"]["done_tasks"] == 3
    assert board["summary"]["done_chars"] == 6
    assert placed == 1 and placed_chars == 3
    assert board["summary"]["untimed_tasks"] == 2
    assert board["summary"]["untimed_chars"] == 3
    assert placed + board["summary"]["untimed_tasks"] == board["summary"]["done_tasks"]
    assert placed_chars + board["summary"]["untimed_chars"] == board["summary"]["done_chars"]
    assert all(point["at"] for point in board["series"])
    html = queue_page(board)
    assert "未标时间 2 项" in html
    assert "没有时钟" not in html
    hour = view("24h", "done", 1, NOW)
    assert hour["summary"]["done_tasks"] == 1
    assert hour["summary"]["untimed_tasks"] == 0
    assert sum(point["tasks"] for point in hour["series"]) == 1
    assert "未标时间" not in queue_page(hour)


def _figure(html: str, label: str) -> tuple[str, str]:
    match = re.search(
        rf"<p class=\"k\">{label}</p><p class=\"money\">([^<]+)</p><p class=\"ov-split\">([^<]+)</p>",
        html,
    )
    assert match, label
    return match.group(1), match.group(2)


def _tab_count(html: str, tab: str) -> str:
    match = re.search(rf"tab={tab}(?:&amp;|&)page=1'[^>]*>(?:全部|已完成|排队中|进行中) ([0-9,]+)", html)
    assert match, tab
    return match.group(1)
