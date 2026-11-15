"""缺了的卡片在状态更新时按正文恢复字数。已有卡片的状态更新不动字数。"""

from __future__ import annotations

from pathlib import Path


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


def _source(engine, sid: str, body: str) -> None:
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,filename)
           VALUES(?,?,?,?,?,?,?,1,?)""",
        (sid, "file", "2026-11-12T00:00:00+00:00", sid, body, "已向量化待蒸馏", "p1", "note.md"),
    )
    engine.db().execute(
        "INSERT INTO queue(source_id,lane,state,enqueued_at) VALUES(?,?, 'queued', ?)",
        (sid, "p1", "2026-11-12T00:00:00+00:00"),
    )
    engine.db().commit()


_OLD_STATUS_TRIGGER = """
CREATE TRIGGER source_cards_status
AFTER UPDATE OF status, pipeline, filename, kind, ingested_at ON sources
BEGIN
  INSERT INTO source_cards(id, kind, filename, ingested_at, pipeline, status, body_chars, body_preview)
  VALUES (
    NEW.id,
    IFNULL(NEW.kind, ''),
    IFNULL(NEW.filename, ''),
    IFNULL(NEW.ingested_at, ''),
    IFNULL(NEW.pipeline, ''),
    IFNULL(NEW.status, ''),
    0,
    ''
  )
  ON CONFLICT(id) DO UPDATE SET
    kind=excluded.kind,
    filename=excluded.filename,
    ingested_at=excluded.ingested_at,
    pipeline=excluded.pipeline,
    status=excluded.status;
END
"""


def test_missing_card_recovers_chars_on_status_update(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    sid = "card-missing"
    body = "正文正好六字"
    _source(engine, sid, body)
    assert engine.db().execute("SELECT body_chars FROM source_cards WHERE id=?", (sid,)).fetchone()["body_chars"] == 6
    engine.db().execute("DELETE FROM source_cards WHERE id=?", (sid,))
    engine.db().commit()
    engine.db().execute("UPDATE sources SET status=? WHERE id=?", ("蒸馏中", sid))
    engine.db().commit()
    card = engine.db().execute(
        "SELECT body_chars, body_preview FROM source_cards WHERE id=?",
        (sid,),
    ).fetchone()
    assert card["body_chars"] == 6
    assert "正文" in card["body_preview"]
    from rogmem.queue_board import _ROW_SQL, view
    from datetime import datetime, timezone

    assert "body_chars" in _ROW_SQL and "sources.body" not in _ROW_SQL
    board = view("24h", "queued", 1, datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc))
    assert board["summary"]["queued_chars"] == 6
    assert board["summary"]["queued_tasks"] == 1


def test_status_update_of_an_existing_card_keeps_its_chars(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    sid = "card-kept"
    _source(engine, sid, "正文正好六字")
    engine.db().execute(
        "UPDATE source_cards SET body_chars=?, body_preview=? WHERE id=?",
        (99, "KEEP", sid),
    )
    engine.db().commit()
    con = engine.db()
    con.execute("BEGIN")
    con.execute("UPDATE sources SET status=? WHERE id=?", ("蒸馏中", sid))
    card = con.execute("SELECT body_chars, body_preview, status FROM source_cards WHERE id=?", (sid,)).fetchone()
    assert card["body_chars"] == 99
    assert card["body_preview"] == "KEEP"
    assert card["status"] == "蒸馏中"
    con.rollback()
    restored = con.execute("SELECT status FROM sources WHERE id=?", (sid,)).fetchone()
    kept = con.execute("SELECT body_chars FROM source_cards WHERE id=?", (sid,)).fetchone()
    assert restored["status"] == "已向量化待蒸馏"
    assert kept["body_chars"] == 99


def test_reconnect_replaces_a_zero_char_status_trigger(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    sid = "card-upgrade"
    _source(engine, sid, "正文正好六字")
    con = engine.db()
    con.execute("DROP TRIGGER IF EXISTS source_cards_status")
    con.executescript(_OLD_STATUS_TRIGGER)
    con.execute("DELETE FROM source_cards WHERE id=?", (sid,))
    con.execute("UPDATE sources SET status=? WHERE id=?", ("蒸馏中", sid))
    con.commit()
    assert con.execute("SELECT body_chars FROM source_cards WHERE id=?", (sid,)).fetchone()["body_chars"] == 0
    engine.connect(tmp_path, tmp_path / "scan")
    engine.db().execute("DELETE FROM source_cards WHERE id=?", (sid,))
    engine.db().execute("UPDATE sources SET status=? WHERE id=?", ("已向量化待蒸馏", sid))
    engine.db().commit()
    assert engine.db().execute("SELECT body_chars FROM source_cards WHERE id=?", (sid,)).fetchone()["body_chars"] == 6
