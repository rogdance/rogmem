"""The pending-vector poll must not full-scan every chunk.

`ensure_pending_vectors` runs inside the distill poll loop, several times a second, while
holding the engine lock. Its correlated EXISTS over an unindexed `chunks` made each round
walk every source and re-scan every chunk: one round took 80s+ in production, and every
admin page that needed the lock queued behind it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[1] / "rogmem" / "schema.sql"

PENDING = (
    "select id, lane from sources where status not in ('no_body','parse_fail') "
    "and ifnull(wait_until,'') not like 'hold:%' and (status='parsed' or exists ("
    "select 1 from chunks where chunks.source_id=sources.id and role!='parent' "
    "and vec_json is null and trim(ifnull(text,''))!='')) order by ingested_at limit 64"
)


def _db(tmp_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(tmp_path / "rogmem.sqlite")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.executemany(
        "INSERT INTO sources(id, ingested_at, kind, content_hash, body, status, lane)"
        " VALUES(?,?,?,?,?,?,?)",
        [
            (
                f"s{i}",
                f"2026-01-01T00:{i // 60 % 60:02d}:{i % 60:02d}",
                "user",
                f"h{i}",
                "正文内容",
                "distilled",
                "slow",
            )
            for i in range(2000)
        ],
    )
    rows = []
    for i in range(2000):
        rows.append((i * 2, f"s{i}", 0, "整篇", "parent"))
        rows.append((i * 2 + 1, f"s{i}", 1, "正文内容", "child"))
    con.executemany("INSERT INTO chunks(id, source_id, seq, text, role) VALUES(?,?,?,?,?)", rows)
    con.commit()
    return con


def test_the_pending_vector_poll_does_not_scan_every_chunk(tmp_path):
    con = _db(tmp_path)
    steps = [row[3] for row in con.execute("explain query plan " + PENDING)]
    assert "SCAN chunks" not in steps, steps
    assert any(step.startswith("SEARCH chunks USING") for step in steps), steps


def test_the_schema_declares_the_chunks_indexes(tmp_path):
    con = _db(tmp_path)
    names = {row[0] for row in con.execute("select name from sqlite_master where type='index'")}
    assert "chunks_source" in names, names
    assert "chunks_missing_vec" in names, names


def test_an_idle_hub_does_not_pay_for_the_full_walk(tmp_path):
    from rogmem.engine import ST_PARSED, waiting_vector_work

    con = _db(tmp_path)
    con.execute("UPDATE chunks SET vec_json='[0.1,0.2]'")
    con.execute("UPDATE sources SET status='distilled'")
    con.commit()
    assert waiting_vector_work(con) is False

    con.execute("UPDATE chunks SET vec_json=NULL WHERE id=1")
    con.commit()
    assert waiting_vector_work(con) is True

    con.execute("UPDATE chunks SET vec_json='[0.1,0.2]'")
    con.execute("UPDATE sources SET status=? WHERE id='s0'", (ST_PARSED,))
    con.commit()
    assert waiting_vector_work(con) is True
