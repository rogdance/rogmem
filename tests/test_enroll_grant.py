"""One enrollment grant mints a distinct key per program and does not rotate an existing one."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from rogmem import crypto


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _pair() -> tuple[bytes, str]:
    secret, public = crypto.new_keypair()
    return secret, crypto.b64encode(public)


def _token(secret: bytes, wrapped: str) -> str:
    return crypto.open_seal(secret, crypto.b64decode(wrapped)).decode()


def test_one_grant_gives_each_program_its_own_token(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    minted = engine.create_enroll_grant()
    cursor_secret, cursor_pub = _pair()
    hermes_secret, hermes_pub = _pair()
    first = engine.redeem_enroll_grant(minted["secret"], "dev-a", "AAAA", "Cursor", cursor_pub)
    second = engine.redeem_enroll_grant(minted["secret"], "dev-a", "AAAA", "Hermes", hermes_pub)
    assert first["ok"] is True and first["already"] is False and first["status"] == "启用"
    assert second["ok"] is True and second["already"] is False
    cursor_token = _token(cursor_secret, first["wrapped"])
    hermes_token = _token(hermes_secret, second["wrapped"])
    assert cursor_token != hermes_token
    assert engine.agent_by_token(cursor_token, "dev-a")["program"] == "Cursor"
    assert engine.agent_by_token(hermes_token, "dev-a")["program"] == "Hermes"
    again = engine.redeem_enroll_grant(minted["secret"], "dev-a", "AAAA", "Cursor", cursor_pub)
    assert again["wrapped"] == first["wrapped"]
    assert _token(cursor_secret, again["wrapped"]) == cursor_token
    assert engine.db().execute("SELECT key_hash FROM agents WHERE id=?", (first["id"],)).fetchone()["key_hash"] == (
        engine.db().execute("SELECT key_hash FROM agents WHERE program='Cursor'").fetchone()["key_hash"]
    )
    other = engine.redeem_enroll_grant(minted["secret"], "dev-b", "BBBB", "Codex", crypto.b64encode(crypto.new_keypair()[1]))
    assert other["ok"] is False and other["error"] == "不是这台设备"


def test_a_new_grant_does_not_replace_an_enabled_host(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    first = engine.create_enroll_grant()
    secret, public = _pair()
    opened = engine.redeem_enroll_grant(first["secret"], "dev-a", "AAAA", "Cursor", public)
    digest = engine.db().execute("SELECT key_hash FROM agents WHERE id=?", (opened["id"],)).fetchone()["key_hash"]
    second = engine.create_enroll_grant()
    kept = engine.redeem_enroll_grant(second["secret"], "dev-a", "AAAA", "Cursor", public)
    fresh_secret, fresh_public = _pair()
    added = engine.redeem_enroll_grant(second["secret"], "dev-a", "AAAA", "Hermes", fresh_public)
    assert kept["already"] is True and kept["wrapped"] == ""
    assert engine.db().execute("SELECT key_hash FROM agents WHERE id=?", (opened["id"],)).fetchone()["key_hash"] == digest
    assert added["status"] == "启用" and _token(fresh_secret, added["wrapped"]) != _token(secret, opened["wrapped"])


def test_an_enabled_host_rekeys_when_the_client_presents_a_new_key(tmp_path, monkeypatch):
    """A host that lost its key re-enrols onto the same row instead of being stranded.

    The row keeps its identity, the old key dies and the new one works. Without
    this, a client whose config no longer holds a token stays refused forever.
    """
    engine = _open(tmp_path, monkeypatch)
    first = engine.create_enroll_grant()
    old_secret, old_public = _pair()
    opened = engine.redeem_enroll_grant(first["secret"], "dev-a", "AAAA", "Cursor", old_public)
    old_token = _token(old_secret, opened["wrapped"])
    assert engine.agent_by_token(old_token, "dev-a") is not None

    new_secret, new_public = _pair()
    fresh = engine.create_enroll_grant()
    rekeyed = engine.redeem_enroll_grant(fresh["secret"], "dev-a", "AAAA", "Cursor", new_public)
    assert rekeyed["ok"] is True and rekeyed["already"] is False and rekeyed["status"] == "启用"
    assert rekeyed["id"] == opened["id"]
    new_token = _token(new_secret, rekeyed["wrapped"])
    assert engine.agent_by_token(new_token, "dev-a")["id"] == opened["id"]
    assert engine.agent_by_token(old_token, "dev-a") is None
    assert engine.db().execute(
        "SELECT COUNT(*) AS n FROM agents WHERE device_id='dev-a' AND program='Cursor'"
    ).fetchone()["n"] == 1


def test_stopped_and_rejected_hosts_stay_put(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO agents(id,device_id,device_name,program,display_name,status,pubkey,key_hash,created_at)
           VALUES('stop','dev-a','AAAA','Cursor','AAAA · Cursor','停用','pub','hash',?)""",
        (engine.utcnow(),),
    )
    engine.db().execute(
        """INSERT INTO agents(id,device_id,device_name,program,display_name,status,pubkey,key_hash,created_at)
           VALUES('no','dev-a','AAAA','Hermes','AAAA · Hermes','已拒绝','pub2',NULL,?)""",
        (engine.utcnow(),),
    )
    engine.db().commit()
    minted = engine.create_enroll_grant()
    stopped = engine.redeem_enroll_grant(minted["secret"], "dev-a", "AAAA", "Cursor", crypto.b64encode(crypto.new_keypair()[1]))
    rejected = engine.redeem_enroll_grant(minted["secret"], "dev-a", "AAAA", "Hermes", crypto.b64encode(crypto.new_keypair()[1]))
    assert stopped["error"] == "已停用"
    assert rejected["error"] == "已拒绝"
    assert engine.db().execute("SELECT status FROM agents WHERE id='stop'").fetchone()["status"] == "停用"
    assert engine.db().execute("SELECT status FROM agents WHERE id='no'").fetchone()["status"] == "已拒绝"


def test_pending_host_is_finished_by_the_grant(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    secret, public = _pair()
    applied = engine.enroll_apply("dev-a", "AAAA", "Codex", public)
    assert applied["status"] == "待放行"
    minted = engine.create_enroll_grant()
    done = engine.redeem_enroll_grant(minted["secret"], "dev-a", "AAAA", "Codex", public)
    assert done["ok"] is True and done["already"] is False
    assert engine.agent_by_token(_token(secret, done["wrapped"]), "dev-a")["id"] == applied["id"]
    assert engine.db().execute("SELECT COUNT(*) AS n FROM agents WHERE device_id='dev-a' AND program='Codex'").fetchone()["n"] == 1


def test_expired_or_unknown_grant_is_refused(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    minted = engine.create_enroll_grant()
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    engine.db().execute("UPDATE enroll_grants SET expires_at=? WHERE id=?", (past, minted["id"]))
    engine.db().commit()
    _secret, public = _pair()
    expired = engine.redeem_enroll_grant(minted["secret"], "dev-a", "AAAA", "Cursor", public)
    unknown = engine.redeem_enroll_grant("rg_nope", "dev-a", "AAAA", "Cursor", public)
    assert expired["error"] == "凭证过期"
    assert unknown["error"] == "凭证无效"


def test_a_fresh_start_holds_no_write_lock(tmp_path, monkeypatch):
    """Startup must end with a clean connection, not a half-finished write.

    connect() writes after its last commit (instant columns, the liveness
    index). Left open, that connection keeps the single writer slot for the
    whole process and every later writer is refused with "database is locked".
    """
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    assert engine.db().in_transaction is False
    minted = engine.create_enroll_grant()
    assert minted["ok"] is True
    assert engine.db().in_transaction is False


def test_a_write_refused_by_the_lock_settles_itself(tmp_path, monkeypatch):
    """A statement that cannot take the lock still ends inside a transaction.

    Left there, that connection keeps a read snapshot and refuses the next
    writer all the way down the line. It has to settle itself.
    """
    engine = _open(tmp_path, monkeypatch)
    con = engine.db()
    con.execute("PRAGMA busy_timeout=1")

    holder = sqlite3.connect(tmp_path / "rogmem.sqlite")
    holder.isolation_level = None
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute(
                """INSERT INTO enroll_grants(id, secret_hash, created_at, expires_at, device_id, device_name)
                   VALUES('x','h','a','b','','')"""
            )
        assert con.in_transaction is False
    finally:
        holder.rollback()
        holder.close()
