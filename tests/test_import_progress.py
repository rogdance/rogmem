"""文件资料点确定后，列表先出现文件名和上传进度，收好之后才换成红绿灯。"""

from rogmem.ui import sources_page


def _file_row() -> dict:
    return {
        "id": "s",
        "kind": "file",
        "preview": "",
        "filename": "已经进来.md",
        "device_name": "书桌",
        "agent_name": "极空间",
        "status": "已解析待向量化",
        "pipeline": "解析:ok",
        "ingested_at": "2026-11-12T00:00:00+00:00",
        "project_ref": "",
        "note": "",
    }


def test_a_batch_sends_one_file_and_then_the_next():
    """One document that the connection cannot carry must not discard the rest."""
    html = sources_page([], "file", {}, {})
    script = html.split("function showDuplicates", 1)[1].split("</script>", 1)[0]
    queue = script.split("function queueFiles", 1)[1].split("function sendFiles", 1)[0]
    upload = script.split("function uploadFile", 1)[1].split("function queueFiles", 1)[0]
    from rogmem.engine import DOCUMENT_MAX_BYTES

    assert "forEach" not in queue
    assert "file.size" in queue
    assert "文件太大" in queue
    assert str(DOCUMENT_MAX_BYTES) in queue
    assert queue.index("file.size") < queue.index("uploadFile(")
    assert "failRow(row, (item && item.error) || '没有导入')" in upload


def test_choosing_files_paints_names_before_the_upload_starts():
    html = sources_page([], "file", {}, {})
    script = html.split("function showDuplicates", 1)[1].split("</script>", 1)[0]
    queue = script.split("function queueFiles", 1)[1].split("function sendFiles", 1)[0]
    send = script.split("function sendFiles", 1)[1].split("function knowledgeDropHasFiles", 1)[0]
    upload = script.split("function uploadFile", 1)[1].split("function queueFiles", 1)[0]
    assert queue.index("revealFiles(files)") < queue.index("uploadFile(")
    assert "queueFiles(files)" in send
    assert upload.index("xhr.open") < upload.index("xhr.send")
    assert "location" not in queue
    assert "textContent = '100%'" not in script
    assert "textContent = '导入中'" in script
    assert "row_html" in script
    assert "dataset.total" in script
    assert "function paintFoot" in script
    assert "data-page-size='" in html
    assert "data-pages" in script
    assert "formatSourceClocks" in html
    assert "upload-meter" in html
    session = sources_page([], "session", {}, {})
    assert "function sendFiles" not in session
    assert "function queueFiles" not in session


def test_dropping_files_reuses_the_same_upload_queue():
    """Drag-and-drop must start the existing sequential import path, not a second pipeline."""
    html = sources_page([], "file", {}, {})
    script = html.split("function showDuplicates", 1)[1].split("</script>", 1)[0]
    drop = script.split("function bindKnowledgeDrop", 1)[1]
    assert "main .content.drop-ready" in html
    assert "function queueFiles" in script
    assert "function bindKnowledgeDrop" in script
    assert drop.index("'dragenter'") < drop.index("'drop'")
    assert "queueFiles(event.dataTransfer" in drop
    assert "dropEffect = 'copy'" in drop
    assert "classList.toggle('drop-ready'" in drop
    assert "nas-import" in drop
    assert "/admin/api/sources/import" in script
    assert script.index("function queueFiles") < script.index("function bindKnowledgeDrop")
    session = sources_page([], "session", {}, {})
    assert "bindKnowledgeDrop" not in session


def test_a_finished_file_keeps_the_lamps():
    from rogmem.ui import source_row_html

    row = _file_row()
    html = sources_page([row], "file", {}, {})
    table = html.split("<table class='sources'>", 1)[1].split("</table>", 1)[0]
    assert source_row_html(row, "file") in table
    assert "class='flow'" in table
    assert "upload-meter" not in table
    assert "已经进来.md" in table


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    from starlette.testclient import TestClient
    from rogmem.app import mcp

    return TestClient(mcp.streamable_http_app())


def test_a_received_file_comes_back_with_the_lamps(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = "这一句是为了让导入回执带上红绿灯。".encode()
    first = client.post(
        "/admin/api/sources/import",
        files=[
            ("files", ("pace.txt", body, "text/plain")),
            ("files", ("other.txt", "另一句也要单独带上处理状态。".encode(), "text/plain")),
        ],
    )
    assert first.status_code == 200
    imported = first.json()["imported"]
    assert len(imported) == 2
    assert all(item["ok"] for item in imported)
    assert all(item["row_html"].startswith("<tr") for item in imported)
    assert all("data-source-id" in item["row_html"] for item in imported)
    assert all("class='flow'" in item["row_html"] for item in imported)
    assert all("lamp" in item["row_html"] for item in imported)
    assert "pace.txt" in imported[0]["row_html"]
    assert imported[0]["id"]
    from rogmem import engine
    from rogmem.ui import source_row_html

    stored = dict(
        engine.db().execute(
            """SELECT id, kind, filename, device_name, agent_name, project_ref, ingested_at,
                      status, pipeline, last_error, redacted, coverage
               FROM sources WHERE id=?""",
            (imported[0]["id"],),
        ).fetchone()
    )
    stored["note"] = engine.list_note(stored)
    assert imported[0]["row_html"] == source_row_html(stored, "file")
    again = client.post(
        "/admin/api/sources/import",
        files={"files": ("pace.txt", body, "text/plain")},
    )
    duplicate = again.json()["imported"][0]
    assert duplicate["duplicate"] is True
    assert "row_html" not in duplicate
    refused = client.post(
        "/admin/api/sources/import",
        files={"files": ("pace.exe", b"nope", "application/octet-stream")},
    )
    rejected = refused.json()["imported"][0]
    assert rejected["ok"] is False
    assert rejected["error"] == "不接受的文件类型"
    assert "row_html" not in rejected
