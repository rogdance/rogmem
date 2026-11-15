"""A retry is a new attempt. The attempt still running does not get to finish the old one.

HTTP 429 is the provider not taking the call. Waiting for another database writer
is part of the write, not an Internal Server Error.
"""

import io
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

from starlette.testclient import TestClient


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _hold_write(path, started):
    """Hold the write lock longer than SQLite's 5s busy timeout, then let go."""
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("BEGIN IMMEDIATE")
    started.set()
    time.sleep(6)
    con.rollback()
    con.close()


def test_a_failed_read_tries_the_file_again(tmp_path, monkeypatch):
    """The bytes are still on disk. Retry reads them again instead of keeping the old sentence."""
    from openpyxl import Workbook

    engine = _open(tmp_path, monkeypatch)
    path = tmp_path / "scan" / "名录.xlsx"
    path.parent.mkdir(exist_ok=True)
    book = Workbook()
    book.active["A1"] = "捷安特"
    book.save(path)
    sid = "reread01"
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,filename,pipeline,last_error)
           VALUES(?,?,?,?,?,?,?,1,?,?,?)""",
        (
            sid,
            "file",
            "2026-11-13T00:49:20+00:00",
            "h",
            "",
            "解析失败",
            "p3",
            "名录.xlsx",
            "解析:fail",
            "没有抽出正文",
        ),
    )
    engine.db().commit()
    assert engine.retry_source(sid)["ok"] is True
    row = engine.db().execute("SELECT body, status, last_error FROM sources WHERE id=?", (sid,)).fetchone()
    assert "捷安特" in row["body"]
    assert row["status"] != "解析失败"
    assert not row["last_error"]


def test_a_document_over_the_limit_is_refused(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "DOCUMENT_MAX_BYTES", 4)
    refused = engine.import_bytes("note.txt", "太长了".encode("utf-8"))
    assert refused["ok"] is False
    assert refused["error"] == "文件太大"
    assert engine.db().execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"] == 0


def test_retry_waits_out_another_writers_lock(tmp_path, monkeypatch):
    """A click must not become Internal Server Error while another writer still has the database."""
    engine = _open(tmp_path, monkeypatch)
    sid = "a8eec05fda55bba6"
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,filename,pipeline,last_error)
           VALUES(?,?,?,?,?,?,?,1,?,?,?)""",
        (
            sid,
            "file",
            "2026-11-10T04:28:11+00:00",
            "h",
            "简历正文",
            "蒸馏失败",
            "p3",
            "中文简历2026.docx",
            "并页|解析:ok,草稿:ok,并页:fail~向量:ok",
            'HTTP 429 {"type":"rate_limit_error","message":"用量上限"}',
        ),
    )
    engine.db().execute(
        "INSERT INTO queue(source_id,lane,state,attempts,last_error) VALUES(?,?, 'failed', 5, ?)",
        (sid, "p3", "HTTP 429"),
    )
    engine.db().commit()
    started = threading.Event()
    holder = threading.Thread(target=_hold_write, args=(tmp_path / "rogmem.sqlite", started), daemon=True)
    holder.start()
    assert started.wait(2)
    from rogmem.app import build_app

    response = TestClient(build_app(), raise_server_exceptions=False).post(
        f"/admin/api/sources/{sid}/retry",
        json={},
    )
    holder.join(timeout=2)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert "Internal Server Error" not in response.text


def _http_error(code: int, body: bytes):
    def respond(req, timeout=60):
        raise urllib.error.HTTPError(getattr(req, "full_url", "https://example.invalid"), code, "error", {}, io.BytesIO(body))

    return respond


def test_provider_backpressure_never_retires_the_file(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "llm_target", lambda role="distill": ("https://example.invalid/v1", "sk-test", "stub"))
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _http_error(429, b'{"type":"rate_limit_error","message":"quota"}'),
    )
    path = tmp_path / "scan" / "中文简历2026.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("甲在南港工作多年。", encoding="utf-8")
    opened = engine.ingest_file(path, device_name="极空间")
    for _ in range(6):
        engine.db().execute(
            "UPDATE queue SET state='queued', next_at=NULL, leased_until=NULL WHERE source_id=?",
            (opened["id"],),
        )
        engine.db().commit()
        assert engine.pump_one() == opened["id"]
    queued = engine.db().execute(
        "SELECT state, last_error FROM queue WHERE source_id=?",
        (opened["id"],),
    ).fetchone()
    source = engine.db().execute("SELECT status, last_error FROM sources WHERE id=?", (opened["id"],)).fetchone()
    assert queued["state"] == "queued"
    assert source["status"] != "蒸馏失败"
    assert str(source["last_error"]).startswith("HTTP 429")


def test_an_inflight_attempt_cannot_overwrite_a_retry(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,body_md)
           VALUES('page-jia','人物','甲','active','{}','{}','旧说法。')"""
    )
    engine.db().commit()
    started = threading.Event()
    go = threading.Event()

    def merge(*_args, **_kwargs):
        started.set()
        assert go.wait(5)
        raise engine.ModelCallFailed("still the old call")

    monkeypatch.setattr(
        engine,
        "_produce_distill_payload",
        lambda *_args, **_kwargs: {"payload_kind": "markdown", "pages": [{"name": "甲", "type": "人物", "body": "甲在南港。"}]},
    )
    monkeypatch.setattr(engine, "_merge_existing_page", merge)
    path = tmp_path / "scan" / "进行中.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("甲在南港。", encoding="utf-8")
    opened = engine.ingest_file(path)
    worker = threading.Thread(target=engine.pump_one, daemon=True)
    worker.start()
    assert started.wait(5)
    assert engine.retry_source(opened["id"])["ok"] is True
    go.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    queued = engine.db().execute(
        "SELECT state, attempts, last_error, superseded FROM queue WHERE source_id=?",
        (opened["id"],),
    ).fetchone()
    source = engine.db().execute("SELECT last_error, pipeline FROM sources WHERE id=?", (opened["id"],)).fetchone()
    assert queued["state"] == "queued"
    assert queued["attempts"] == 0
    assert queued["last_error"] is None
    assert queued["superseded"] == 0
    assert source["last_error"] is None
    assert source["pipeline"] != "解析:ok"


def test_a_call_the_provider_did_not_finish_stays_on_the_line(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "llm_target", lambda role="distill": ("https://example.invalid/v1", "sk-test", "stub"))
    monkeypatch.setattr(urllib.request, "urlopen", _http_error(500, b'{"error":"upstream"}'))
    path = tmp_path / "scan" / "坏了.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("甲在南港工作多年。", encoding="utf-8")
    opened = engine.ingest_file(path)
    for _ in range(5):
        engine.db().execute(
            "UPDATE queue SET state='queued', next_at=NULL, leased_until=NULL WHERE source_id=?",
            (opened["id"],),
        )
        engine.db().commit()
        assert engine.pump_one() == opened["id"]
    queued = engine.db().execute("SELECT state, attempts FROM queue WHERE source_id=?", (opened["id"],)).fetchone()
    source = engine.db().execute("SELECT status FROM sources WHERE id=?", (opened["id"],)).fetchone()
    assert queued["state"] == "queued"
    assert queued["attempts"] == 5
    assert source["status"] != "蒸馏失败"


def test_a_verdict_stops_after_five_and_comes_back_after_the_backoff(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)

    def refuse(*_args, **_kwargs):
        raise RuntimeError("模型没有交出页面")

    monkeypatch.setattr(engine, "_distill", refuse)
    path = tmp_path / "scan" / "没交卷.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("甲在南港工作多年。", encoding="utf-8")
    opened = engine.ingest_file(path)
    for _ in range(5):
        engine.db().execute(
            "UPDATE queue SET state='queued', next_at=NULL, leased_until=NULL WHERE source_id=?",
            (opened["id"],),
        )
        engine.db().commit()
        assert engine.pump_one() == opened["id"]
    stopped = engine.db().execute("SELECT state, next_at FROM queue WHERE source_id=?", (opened["id"],)).fetchone()
    assert stopped["state"] == "failed"
    assert stopped["next_at"]
    assert engine.lease_next() is None
    engine.db().execute("UPDATE queue SET next_at=? WHERE source_id=?", ("2000-01-01T00:00:00+00:00", opened["id"]))
    engine.db().commit()
    again = engine.lease_next()
    assert again is not None and again["source_id"] == opened["id"]


def test_manual_retry_keeps_the_failed_stage(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    sid = "src-keep"
    stage = '{"phase":"draft","pages":[{"name":"甲","type":"人物","body":"甲在南港。"}]}'
    pipeline = "并页|解析:ok,草稿:ok,并页:fail~向量:ok"
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,pipeline,stage_json,last_error)
           VALUES(?,?,?,?,?,?,?,1,?,?,?)""",
        (
            sid,
            "file",
            "2026-11-10T04:28:11+00:00",
            "h",
            "甲在南港。",
            "蒸馏失败",
            "p3",
            pipeline,
            stage,
            "HTTP 429 {}",
        ),
    )
    engine.db().execute(
        "INSERT INTO queue(source_id,lane,state,attempts,last_error,leased_until) VALUES(?,?, 'failed', 5, ?, ?)",
        (sid, "p3", "HTTP 429 {}", "2026-11-11T00:00:00+00:00"),
    )
    engine.db().commit()
    assert engine.retry_source(sid)["ok"] is True
    row = engine.db().execute("SELECT pipeline, stage_json, status, last_error FROM sources WHERE id=?", (sid,)).fetchone()
    queued = engine.db().execute(
        "SELECT state, attempts, next_at, leased_until, last_error FROM queue WHERE source_id=?",
        (sid,),
    ).fetchone()
    assert row["stage_json"] == stage
    assert row["pipeline"] != "解析:ok"
    assert "并页" in (row["pipeline"] or "")
    assert "~向量:ok" in (row["pipeline"] or "")
    assert row["last_error"] is None
    assert queued["state"] == "queued"
    assert queued["attempts"] == 0
    assert queued["next_at"] is None
    assert queued["leased_until"] is None
    assert queued["last_error"] is None


def _distill_failed(engine, sid: str) -> None:
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,filename,pipeline,last_error)
           VALUES(?,?,?,?,?,?,?,1,?,?,?)""",
        (
            sid,
            "file",
            "2026-11-10T04:28:11+00:00",
            "h",
            "简历正文",
            "蒸馏失败",
            "p3",
            "中文简历2026.docx",
            "并页|解析:ok,草稿:ok,并页:fail~向量:ok",
            "HTTP 429",
        ),
    )
    engine.db().execute(
        "INSERT INTO queue(source_id,lane,state,attempts,last_error) VALUES(?,?, 'failed', 5, ?)",
        (sid, "p3", "HTTP 429"),
    )
    engine.db().commit()


def _commit_raises(engine, exc: BaseException):
    """The next commit on this thread's connection raises after the writes have run."""

    inner = engine.db()

    class _Boom:
        def commit(self):
            raise exc

        def __getattr__(self, name):
            return getattr(inner, name)

    engine._LOCAL.con = _Boom()
    return inner


def _restore_commit(engine, inner) -> None:
    engine._LOCAL.con = inner


def test_retry_commit_runtime_error_rolls_the_write_back(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    sid = "retry-runtime"
    _distill_failed(engine, sid)
    original = _commit_raises(engine, RuntimeError("commit blew up"))
    try:
        with pytest.raises(RuntimeError, match="commit blew up"):
            engine.retry_source(sid)
    finally:
        _restore_commit(engine, original)
    assert engine.db().in_transaction is False
    source = engine.db().execute("SELECT status FROM sources WHERE id=?", (sid,)).fetchone()
    queued = engine.db().execute("SELECT state FROM queue WHERE source_id=?", (sid,)).fetchone()
    assert source["status"] == "蒸馏失败"
    assert queued["state"] == "failed"
    other = sqlite3.connect(tmp_path / "rogmem.sqlite")
    try:
        other.execute("PRAGMA busy_timeout=300")
        other.execute("UPDATE queue SET last_error=? WHERE source_id=?", ("旁路", sid))
        other.commit()
    finally:
        other.close()
    assert engine.retry_source(sid)["ok"] is True
    assert engine.db().execute("SELECT state FROM queue WHERE source_id=?", (sid,)).fetchone()["state"] == "queued"


def test_retry_commit_integrity_error_rolls_the_write_back(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    sid = "retry-integrity"
    _distill_failed(engine, sid)
    original = _commit_raises(engine, sqlite3.IntegrityError("constraint failed"))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            engine.retry_source(sid)
    finally:
        _restore_commit(engine, original)
    assert engine.db().in_transaction is False
    assert engine.db().execute("SELECT state FROM queue WHERE source_id=?", (sid,)).fetchone()["state"] == "failed"
    assert engine.db().execute("SELECT status FROM sources WHERE id=?", (sid,)).fetchone()["status"] == "蒸馏失败"


def _bench_source(engine, sid: str) -> None:
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,filename)
           VALUES(?,?,?,?,?,?,?,1,?)""",
        (sid, "file", "2026-11-10T04:28:11+00:00", sid, "已提交正文", "蒸馏失败", "p3", "wiki-bench-a.md"),
    )
    engine.db().execute(
        "INSERT INTO queue(source_id,lane,state,attempts,last_error) VALUES(?,?, 'failed', 2, ?)",
        (sid, "p3", "停了"),
    )
    engine.db().commit()


def _dirty_connection(inner):
    class _Dirty:
        def commit(self):
            raise RuntimeError("commit blew up")

        def rollback(self):
            raise sqlite3.OperationalError("rollback failed")

        def close(self):
            inner.close()

        def __getattr__(self, name):
            return getattr(inner, name)

    return _Dirty()


def test_rebench_reopen_failure_keeps_the_original_error(tmp_path, monkeypatch):
    """主线程上，回滚失败后再重连失败，不能把原来的 RuntimeError 换成重连错误。"""

    engine = _open(tmp_path, monkeypatch)
    sid = "rebench-reopen"
    _bench_source(engine, sid)
    inner = engine.db()
    dirty = _dirty_connection(inner)
    engine._DB = dirty
    engine._LOCAL.con = dirty
    real_open = engine._open_connection

    def reopen_fails(path):
        raise sqlite3.OperationalError("reopen failed")

    monkeypatch.setattr(engine, "_open_connection", reopen_fails)
    with pytest.raises(RuntimeError, match="commit blew up") as caught:
        engine.rebench_source(sid)
    assert not isinstance(caught.value, sqlite3.OperationalError)
    assert getattr(engine._LOCAL, "con", None) is not dirty
    assert engine._DB is not dirty
    monkeypatch.setattr(engine, "_open_connection", real_open)
    fresh = engine.db()
    assert fresh is not dirty
    assert fresh is not inner
    assert fresh.execute("SELECT status, body FROM sources WHERE id=?", (sid,)).fetchone()["status"] == "蒸馏失败"
    assert fresh.execute("SELECT body FROM sources WHERE id=?", (sid,)).fetchone()["body"] == "已提交正文"
    assert fresh.execute("SELECT state FROM queue WHERE source_id=?", (sid,)).fetchone()["state"] == "failed"
    fresh.execute("UPDATE sources SET last_error=? WHERE id=?", ("下一请求", sid))
    fresh.commit()


def test_worker_rollback_failure_keeps_the_original_error(tmp_path, monkeypatch):
    """工作线程丢掉自己的连接时，原异常仍在，下一次 db() 另开一条。"""

    engine = _open(tmp_path, monkeypatch)
    sid = "rebench-worker"
    _bench_source(engine, sid)
    main = engine.db()
    box: dict = {}

    def run() -> None:
        inner = engine.db()
        dirty = _dirty_connection(inner)
        engine._LOCAL.con = dirty
        try:
            engine.rebench_source(sid)
        except BaseException as exc:
            box["exc"] = exc
        box["cached"] = getattr(engine._LOCAL, "con", None)
        fresh = engine.db()
        box["fresh"] = fresh
        box["dirty"] = dirty
        box["status"] = fresh.execute("SELECT status FROM sources WHERE id=?", (sid,)).fetchone()["status"]
        box["state"] = fresh.execute("SELECT state FROM queue WHERE source_id=?", (sid,)).fetchone()["state"]

    worker = threading.Thread(target=run)
    worker.start()
    worker.join()
    assert isinstance(box["exc"], RuntimeError)
    assert str(box["exc"]) == "commit blew up"
    assert box["cached"] is not box["dirty"]
    assert box["fresh"] is not box["dirty"]
    assert box["status"] == "蒸馏失败"
    assert box["state"] == "failed"
    assert engine.db() is main
    assert engine.db().execute("SELECT body FROM sources WHERE id=?", (sid,)).fetchone()["body"] == "已提交正文"


def test_rebench_commit_runtime_error_rolls_the_write_back(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    sid = "rebench-runtime"
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,filename)
           VALUES(?,?,?,?,?,?,?,1,?)""",
        (sid, "file", "2026-11-10T04:28:11+00:00", "h", "评测正文", "已向量化待蒸馏", "p3", "wiki-bench-a.md"),
    )
    engine.db().execute(
        "INSERT INTO queue(source_id,lane,state,attempts,last_error) VALUES(?,?, 'failed', 2, ?)",
        (sid, "p3", "停了"),
    )
    engine.db().commit()
    original = _commit_raises(engine, RuntimeError("commit blew up"))
    try:
        with pytest.raises(RuntimeError, match="commit blew up"):
            engine.rebench_source(sid)
    finally:
        _restore_commit(engine, original)
    assert engine.db().in_transaction is False
    assert engine.db().execute("SELECT state FROM queue WHERE source_id=?", (sid,)).fetchone()["state"] == "failed"


def _failed_note(engine, tmp_path, sid: str, text: str = "甲在南港工作多年。") -> str:
    scan = tmp_path / "scan"
    scan.mkdir(exist_ok=True)
    (scan / "note.md").write_text(text, encoding="utf-8")
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,filename,pipeline,last_error)
           VALUES(?,?,?,?,?,?,?,1,?,?,?)""",
        (
            sid,
            "file",
            "2026-11-12T00:00:00+00:00",
            sid,
            "",
            "解析失败",
            "p3",
            "note.md",
            "解析:fail",
            "没有抽出正文",
        ),
    )
    engine.db().commit()
    return sid


def _assert_reread_untouched(engine, tmp_path, sid: str) -> None:
    con = engine.db()
    assert con.in_transaction is False
    source = con.execute("SELECT status, body FROM sources WHERE id=?", (sid,)).fetchone()
    card = con.execute("SELECT status FROM source_cards WHERE id=?", (sid,)).fetchone()
    queued = con.execute(
        "SELECT COUNT(*) AS n FROM queue WHERE source_id=? AND state='queued'",
        (sid,),
    ).fetchone()["n"]
    assert source["status"] == "解析失败"
    assert "甲在南港" not in (source["body"] or "")
    assert card["status"] == "解析失败"
    assert queued == 0
    other = sqlite3.connect(tmp_path / "rogmem.sqlite")
    try:
        other.execute("PRAGMA busy_timeout=300")
        other.execute("UPDATE sources SET last_error=? WHERE id=?", ("旁路", sid))
        other.commit()
        assert other.execute("SELECT status FROM sources WHERE id=?", (sid,)).fetchone()[0] == "解析失败"
    finally:
        other.close()


def test_reread_rolls_back_when_planning_raises(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    sid = _failed_note(engine, tmp_path, "note-plan")

    def explode(con, source_id, planned):
        con.execute(
            "INSERT INTO chunks(source_id,seq,text) VALUES(?,0,?)",
            (source_id, "幽灵"),
        )
        raise RuntimeError("planned blew up")

    monkeypatch.setattr(engine, "_write_planned", explode)
    with pytest.raises(RuntimeError, match="planned blew up"):
        engine.retry_source(sid)
    assert engine.db().execute("SELECT COUNT(*) AS n FROM chunks WHERE source_id=?", (sid,)).fetchone()["n"] == 0
    _assert_reread_untouched(engine, tmp_path, sid)


def test_reread_rolls_back_when_vector_gate_raises(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    sid = _failed_note(engine, tmp_path, "note-gate")

    def explode(con, source_id, lane):
        con.execute(
            "INSERT INTO queue(source_id,lane,state,enqueued_at) VALUES(?,?, 'queued', ?)",
            (source_id, lane or "p3", "2026-11-12T01:00:00+00:00"),
        )
        raise RuntimeError("vector gate blew up")

    monkeypatch.setattr(engine, "_finish_vector_gate", explode)
    with pytest.raises(RuntimeError, match="vector gate blew up"):
        engine.retry_source(sid)
    _assert_reread_untouched(engine, tmp_path, sid)


def test_reread_rolls_back_when_commit_raises(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    sid = _failed_note(engine, tmp_path, "note-commit")
    original = _commit_raises(engine, RuntimeError("commit blew up"))
    try:
        with pytest.raises(RuntimeError, match="commit blew up"):
            engine.retry_source(sid)
    finally:
        _restore_commit(engine, original)
    _assert_reread_untouched(engine, tmp_path, sid)


def test_reread_error_note_rolls_back_when_commit_raises(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    sid = _failed_note(engine, tmp_path, "note-thin", text="!!!")
    original = _commit_raises(engine, RuntimeError("commit blew up"))
    try:
        with pytest.raises(RuntimeError, match="commit blew up"):
            engine.retry_source(sid)
    finally:
        _restore_commit(engine, original)
    source = engine.db().execute("SELECT status, last_error FROM sources WHERE id=?", (sid,)).fetchone()
    assert engine.db().in_transaction is False
    assert source["status"] == "解析失败"
    assert source["last_error"] == "没有抽出正文"


def test_failed_rollback_opens_a_fresh_connection(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    sid = _failed_note(engine, tmp_path, "note-dirty")
    inner = engine.db()

    class _Dirty:
        def rollback(self):
            raise sqlite3.OperationalError("rollback failed")

        def close(self):
            inner.close()

        def __getattr__(self, name):
            return getattr(inner, name)

    dirty = _Dirty()
    engine._DB = dirty
    engine._LOCAL.con = dirty

    def explode(con, source_id, lane):
        raise RuntimeError("vector gate blew up")

    monkeypatch.setattr(engine, "_finish_vector_gate", explode)
    with pytest.raises(RuntimeError, match="vector gate blew up"):
        engine.retry_source(sid)
    fresh = engine.db()
    assert fresh is not dirty
    assert fresh is not inner
    assert engine._DB is fresh
    fresh.execute("SELECT 1").fetchone()
    _assert_reread_untouched(engine, tmp_path, sid)
    fresh.execute("UPDATE sources SET last_error=? WHERE id=?", ("下一请求", sid))
    fresh.commit()


def test_note_reread_still_keeps_the_text(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    sid = _failed_note(engine, tmp_path, "note-ok")
    assert engine.retry_source(sid)["ok"] is True
    row = engine.db().execute("SELECT body, status, last_error FROM sources WHERE id=?", (sid,)).fetchone()
    card = engine.db().execute("SELECT status FROM source_cards WHERE id=?", (sid,)).fetchone()
    assert "甲在南港工作多年" in row["body"]
    assert row["status"] != "解析失败"
    assert not row["last_error"]
    assert card["status"] == row["status"]
    assert engine.db().in_transaction is False


def test_retry_page_shows_the_error_field(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem import ui

    html = ui.sources_page([], "file", {}, {"devices": [], "agents": [], "projects": []}, None)
    assert "data.error" in html


def test_rebuild_does_not_hold_a_write_across_embeds(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.set_embed_mode("hash")
    engine.db().execute(
        "INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready) VALUES('s','file',?,'h','甲','已向量化待蒸馏','p3',1)",
        ("2026-11-10T00:00:00+00:00",),
    )
    engine.db().execute("INSERT INTO chunks(source_id,seq,text) VALUES('s',0,'甲在南港')")
    engine.db().execute("INSERT INTO chunks(source_id,seq,text) VALUES('s',1,'乙在北城')")
    engine.db().commit()
    seen = []

    def spy(text):
        seen.append(engine.db().in_transaction)
        return [0.2, 0.4]

    monkeypatch.setattr(engine, "_embed_one", spy)
    assert engine.rebuild_chunk_vectors() == 2
    assert seen == [False, False]


def test_retry_reads_a_picture_only_body_from_the_file(tmp_path, monkeypatch):
    """Picture names are not a reading. Retry reads the file again and drops those chunks."""
    engine = _open(tmp_path, monkeypatch)
    name = "南港账册.md"
    scan = tmp_path / "scan"
    scan.mkdir(exist_ok=True)
    (scan / name).write_text("甲在南港记下了三笔账。", encoding="utf-8")
    picture = "![南港账册 _page_1.jpg](images/南港账册 _page_1.jpg)"
    sid = "pic-body"
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,filename,pipeline,coverage)
           VALUES(?,?,?,?,?,?,?,1,?,?,?)""",
        (
            sid,
            "file",
            "2026-11-13T09:14:45+00:00",
            "h",
            picture,
            "蒸馏已结晶",
            "p3",
            name,
            "完成:ok~向量:ok",
            "pages",
        ),
    )
    engine.db().execute(
        "INSERT INTO chunks(source_id,seq,loc,text,role) VALUES(?,0,'0-9',?,'child')",
        (sid, picture),
    )
    engine.db().execute(
        "INSERT INTO queue(source_id,lane,state,enqueued_at,attempts) VALUES(?, 'p3', 'done', ?, 0)",
        (sid, "2026-11-14T04:39:53+00:00"),
    )
    engine.db().execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,created_at)
           VALUES('stmt-pic','crystal-pic','正文',?,?,?,'摘录','摘录',?)""",
        ("图里的字", picture, sid, "2026-11-13T09:20:00+00:00"),
    )
    engine.db().commit()

    assert engine.retry_source(sid)["ok"] is True
    row = engine.db().execute(
        "SELECT body, status, last_error, coverage FROM sources WHERE id=?",
        (sid,),
    ).fetchone()
    chunks = [
        item["text"]
        for item in engine.db().execute("SELECT text FROM chunks WHERE source_id=?", (sid,))
    ]
    quote = engine.db().execute(
        "SELECT invalid_at FROM statements WHERE id='stmt-pic'"
    ).fetchone()
    queued = engine.db().execute(
        "SELECT state FROM queue WHERE source_id=? AND state!='done'",
        (sid,),
    ).fetchone()
    assert "甲在南港记下了三笔账" in row["body"]
    assert "![" not in (row["body"] or "")
    assert row["status"] == "已向量化待蒸馏"
    assert not row["last_error"]
    assert row["coverage"] in {None, ""}
    assert chunks and all("![" not in text for text in chunks)
    assert any("甲在南港记下了三笔账" in text for text in chunks)
    assert quote["invalid_at"]
    assert queued["state"] == "queued"
    assert engine.db().in_transaction is False


def test_retry_keeps_a_body_that_already_has_words(tmp_path, monkeypatch):
    """A reading that already has words is distilled again. The file is not read over it."""
    engine = _open(tmp_path, monkeypatch)
    scan = tmp_path / "scan"
    scan.mkdir(exist_ok=True)
    (scan / "夜班.md").write_text("磁盘上是另一句。", encoding="utf-8")
    sid = "word-body"
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready,filename)
           VALUES(?,?,?,?,?,?,?,1,?)""",
        (
            sid,
            "file",
            "2026-11-13T09:14:45+00:00",
            "h",
            "乙在北港值了夜班。",
            "蒸馏失败",
            "p3",
            "夜班.md",
        ),
    )
    engine.db().commit()
    assert engine.retry_source(sid)["ok"] is True
    row = engine.db().execute("SELECT body, status FROM sources WHERE id=?", (sid,)).fetchone()
    assert row["body"] == "乙在北港值了夜班。"
    assert "磁盘上是另一句" not in row["body"]
    assert row["status"] == "蒸馏中"
