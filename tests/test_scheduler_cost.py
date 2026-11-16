"""One scheduler round must not cost more as the backlog grows.

`lease_next` runs inside the engine lock, several times a second, from three distill
workers. It used to load every waiting row — with its body — and rewrite every due
row. In production that backlog is 36k rows: once the page cache went cold a single
round took minutes, and the whole console queued behind the lock. A round only ever
hands out one unit, so it must look at a bounded window, not at the whole queue.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rogmem import engine

SCHEMA = Path(__file__).resolve().parents[1] / "rogmem" / "schema.sql"
FUTURE = "2026-06-01T00:00:00+00:00"


class _Recorder:
    """A connection stand-in that keeps the SQL the caller ran."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con
        self.sql: list[str] = []

    def execute(self, sql, parameters=()):
        self.sql.append(sql)
        return self.con.execute(sql, parameters)


def _db(tmp_path: Path, *, waiting: int = 4000) -> sqlite3.Connection:
    con = sqlite3.connect(tmp_path / "rogmem.sqlite")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.executemany(
        "INSERT INTO sources(id, ingested_at, kind, content_hash, body, status, lane)"
        " VALUES(?,?,?,?,?,?,?)",
        [
            (f"s{i}", f"2026-01-01T00:{i // 60 % 60:02d}:{i % 60:02d}", "user", f"h{i}", "正文内容", "parsed", "p3")
            for i in range(waiting + 4)
        ],
    )
    rows = [(i, f"s{i}", "p3", "queued", None) for i in range(waiting)]
    # 头尾各要看得见；中段和还没到点的都不必每轮都装进来。
    rows.append((waiting, f"s{waiting}", "p0", "queued", None))
    rows.append((waiting + 1, f"s{waiting + 1}", "p3", "queued", "2099-01-01T00:00:00+00:00"))
    con.executemany("INSERT INTO queue(id, source_id, lane, state, next_at) VALUES(?,?,?,?,?)", rows)
    con.commit()
    return con


def test_a_round_is_bounded_by_the_window_not_by_the_backlog(tmp_path):
    con = _db(tmp_path, waiting=4000)
    recorder = _Recorder(con)
    rows = engine._queued_work(recorder, FUTURE)

    ceiling = engine._SCHEDULE_OLDEST + engine._SCHEDULE_NEWEST
    assert len(rows) == ceiling, len(rows)
    assert len(rows) < 4000


def test_the_window_keeps_both_the_oldest_and_the_newest(tmp_path):
    con = _db(tmp_path, waiting=4000)
    picked = {int(row["qid"]) for row in engine._queued_work(con, FUTURE)}

    assert 0 in picked, "等最久的一条要看得见"
    assert 4000 in picked, "刚落地的一条要看得见"
    assert 2000 not in picked, "窗口之外的老堆积不必每轮都装进来"
    assert 4001 not in picked, "还没到点的不能被挑走"


def test_the_window_walks_an_index(tmp_path):
    con = _db(tmp_path, waiting=4000)
    recorder = _Recorder(con)
    engine._queued_work(recorder, FUTURE)
    steps = [row[3] for row in con.execute("explain query plan " + recorder.sql[0], (FUTURE, 1024, FUTURE, 256))]

    assert not any(step.startswith("SCAN queue") for step in steps), steps
    assert any("queue_waiting" in step for step in steps), steps


def test_a_due_failure_is_reset_in_batches(tmp_path):
    con = _db(tmp_path, waiting=4000)
    con.executemany(
        "INSERT INTO queue(id, source_id, lane, state, next_at) VALUES(?,?,?,?,?)",
        [(10_000 + i, "s0", "p3", "failed", "2020-01-01T00:00:00+00:00") for i in range(3000)],
    )
    con.commit()

    recorder = _Recorder(con)
    engine._reclaim_expired(recorder, FUTURE)

    revivals = [sql for sql in recorder.sql if "state='failed'" in sql]
    assert len(revivals) == 1, recorder.sql
    steps = [row[3] for row in con.execute("explain query plan " + revivals[0], (FUTURE, engine._SCHEDULE_RECLAIM))]
    assert not any(step.startswith("SCAN queue") for step in steps), steps

    moved = con.execute("SELECT COUNT(*) FROM queue WHERE state='queued' AND id>=10000").fetchone()[0]
    assert moved == engine._SCHEDULE_RECLAIM, moved


def test_the_schema_declares_the_queue_indexes(tmp_path):
    con = _db(tmp_path)
    names = {row[0] for row in con.execute("select name from sqlite_master where type='index'")}
    for needed in ("queue_waiting", "queue_leased", "queue_due"):
        assert needed in names, names
