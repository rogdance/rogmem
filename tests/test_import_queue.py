"""上传先秒回一条排队中的行，读文交给读文件的线程，列表订阅这几行直到它们停下。

一个进程里每根线程各有一条 SQLite 连接，主线程和 TestClient 的请求线程不共用。
因此这里的用例要么全走 HTTP，要么全在主线程直调 engine，不把两边混在一个断言里。
"""

import hashlib
import secrets

from rogmem import engine
from rogmem.progress import ST_QUEUED


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    from starlette.testclient import TestClient
    from rogmem.app import mcp

    return TestClient(mcp.streamable_http_app())


def _upload(client, name, body):
    return client.post(
        "/admin/api/sources/import",
        files={"files": (name, body, "text/plain")},
    ).json()["imported"][0]


def _stage(tmp_path, monkeypatch, name, body: bytes) -> dict:
    """在主线程把一份文件放进扫描目录并排队，不经过请求线程。"""
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    staged = tmp_path / "scan" / f".part-{secrets.token_hex(8)}"
    staged.write_bytes(body)
    return engine.queue_staged(name, staged, hashlib.sha256(body).hexdigest())


def _status(source_id):
    row = engine.db().execute(
        "SELECT status, pipeline, body FROM sources WHERE id=?", (source_id,)
    ).fetchone()
    return dict(row) if row else None


def test_the_upload_endpoint_answers_queued_and_the_watch_agrees(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    item = _upload(client, "note.txt", "排队中的一句原文。".encode())
    assert item["ok"] is True
    assert "排队中" in item["row_html"]
    assert item["active"] is True

    rows = client.get("/admin/api/sources/progress", params={"ids": item["id"]}).json()["rows"]
    assert len(rows) == 1
    assert rows[0]["id"] == item["id"]
    assert rows[0]["progress"] == "排队中"
    assert rows[0]["active"] is True
    assert rows[0]["row_html"] == item["row_html"]

    empty = client.get("/admin/api/sources/progress", params={"ids": ""})
    assert empty.json()["rows"] == []


def test_the_same_bytes_do_not_make_a_second_row(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = "同一份内容只留一条。".encode()
    first = _upload(client, "same.txt", body)
    again = _upload(client, "same.txt", body)
    assert again["duplicate"] is True
    assert again["id"] == first["id"]
    assert "row_html" not in again


def test_the_reader_finishes_a_queued_file(tmp_path, monkeypatch):
    item = _stage(tmp_path, monkeypatch, "note.txt", "排队中的一句原文。".encode())
    assert item["ok"] is True

    stored = _status(item["id"])
    assert stored["status"] == ST_QUEUED
    assert stored["body"] == ""
    assert item["id"] in engine.pending_ingests(5)

    assert engine.ingest_queued(item["id"]) == "done"

    after = _status(item["id"])
    assert after["status"] == "已向量化待萃取"
    assert after["body"] == "排队中的一句原文。"
    chunks = engine.db().execute(
        "SELECT COUNT(*) AS n FROM chunks WHERE source_id=?", (item["id"],)
    ).fetchone()["n"]
    assert chunks >= 1
    assert engine.pending_ingests(5) == []


def test_the_scan_loop_reads_a_queued_file_instead_of_copying_it(tmp_path, monkeypatch):
    """扫描线程是秒回那条的后手：读文件的线程不在了，它把这一份读完，不另开一条。"""
    body = "扫描目录里的这一份只算一条。".encode()
    item = _stage(tmp_path, monkeypatch, "once.txt", body)
    digest = engine.db().execute(
        "SELECT content_hash FROM sources WHERE id=?", (item["id"],)
    ).fetchone()["content_hash"]

    engine.scan_dir()

    rows = engine.db().execute(
        "SELECT id, status, body FROM sources WHERE content_hash=? AND kind='file'",
        (digest,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == item["id"]
    assert rows[0]["status"] != ST_QUEUED
    assert rows[0]["body"] == body.decode()


def test_a_read_that_cannot_land_does_not_stay_claimed(tmp_path, monkeypatch):
    """读没成的时候不能留一个「解析中」挂在那里：要么回到队列，要么记成读取失败。"""
    item = _stage(tmp_path, monkeypatch, "gone.txt", "文件已经不在了。".encode())
    (tmp_path / "scan" / "gone.txt").unlink()

    assert engine.ingest_queued(item["id"]) == "failed"

    stored = _status(item["id"])
    assert stored["status"] == "解析失败"
    assert engine.pending_ingests(5) == []


def test_the_list_page_carries_both_the_fresh_hint_and_the_row_watch():
    """#170 的探测条与本页的行订阅各做各的：探测条提示别处的新入库，订阅把刚导入的行走完。

    导入落行之后要顺手把探测条的基线抬到最新，否则过 20 秒它会报一条已经看得见的「有新的」。
    """
    from rogmem.ui import sources_page

    html = sources_page([], "file", {}, {})
    assert "id='sources-fresh'" in html
    assert "function pollSources" in html
    assert "function rebaselineFresh" in html
    assert "watchSource(item.id); rebaselineFresh();" in html


def test_a_watched_row_turns_quiet_after_the_reader_runs(tmp_path, monkeypatch):
    item = _stage(tmp_path, monkeypatch, "watch.txt", "这一行起来之后要停。".encode())

    moving = engine.source_rows_by_ids([item["id"]])
    assert len(moving) == 1
    assert moving[0]["active"] is True
    assert moving[0]["progress"] == "排队中"

    engine.ingest_queued(item["id"])

    settled = engine.source_rows_by_ids([item["id"]])
    assert settled[0]["active"] is False
    assert settled[0]["progress"] == ""
