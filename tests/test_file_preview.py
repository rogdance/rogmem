"""文件资料「文件预览」：入口顺序、原文接口、随包渲染器。

原件仍在扫描目录里，服务端只按 source id 把字节发回；渲染全在浏览器里做，
不把文件送到任何外部服务。这几项守住入口、边界和 9 种格式的覆盖。
"""

from __future__ import annotations

from starlette.testclient import TestClient

from rogmem import file_preview, ui

ROW = {
    "id": "src-1",
    "kind": "file",
    "filename": "周报.docx",
    "body": "这是正文",
    "status": "已解析待向量化",
    "_part": "text",
}


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _app_client():
    from rogmem.app import mcp

    return TestClient(mcp.streamable_http_app())


def _client(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    return _app_client()


def _insert(engine, *, sid="src-1", kind="file", filename="周报.docx"):
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,filename)
           VALUES(?,?,'2026-11-22T00:00:00+00:00','h','这是正文','已解析','fast',?)""",
        (sid, kind, filename),
    )
    engine.db().commit()


# ---- 页面入口 ----


def test_the_tab_is_renamed_and_preview_sits_in_the_middle():
    html = ui.source_detail(ROW, [], None, "file")
    tabs = html.split("<nav class='wiki-tabs'>", 1)[1].split("</nav>", 1)[0]
    assert tabs.index("文本解析") < tabs.index("文件预览") < tabs.index("关联结晶")
    assert "原文解析" not in html
    assert "&part=preview" in tabs


def test_session_detail_has_no_file_preview():
    session = ui.source_detail({**ROW, "kind": "user", "_part": "preview"}, [], None, "session")
    assert "文件预览" not in session
    assert "文本解析" in session
    assert "关联结晶" in session


def test_preview_part_paints_the_offline_viewer_and_the_raw_file_link():
    html = ui.source_detail({**ROW, "_part": "preview"}, [], None, "file")
    assert "id='file-preview-stage'" in html
    assert "/admin/static/preview/omni-doc-viewer.iife.js" in html
    assert "/admin/static/preview/pdfjs/build/pdf.worker.min.mjs" in html
    assert "/admin/api/sources/src-1/file" in html
    assert "download=1" in html
    assert "<nav class='wiki-tabs'>" in html
    assert "class='on' href='/admin/sources/src-1?from=file&part=preview'" in html
    assert "这是正文" not in html  # 预览页不重复画文本解析的正文


def test_clicking_a_file_name_in_the_list_opens_the_preview():
    row = ui.source_row_html(ROW, "file")
    assert "&part=preview'>周报.docx</a>" in row
    assert ">查看</a>" in row  # 详情页的文本解析仍留着入口


# ---- 9 种格式 ----


def test_every_supported_suffix_has_a_mime_and_a_renderer():
    assert set(ui.FILE_TYPES) == set(file_preview.MIME)
    for suffix in ui.FILE_TYPES:
        assert file_preview.suffix_of("知识文件." + suffix) == suffix
        assert file_preview.mime_for("知识文件." + suffix) != "application/octet-stream"


def test_bundled_assets_and_licences_are_present():
    root = file_preview.ASSET_ROOT
    for rel in (
        "omni-doc-viewer.iife.js",
        "LICENSE.omni-doc-viewer.txt",
        "NOTICE.omni-doc-viewer.txt",
        "REVISION",
        "pdfjs/build/pdf.worker.min.mjs",
        "pdfjs/standard_fonts",
        "pdfjs/cmaps",
    ):
        assert (root / rel).exists(), rel
    assert "window.RogMemDoc" in (root / "omni-doc-viewer.iife.js").read_text(encoding="utf-8")


# ---- 静态资源 ----


def test_static_route_serves_the_viewer_and_stays_inside_vendor(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    script = client.get("/admin/static/preview/omni-doc-viewer.iife.js")
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert b"RogMemDoc" in script.content
    worker = client.get("/admin/static/preview/pdfjs/build/pdf.worker.min.mjs")
    assert worker.status_code == 200
    assert worker.headers["content-type"].startswith("text/javascript")
    assert client.get("/admin/static/preview/nope.js").status_code == 404
    assert client.get("/admin/static/preview/../../../rogmem/engine.py").status_code == 404


# ---- 原文接口 ----


def test_raw_file_is_served_inline_with_the_right_mime(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _insert(engine)
    scan = tmp_path / "scan"
    (scan / "周报.docx").write_bytes(b"PK-ORIGINAL-BYTES")
    client = _app_client()

    response = client.get("/admin/api/sources/src-1/file")
    assert response.status_code == 200
    assert response.content == b"PK-ORIGINAL-BYTES"
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert response.headers["content-disposition"].startswith("inline;")
    assert "X-Content-Type-Options" in response.headers

    download = client.get("/admin/api/sources/src-1/file?download=1")
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment;")


def test_all_nine_suffixes_get_their_own_mime(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    scan = tmp_path / "scan"
    client = _app_client()
    for index, suffix in enumerate(ui.FILE_TYPES):
        sid = f"src-{index}"
        name = f"sample-{index}.{suffix}"
        _insert(engine, sid=sid, filename=name)
        (scan / name).write_text("内容", encoding="utf-8")
        response = client.get(f"/admin/api/sources/{sid}/file")
        assert response.status_code == 200, suffix
        assert response.headers["content-type"].startswith(file_preview.mime_for(name)), suffix


def test_missing_session_and_out_of_scope_files_are_not_exposed(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    scan = tmp_path / "scan"
    (scan / "周报.docx").write_bytes(b"OK")
    _insert(engine, sid="session-1", kind="user", filename="周报.docx")
    _insert(engine, sid="missing-1", filename="没存盘.docx")
    _insert(engine, sid="outside-1", filename="../../外面的.txt")
    (tmp_path / "外面的.txt").write_text("不该被读到", encoding="utf-8")
    client = _app_client()

    for sid in ("session-1", "missing-1", "outside-1", "no-such-id"):
        assert client.get(f"/admin/api/sources/{sid}/file").status_code == 404, sid
