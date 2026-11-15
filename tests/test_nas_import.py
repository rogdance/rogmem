from pathlib import Path

import pytest


def _open(tmp_path, monkeypatch, nas: Path | None):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    if nas is not None:
        nas.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ROGMEM_NAS", str(nas if nas is not None else tmp_path / "missing-nas"))
    from rogmem import engine

    engine.connect(tmp_path / "data", tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def test_constants_match_the_spec():
    from rogmem import engine

    assert engine.NAS_PAGE == 200
    assert engine.NAS_CAP == 5000
    assert engine.NAS_IMPORT_MAX == 50


def test_missing_root_reports_that_the_folder_is_not_mounted(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch, None)
    assert engine.nas_root() is None
    assert engine.list_nas("") == {"ok": False, "error": "极空间目录还没挂进容器"}


def test_listing_skips_hidden_docker_and_recycle_and_orders_dirs_first(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    engine = _open(tmp_path, monkeypatch, nas)
    (nas / "b").mkdir()
    (nas / "a").mkdir()
    (nas / "A.md").write_text("甲", encoding="utf-8")
    (nas / ".secret.md").write_text("藏", encoding="utf-8")
    (nas / "封面.png").write_bytes(b"png")
    (nas / "Dockers").mkdir()
    (nas / "Dockers" / "secret.md").write_text("包", encoding="utf-8")
    (nas / "#recycle").mkdir()
    (nas / "#recycle" / "old.md").write_text("旧", encoding="utf-8")
    listed = engine.list_nas("")
    assert listed["ok"] is True
    assert listed["path"] == ""
    assert listed["offset"] == 0
    assert listed["limit"] == 200
    assert listed["more"] is False
    assert listed["truncated"] is False
    assert [item["name"] for item in listed["entries"]] == ["a", "b", "A.md", "封面.png"]
    kinds = {item["name"]: item["kind"] for item in listed["entries"]}
    assert kinds["a"] == "dir" and kinds["A.md"] == "file"
    accepted = {item["name"]: item.get("accepted") for item in listed["entries"]}
    assert accepted["A.md"] is True
    assert accepted["封面.png"] is False
    assert "Dockers" not in kinds
    assert engine.list_nas("Dockers") == {"ok": False, "error": "不能从这里导入"}
    assert engine.list_nas("#recycle") == {"ok": False, "error": "不能从这里导入"}
    assert engine.list_nas(".secret.md") == {"ok": False, "error": "不能从这里导入"}


def test_listing_refuses_paths_that_leave_the_root(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    engine = _open(tmp_path, monkeypatch, nas)
    outside = tmp_path / "outside.md"
    outside.write_text("外面的句子不该进来。", encoding="utf-8")
    (nas / "leak.md").symlink_to(outside)
    (nas / "out").symlink_to(tmp_path)
    listed = engine.list_nas("")
    assert [item["name"] for item in listed["entries"]] == []
    for rel in ("../outside.md", "/etc/passwd", "leak.md", "out", "foo\\bar", "a/../../outside.md", "ok/../leak.md"):
        assert engine.list_nas(rel) == {"ok": False, "error": "不能从这里导入"}


def test_listing_skips_a_symlink_loop_and_import_reports_it(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    engine = _open(tmp_path, monkeypatch, nas)
    loop = nas / "loop"
    loop.symlink_to(loop)
    (nas / "keep.md").write_text("留下的一句。", encoding="utf-8")
    listed = engine.list_nas("")
    assert listed["ok"] is True
    assert [item["name"] for item in listed["entries"]] == ["keep.md"]
    mixed = engine.import_nas(["loop", "keep.md"], None)
    assert mixed["imported"][0] == {"ok": False, "error": "不能从这里导入", "filename": "loop"}
    assert mixed["imported"][1]["ok"] is True
    assert mixed["imported"][1]["filename"] == "keep.md"
    assert _names(engine) == ["keep.md"]


def test_listing_follows_a_symlink_that_stays_inside(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    engine = _open(tmp_path, monkeypatch, nas)
    (nas / "real.md").write_text("里面的一句。", encoding="utf-8")
    (nas / "alias.md").symlink_to(nas / "real.md")
    folder = nas / "docs"
    folder.mkdir()
    (folder / "note.md").write_text("笔记一句。", encoding="utf-8")
    (nas / "link").symlink_to(folder)
    names = [item["name"] for item in engine.list_nas("")["entries"]]
    assert names == ["docs", "link", "alias.md", "real.md"]
    assert engine.list_nas("link/")["path"] == "link"
    assert [item["name"] for item in engine.list_nas("link")["entries"]] == ["note.md"]


def test_listing_pages_and_stops_at_five_thousand(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    engine = _open(tmp_path, monkeypatch, nas)
    for index in range(engine.NAS_CAP + 1):
        (nas / f"f{index:04d}.md").write_text("一句", encoding="utf-8")
    first = engine.list_nas("", 0)
    assert first["more"] is True
    assert first["truncated"] is True
    assert len(first["entries"]) == 200
    assert first["entries"][0]["name"] == "f0000.md"
    last = engine.list_nas("", 5000)
    assert last["entries"] == []
    assert last["more"] is False
    assert last["ok"] is True
    assert engine.list_nas("", "nope")["offset"] == 0
    assert engine.list_nas("", -3)["offset"] == 0


def _names(engine) -> list[str]:
    return sorted(path.name for path in (engine.active_scan() or Path()).iterdir())


def _count(engine) -> int:
    return engine.db().execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]


def test_import_reads_bytes_through_the_existing_door(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    engine = _open(tmp_path, monkeypatch, nas)
    (nas / "real.md").write_text("里面的一句。", encoding="utf-8")
    (nas / "alias.md").symlink_to(nas / "real.md")
    (nas / "docs").mkdir()
    (nas / "docs" / "note.md").write_text("子层不该被目录导入收走。", encoding="utf-8")
    (nas / "here.md").write_text("这一层的一句。", encoding="utf-8")
    (nas / "封面.png").write_bytes(b"png")
    assert engine.scan_dir(force=True) == 0
    assert _count(engine) == 0
    brought = engine.import_nas(["alias.md"], None)
    assert brought["ok"] is True
    assert brought["imported"][0]["duplicate"] is False
    assert brought["imported"][0]["filename"] == "alias.md"
    assert _names(engine) == ["alias.md"]
    body = engine.db().execute("SELECT body FROM sources").fetchone()["body"]
    assert "里面的一句。" in body
    again = engine.import_nas(["alias.md"], None)
    assert again["imported"][0]["duplicate"] is True
    assert _count(engine) == 1
    folder = engine.import_nas_dir("", None)
    assert folder["ok"] is True
    assert [item["duplicate"] for item in folder["imported"]] == [True, False, True]
    assert folder["imported"][1]["filename"] == "here.md"
    assert "here.md" in _names(engine)
    assert "real.md" in _names(engine)
    assert "note.md" not in _names(engine)
    refused = engine.import_nas(["封面.png"], None)
    assert refused["imported"][0]["ok"] is False
    assert refused["imported"][0]["error"] == "不接受的文件类型"
    assert "封面.png" not in _names(engine)


def test_import_rejects_escape_and_keeps_the_rest(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    engine = _open(tmp_path, monkeypatch, nas)
    outside = tmp_path / "outside.md"
    outside.write_text("外面的句子不该进来。", encoding="utf-8")
    (nas / "leak.md").symlink_to(outside)
    (nas / "keep.md").write_text("留下的一句。", encoding="utf-8")
    (nas / "docs").mkdir()
    (nas / "Dockers").mkdir()
    (nas / "Dockers" / "secret.md").write_text("包", encoding="utf-8")
    mixed = engine.import_nas(["../outside.md", "leak.md", "Dockers/secret.md", "keep.md", "docs"], None)
    assert [item["ok"] for item in mixed["imported"]] == [False, False, False, True, False]
    assert mixed["imported"][0]["error"] == "不能从这里导入"
    assert _names(engine) == ["keep.md"]
    assert "外面的句子不该进来。" not in engine.db().execute("SELECT body FROM sources").fetchone()["body"]


def test_import_stops_before_any_write_when_the_batch_is_too_big(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    engine = _open(tmp_path, monkeypatch, nas)
    paths = []
    for index in range(51):
        name = f"n{index:02d}.md"
        (nas / name).write_text("一句", encoding="utf-8")
        paths.append(name)
    assert engine.import_nas(paths, None) == {"ok": False, "error": "一次最多导入 50 个文件"}
    assert engine.import_nas_dir("", None) == {
        "ok": False,
        "error": "这一层有 51 个可导入文件，一次最多 50 个",
    }
    assert _names(engine) == []
    assert _count(engine) == 0
    assert engine.import_nas([], None) == {"ok": False, "error": "没有文件"}
    assert engine.import_nas_dir("missing", None) == {"ok": False, "error": "不能从这里导入"}
    empty = tmp_path / "empty-nas"
    engine = _open(tmp_path / "other", monkeypatch, empty)
    (empty / "封面.png").write_bytes(b"png")
    assert engine.import_nas_dir("", None) == {"ok": False, "error": "这一层没有可导入的文件"}


def test_a_read_error_fails_one_file_and_continues(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    engine = _open(tmp_path, monkeypatch, nas)
    (nas / "a.md").write_text("甲一句。", encoding="utf-8")
    (nas / "b.md").write_text("乙一句。", encoding="utf-8")

    def broken(path: Path) -> bytes:
        if path.name == "a.md":
            raise OSError("denied")
        return path.read_bytes()

    monkeypatch.setattr(engine, "_nas_read", broken)
    result = engine.import_nas(["a.md", "b.md"], None)
    assert result["imported"][0] == {"ok": False, "error": "没有这个文件", "filename": "a.md"}
    assert result["imported"][1]["ok"] is True
    assert _names(engine) == ["b.md"]


def test_unmounted_import_does_not_block_local_bytes(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch, None)
    assert engine.import_nas(["a.md"], None) == {"ok": False, "error": "极空间目录还没挂进容器"}
    local = engine.import_bytes("local.md", "本机的一句。".encode("utf-8"))
    assert local["ok"] is True
    assert _names(engine) == ["local.md"]


def _client(tmp_path, monkeypatch, nas: Path | None):
    from rogmem import rate_limit

    rate_limit.reset()
    _open(tmp_path, monkeypatch, nas)
    from starlette.testclient import TestClient
    from rogmem.app import build_app

    return TestClient(build_app())


def test_routes_use_the_admin_guard_and_the_import_receipt(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    nas.mkdir()
    (nas / "note.md").write_text("接口进来的一句。", encoding="utf-8")
    client = _client(tmp_path, monkeypatch, nas)
    opened = client.get("/admin/api/sources/nas", params={"path": "", "offset": "0"})
    assert opened.status_code == 200
    assert opened.json()["entries"][0]["name"] == "note.md"
    jail = client.get("/admin/api/sources/nas", params={"path": "../note.md"})
    assert jail.status_code == 400
    assert jail.json() == {"ok": False, "error": "不能从这里导入"}
    empty = client.post("/admin/api/sources/import-nas", json={})
    assert empty.status_code == 400
    assert empty.json() == {"ok": False, "error": "没有文件"}
    both = client.post("/admin/api/sources/import-nas", json={"paths": ["note.md"], "dir": ""})
    assert both.status_code == 400
    saved = client.post(
        "/admin/api/sources/import-nas",
        json={"paths": ["note.md"], "device_name": "书桌"},
    )
    assert saved.status_code == 200
    item = saved.json()["imported"][0]
    assert item["ok"] is True
    assert "class='flow'" in item["row_html"]
    from rogmem import engine

    assert engine.db().execute("SELECT device_name FROM sources").fetchone()["device_name"] == "书桌"
    missing = _client(tmp_path / "bare", monkeypatch, None)
    bare = missing.get("/admin/api/sources/nas")
    assert bare.status_code == 200
    assert bare.json() == {"ok": False, "error": "极空间目录还没挂进容器"}


def test_routes_reject_anonymous_callers_when_the_guard_is_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("rogmem.app._admin_ok", lambda _request: False)
    client = _client(tmp_path, monkeypatch, tmp_path / "nas")
    assert client.get("/admin/api/sources/nas").status_code == 401
    assert client.get("/admin/api/sources/nas").json() == {"error": "未登录"}
    denied = client.post("/admin/api/sources/import-nas", json={"dir": ""})
    assert denied.status_code == 401
    assert denied.json() == {"error": "未登录"}


def test_the_file_page_offers_both_imports_and_the_session_page_does_not():
    from rogmem.ui import sources_page

    files = sources_page([], "file", {}, {})
    session = sources_page([], "session", {}, {})
    assert 'id="import-open"' in files
    assert 'aria-haspopup="menu"' in files
    assert "从本机导入" in files
    assert "从极空间导入" in files
    assert 'id="nas-import"' in files
    assert 'aria-label="从极空间导入"' in files
    assert 'onchange="sendFiles(this)"' in files
    assert "function sendFiles" in files
    assert "function uploadFile" in files
    assert "从极空间导入" not in session
    assert "function sendFiles" not in session
    assert "background: var(--surface-popover)" in files
    assert "background: var(--surface-base)" in files
    assert "color: var(--text-primary)" in files
    assert "function nasOwns" in files
    load = files.split("async function loadNas", 1)[1].split("async function postNas", 1)[0]
    assert load.index("if (!nasOwns(session, request)) return;") < load.index("paintNas(")
    post = files.split("async function postNas", 1)[1].split("function setImportPhase", 1)[0]
    assert "if (nasImporting) return;" in post
    assert post.index("if (!nasOwns(session, request)) return;") < post.index("nas-result")


def test_saved_mount_overrides_the_environment_and_clearing_restores_it(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    other = tmp_path / "other"
    engine = _open(tmp_path, monkeypatch, nas)
    other.mkdir()
    (other / "note.md").write_text("另一处的一句。", encoding="utf-8")
    assert engine.nas_configured_path() == str(nas)
    saved = engine.save_basic({"nas_path": str(other) + "/"})
    assert saved["ok"] is True
    assert saved["config"]["nas_path"] == str(other)
    assert engine.get_setting("nas_path") == str(other)
    assert engine.nas_root() == other.resolve()
    listed = engine.list_nas("")
    assert [item["name"] for item in listed["entries"]] == ["note.md"]
    engine.save_basic({"owner_appellation": "阿禾"})
    assert engine.get_setting("nas_path") == str(other)
    cleared = engine.save_basic({"nas_path": "  "})
    assert cleared["ok"] is True
    assert engine.get_setting("nas_path") == ""
    assert engine.nas_root() == nas.resolve()
    assert cleared["config"]["nas_path"] == str(nas)


def test_a_saved_path_can_be_set_before_the_directory_exists(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch, tmp_path / "nas")
    pending = tmp_path / "later"
    saved = engine.save_basic({"nas_path": str(pending)})
    assert saved["ok"] is True
    assert engine.nas_root() is None
    assert engine.list_nas("") == {"ok": False, "error": "极空间目录还没挂进容器"}
    pending.mkdir()
    (pending / "ready.md").write_text("挂上之后能读。", encoding="utf-8")
    assert [item["name"] for item in engine.list_nas("")["entries"]] == ["ready.md"]


def test_relative_mount_path_is_rejected_and_leaves_the_stored_value(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    engine = _open(tmp_path, monkeypatch, nas)
    engine.save_basic({"nas_path": str(nas)})
    for raw in ("relative/dir", "nas", "../nas", "C:\\nas", "/nas\n/etc", "\x00/nas"):
        got = engine.save_basic({"nas_path": raw})
        assert got["ok"] is False, raw
        assert got["error"] == "挂载路径要是绝对路径"
        assert engine.get_setting("nas_path") == str(nas)
        assert engine.nas_root() == nas.resolve()


def test_basic_page_shows_the_mount_the_importer_reads(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    engine = _open(tmp_path, monkeypatch, nas)
    from rogmem.ui import basic_page

    unset = basic_page(engine.basic_config())
    plate = unset.split("<h2>个人空间</h2>", 1)[1].split("<h2>自动采集方案</h2>", 1)[0]
    assert "挂载路径" in plate
    assert "知识导入从这里读。" in plate
    assert 'name="nas_path"' in plate
    assert f'value="{nas}"' in plate
    assert "nas_path:this.nas_path.value" in unset
    assert 'name="scan_path"' not in plate
    engine.save_basic({"nas_path": "/mnt/personal"})
    saved = basic_page(engine.basic_config())
    assert 'name="nas_path" aria-label="个人空间挂载路径" value="/mnt/personal"' in saved
