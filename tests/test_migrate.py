"""SQLite 版本化迁移：PRAGMA user_version + 可追加步骤。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rogmem import migrate


def test_schema_version_constant_is_positive():
    assert migrate.SCHEMA_VERSION >= 1


def test_fresh_db_reaches_schema_version(tmp_path):
    path = tmp_path / "rogmem.sqlite"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    schema = (Path(__file__).resolve().parents[1] / "rogmem" / "schema.sql").read_text(
        encoding="utf-8"
    )
    con.executescript(schema)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 0

    applied = migrate.apply(con)
    assert applied == migrate.SCHEMA_VERSION
    assert con.execute("PRAGMA user_version").fetchone()[0] == migrate.SCHEMA_VERSION
    # defense audit table from a versioned step
    tables = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "defense_hits" in tables
    con.close()


def test_apply_is_idempotent(tmp_path):
    path = tmp_path / "rogmem.sqlite"
    con = sqlite3.connect(path)
    schema = (Path(__file__).resolve().parents[1] / "rogmem" / "schema.sql").read_text(
        encoding="utf-8"
    )
    con.executescript(schema)
    first = migrate.apply(con)
    second = migrate.apply(con)
    assert first == migrate.SCHEMA_VERSION
    assert second == 0  # nothing new
    assert con.execute("PRAGMA user_version").fetchone()[0] == migrate.SCHEMA_VERSION
    con.close()


def test_connect_stamps_user_version(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    ver = engine.db().execute("PRAGMA user_version").fetchone()[0]
    assert ver == migrate.SCHEMA_VERSION
    row = engine.db().execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='defense_hits'"
    ).fetchone()
    assert row is not None
