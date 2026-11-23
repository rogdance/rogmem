"""大文件资料：按体量分级，整本书不进服务端 HTML、不进浏览器 DOM。

一次生产事故的根因是详情页按「一轮对话几百字」设计，把 3 MB 全文整本
塞进 <pre>。这里守住体量预算、分片边界、编码探测、复杂度闸门和异步隔离。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from starlette.requests import Request

from rogmem import document_view, file_preview, ui

BIG_ROW = {
    "id": "src-big",
    "kind": "file",
    "filename": "全集.txt",
    "body": "开头" + "正文" * 200000,
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


def _insert(engine, *, sid="src-big", filename="全集.txt", body="开头"):
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,filename)
           VALUES(?,?,'2026-11-22T00:00:00+00:00','h',?,'已解析','fast',?)""",
        (sid, "file", body, filename),
    )
    engine.db().commit()


def _big_file(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    scan = tmp_path / "scan"
    scan.mkdir(parents=True, exist_ok=True)
    (scan / "全集.txt").write_bytes(("明朝" * 100000).encode("gb18030"))
    _insert(engine)
    return engine


# ---- 体量判定 ----


def test_small_file_keeps_the_offline_viewer():
    body = file_preview.preview_body("src-1", "周报.docx", 4096)
    assert "renderDocument" in body
    assert "omni-doc-viewer.iife.js" in body
    assert "/admin/api/sources/src-1/file" in body


def test_large_file_never_renders_the_whole_document():
    body = file_preview.preview_body("src-big", "全集.txt", 3190811)
    assert "renderDocument" not in body
    assert "omni-doc-viewer.iife.js" not in body
    assert "/admin/api/sources/src-big/text-window" in body
    assert "textContent" in body
    assert "上一片" in body and "下一片" in body
    assert "3.0 MB" in body
    for marker in ("__WINDOW_URL__", "__WINDOW_BYTES__", "__SIZE__", "__FILENAME__", "__FILE_URL__"):
        assert marker not in body


def test_needs_window_is_about_size_not_filename():
    assert document_view.needs_window(3190811)
    assert not document_view.needs_window(1024)
    assert not document_view.needs_window(0)


# ---- 编码与分片 ----


def test_decode_document_detects_utf8_and_gbk():
    assert document_view.decode_document("明朝".encode("utf-8")) == ("明朝", "utf-8")
    assert document_view.decode_document("明朝".encode("gb18030")) == ("明朝", "gb18030")


def test_window_bounds_are_clamped(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"x" * 400000)
    first = document_view.read_window(path, 0, 999999999)
    assert first["limit"] <= document_view.WINDOW_BYTES
    assert first["offset"] == 0 and first["has_prev"] is False
    assert first["has_more"] is True
    late = document_view.read_window(path, 999999999, document_view.WINDOW_BYTES)
    assert late["offset"] == 400000
    neg = document_view.read_window(path, -50, 0)
    assert neg["offset"] == 0 and neg["limit"] >= 1


def test_window_does_not_split_a_multibyte_character(tmp_path):
    path = tmp_path / "gbk.txt"
    path.write_bytes(("明朝那些事儿" * 40000).encode("gb18030"))
    window = document_view.read_window(path, 0, document_view.WINDOW_BYTES)
    assert window["encoding"] == "gb18030"
    assert "\ufffd" not in window["text"]
    assert window["text"].startswith("明朝")
    following = document_view.read_window(path, window["next_offset"])
    assert "\ufffd" not in following["text"]
    assert window["next_offset"] == window["offset"] + window["limit"]


# ---- 页面直出预算 ----


def test_text_tab_truncates_a_whole_book():
    html = ui.source_detail(BIG_ROW, [], None, "file")
    assert len(html) < 400000
    assert "文本解析只显示开头" in html
    assert "结尾标记" not in html


def test_preview_tab_of_large_file_uses_the_window(tmp_path, monkeypatch):
    row = {**BIG_ROW, "_part": "preview", "_file_size": 3190811}
    html = ui.source_detail(row, [], None, "file")
    assert "renderDocument" not in html
    assert "/admin/api/sources/src-big/text-window" in html
    assert "按片显示" in html


def test_detail_page_marks_small_files_unchanged():
    row = {**BIG_ROW, "body": "这是正文", "_part": "preview", "id": "src-1"}
    html = ui.source_detail(row, [], None, "file")
    assert "renderDocument" in html


# ---- 服务端闸门 ----


def test_engine_skips_full_body_reconciliation_for_large_source(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _insert(engine, body="正文" * 300000)
    calls = []
    monkeypatch.setattr(engine, "_uncovered_sentences", lambda *a, **k: calls.append("retained") or [])
    monkeypatch.setattr(engine, "_source_fate", lambda *a, **k: calls.append("fate") or [])
    detail = engine.source_detail("src-big")
    assert calls == []
    assert detail["source"]["_detail_truncated"] is True
    assert detail["source"]["_retained"] == []
    assert detail["source"]["_fate"] == []


def test_engine_still_reconciles_small_sources(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _insert(engine, body="这是正文")
    calls = []
    monkeypatch.setattr(engine, "_uncovered_sentences", lambda *a, **k: calls.append("retained") or [])
    monkeypatch.setattr(engine, "_source_fate", lambda *a, **k: calls.append("fate") or [])
    engine.source_detail("src-big")
    assert calls == ["retained", "fate"]


# ---- 真路由：分片接口、上限、路径安全、异步隔离 ----


def test_window_route_returns_one_bounded_decoded_piece(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    _big_file(tmp_path, monkeypatch)
    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    res = client.get("/admin/api/sources/src-big/text-window?offset=0&limit=999999999")
    assert res.status_code == 200
    data = res.json()
    assert data["limit"] <= document_view.WINDOW_BYTES
    assert data["total"] > document_view.PREVIEW_INLINE_LIMIT
    assert data["encoding"] == "gb18030"
    assert data["text"].startswith("明朝")
    assert "\ufffd" not in data["text"]
    assert data["has_more"] is True
    second = client.get(f"/admin/api/sources/src-big/text-window?offset={data['next_offset']}")
    assert second.status_code == 200
    assert second.json()["offset"] == data["next_offset"]


def test_window_route_rejects_unknown_source_and_path_escape(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    engine = _big_file(tmp_path, monkeypatch)
    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    assert client.get("/admin/api/sources/nope/text-window").status_code == 404
    engine.db().execute(
        "UPDATE sources SET filename=? WHERE id=?", ("../../etc/passwd.txt", "src-big")
    )
    engine.db().commit()
    assert client.get("/admin/api/sources/src-big/text-window").status_code == 404


def test_detail_page_of_big_file_never_ships_the_whole_book(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    _big_file(tmp_path, monkeypatch)
    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    res = client.get("/admin/sources/src-big?from=file&part=preview")
    assert res.status_code == 200
    html = res.text
    assert "renderDocument" not in html
    assert "/admin/api/sources/src-big/text-window" in html
    assert len(html) < 400000
    text_tab = client.get("/admin/sources/src-big?from=file&part=text")
    assert text_tab.status_code == 200
    assert len(text_tab.text) < 400000


def test_detail_runs_slow_material_in_a_worker_thread(tmp_path, monkeypatch):
    from rogmem import app as app_module, engine

    _big_file(tmp_path, monkeypatch)
    seen = {}
    real = engine.source_detail

    def spy(sid):
        seen["thread"] = threading.current_thread().name
        return real(sid)

    monkeypatch.setattr(engine, "source_detail", spy)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/admin/sources/src-big",
        "query_string": b"from=file&part=preview",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
        "scheme": "http",
        "root_path": "",
        "http_version": "1.1",
        "path_params": {"sid": "src-big"},
    }
    response = asyncio.run(app_module.admin_source(Request(scope)))
    assert response.status_code == 200
    assert seen["thread"] != threading.current_thread().name


# ---- 按格式分级：文本类按片，二进制不误伤 ----


def test_suffix_decides_the_strategy():
    assert document_view.needs_window(3 * 1024 * 1024, "txt")
    assert not document_view.needs_window(3 * 1024 * 1024, "pdf")
    assert document_view.needs_download_only(40 * 1024 * 1024, "pdf")
    assert not document_view.needs_download_only(40 * 1024 * 1024, "txt")


def test_large_pdf_keeps_its_page_renderer_under_the_hard_limit():
    body = file_preview.preview_body("s", "书.pdf", 3 * 1024 * 1024)
    assert "renderDocument" in body
    assert "text-window" not in body


def test_oversized_binary_is_download_only():
    body = file_preview.preview_body("s", "书.pdf", 40 * 1024 * 1024)
    assert "renderDocument" not in body
    assert "text-window" not in body
    assert "下载原文件" in body
    assert "40.0 MB" in body
    assert "__SIZE__" not in body


def test_large_text_still_uses_windows_above_the_hard_limit():
    body = file_preview.preview_body("s", "全集.txt", 40 * 1024 * 1024)
    assert "renderDocument" not in body
    assert "/admin/api/sources/s/text-window" in body


def test_sniff_encoding_handles_utf8_and_gbk(tmp_path):
    utf8 = tmp_path / "u.txt"
    utf8.write_text("明朝那些事儿", encoding="utf-8")
    gbk = tmp_path / "g.txt"
    gbk.write_bytes("明朝那些事儿".encode("gb18030"))
    assert document_view.sniff_encoding(Path(utf8)) == "utf-8"
    assert document_view.sniff_encoding(Path(gbk)) == "gb18030"


def test_inline_original_declares_the_real_charset(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    engine = _open(tmp_path, monkeypatch)
    scan = tmp_path / "scan"
    scan.mkdir(parents=True, exist_ok=True)
    raw = "明朝那些事儿".encode("gb18030")
    (scan / "全集.txt").write_bytes(raw)
    _insert(engine)
    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    res = client.get("/admin/api/sources/src-big/file")
    assert res.status_code == 200
    assert res.content == raw  # 字节不被转码
    assert "charset=gb18030" in res.headers["content-type"]


def test_window_route_refuses_binary_suffixes(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    engine = _open(tmp_path, monkeypatch)
    scan = tmp_path / "scan"
    scan.mkdir(parents=True, exist_ok=True)
    (scan / "书.pdf").write_bytes(b"%PDF-1.7")
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,filename)
           VALUES('src-pdf','file','2026-11-22T00:00:00+00:00','h','','已解析','fast','书.pdf')"""
    )
    engine.db().commit()
    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    assert client.get("/admin/api/sources/src-pdf/text-window").status_code == 404


def test_pager_uses_server_offsets_not_fixed_chunk_size():
    body = file_preview.preview_body("src-big", "全集.txt", 3190811)
    assert "data.next_offset" in body
    assert "data.prev_offset" in body
    assert "load(prevAt)" in body and "load(nextAt)" in body
    assert "offset + piece" not in body


def test_paging_back_and_forward_keeps_characters_intact(tmp_path):
    path = tmp_path / "gbk.txt"
    path.write_bytes(("明朝那些事儿" * 40000).encode("gb18030"))
    first = document_view.read_window(path, 0)
    second = document_view.read_window(path, first["next_offset"])
    assert "\ufffd" not in second["text"]
    back = document_view.read_window(path, second["prev_offset"])
    assert back["offset"] == first["offset"]
    assert "\ufffd" not in back["text"]
