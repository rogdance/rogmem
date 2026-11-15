"""真实引擎上的目录窗。临时目录，不连接真极空间。"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
pytest.importorskip("starlette")
pytest.importorskip("uvicorn")
from playwright.sync_api import sync_playwright

from rogmem import engine, rate_limit


class _Gate:
    def __init__(self):
        self.list_hold = None
        self.post_hold = None
        self.calls = []

    def release(self):
        if self.list_hold is not None:
            self.list_hold.set()
        if self.post_hold is not None:
            self.post_hold.set()


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _boot(tmp_path: Path, nas: Path):
    rate_limit.reset()
    data = tmp_path / "data"
    scan = tmp_path / "scan"
    nas.mkdir(parents=True, exist_ok=True)
    import os

    os.environ["ROGMEM_DATA"] = str(data)
    os.environ["ROGMEM_SCAN"] = str(scan)
    os.environ["ROGMEM_NAS"] = str(nas)
    os.environ["ROGMEM_EMBED"] = "off"
    os.environ["ROGMEM_NO_PASSWORD"] = "1"
    engine.connect(data, scan)
    engine.set_embed_mode("off")
    gate = _Gate()
    real_list = engine.list_nas
    real_import = engine.import_nas
    real_dir = engine.import_nas_dir

    def list_nas(rel="", offset=0):
        gate.calls.append(("list", "" if rel is None else str(rel), int(offset or 0)))
        if gate.list_hold is not None and str(rel or "") == gate.list_hold_path:
            gate.list_hold.wait(8)
        return real_list(rel, offset)

    def import_nas(paths, device_name=None):
        gate.calls.append(("paths", list(paths)))
        if gate.post_hold is not None:
            gate.post_hold.wait(8)
        return real_import(paths, device_name)

    def import_nas_dir(rel, device_name=None):
        gate.calls.append(("dir", "" if rel is None else str(rel)))
        if gate.post_hold is not None:
            gate.post_hold.wait(8)
        return real_dir(rel, device_name)

    gate.list_hold_path = "甲"
    engine.list_nas = list_nas
    engine.import_nas = import_nas
    engine.import_nas_dir = import_nas_dir
    import uvicorn
    from rogmem.app import build_app

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("live server did not start")

    def close():
        gate.release()
        server.should_exit = True
        thread.join(timeout=3)
        engine.list_nas = real_list
        engine.import_nas = real_import
        engine.import_nas_dir = real_dir
        engine.NAS_PAGE = 200

    return f"http://127.0.0.1:{port}", gate, close


def _page(browser, origin, width, height=None, theme="dark"):
    if height is None:
        height = 844 if width < 800 else 900
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    page.add_init_script(f"localStorage.setItem('rogmem-theme', '{theme}')")
    page.goto(origin + "/admin/sources?tab=file")
    page.wait_for_selector("#import-open")
    page.evaluate("window.__stay = 1")
    return context, page


def _open_nas(page):
    page.click("#import-open")
    page.click("#import-nas")
    page.wait_for_selector("#nas-import[open]")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            yield br
        finally:
            br.close()


def test_close_during_a_real_import_does_not_lock_or_navigate_the_next_session(tmp_path, browser):
    nas = tmp_path / "nas"
    (nas / "甲").mkdir(parents=True)
    (nas / "甲" / "甲文件.md").write_text("甲的一句。", encoding="utf-8")
    (nas / "乙").mkdir()
    (nas / "乙" / "乙文件.md").write_text("乙的一句。", encoding="utf-8")
    (nas / "慢.md").write_text("慢的一句。", encoding="utf-8")
    (nas / "样例.md").write_text("样例的一句。", encoding="utf-8")
    origin, gate, close = _boot(tmp_path, nas)
    try:
        context, page = _page(browser, origin, 1440)
        gate.post_hold = threading.Event()
        _open_nas(page)
        page.wait_for_selector("#nas-list input")
        page.locator("#nas-list input[value='慢.md']").check()
        page.locator("#nas-selected").click()
        page.wait_for_function("document.getElementById('nas-selected').disabled === true")
        assert page.locator("#nas-status").inner_text() == "导入中"
        lists_before_close = [item for item in gate.calls if item[0] == "list"]
        page.keyboard.press("Escape")
        page.wait_for_function("document.getElementById('nas-import').open === false")
        page.wait_for_function("document.activeElement.id === 'import-open'")
        assert page.evaluate("window.__stay") == 1
        _open_nas(page)
        page.wait_for_selector("text=乙")
        assert len([item for item in gate.calls if item[0] == "list"]) > len(lists_before_close)
        page.locator("#nas-list button", has_text="乙").click()
        page.wait_for_selector("text=乙文件.md")
        gate.post_hold.set()
        page.wait_for_timeout(300)
        assert page.evaluate("window.__stay") == 1
        assert "/admin/sources" in page.url
        assert page.locator("#nas-import").evaluate("node => node.open") is True
        assert "乙文件.md" in page.locator("#nas-list").inner_text()
        context.close()
    finally:
        close()


def test_a_slow_real_listing_drops_the_old_selection_and_the_late_directory(tmp_path, browser):
    nas = tmp_path / "nas"
    (nas / "甲").mkdir(parents=True)
    (nas / "甲" / "甲文件.md").write_text("甲的一句。", encoding="utf-8")
    (nas / "乙").mkdir()
    (nas / "乙" / "乙文件.md").write_text("乙的一句。", encoding="utf-8")
    (nas / "样例.md").write_text("上层的一句。", encoding="utf-8")
    (nas / "另一.md").write_text("另一句。", encoding="utf-8")
    origin, gate, close = _boot(tmp_path, nas)
    try:
        context, page = _page(browser, origin, 390)
        _open_nas(page)
        page.wait_for_selector("#nas-list input")
        page.locator("#nas-list input").first.check()
        gate.list_hold = threading.Event()
        page.locator("#nas-list button", has_text="甲").click()
        page.wait_for_function("document.getElementById('nas-status').textContent === '列出中'")
        assert page.locator("#nas-list input").count() == 0
        assert page.locator("#nas-selected").is_disabled() or page.locator("#nas-selected").is_hidden()
        page.locator("#nas-list button", has_text="乙").click()
        page.wait_for_selector("text=乙文件.md")
        gate.list_hold.set()
        page.wait_for_timeout(250)
        assert "乙文件.md" in page.locator("#nas-list").inner_text()
        assert "甲文件.md" not in page.locator("#nas-list").inner_text()
        assert not any(item[0] == "paths" and "样例.md" in item[1] for item in gate.calls)
        context.close()
    finally:
        close()


def test_real_engine_reports_limits_pages_xss_and_duplicate_without_leaving(tmp_path, browser):
    nas = tmp_path / "nas"
    page_dir = nas / "页"
    page_dir.mkdir(parents=True)
    for name in ("a.md", "b.md", "c.md"):
        (page_dir / name).write_text(name + "的一句。", encoding="utf-8")
    many = nas / "很多"
    many.mkdir()
    for index in range(51):
        (many / f"n{index:02d}.md").write_text("一句。", encoding="utf-8")
    (nas / "已有.md").write_text("已经在库里的一句。", encoding="utf-8")
    (nas / "标记<b>注入.md").write_text("标记不是标签。", encoding="utf-8")
    origin, gate, close = _boot(tmp_path, nas)
    engine.import_nas(["已有.md"], None)
    try:
        context, page = _page(browser, origin, 1440, theme="light")
        page.evaluate("window.__stay = 1")
        denied = page.request.get(origin + "/admin/api/sources/nas?path=../secret.md")
        assert denied.status == 400
        assert denied.json()["error"] == "不能从这里导入"
        marked = page.request.get(origin + "/admin/api/sources/nas?path=%3Cscript%3E")
        assert marked.status == 400
        _open_nas(page)
        page.wait_for_selector("#nas-list")
        assert page.locator("#nas-list b").count() == 0
        assert page.locator("#nas-list img").count() == 0
        assert "标记<b>注入.md" in page.locator("#nas-list").inner_text()
        engine.NAS_PAGE = 2
        page.locator("#nas-list button", has_text="页").click()
        page.wait_for_selector("#nas-page-next")
        assert page.locator("#nas-page-next").is_visible()
        page.locator("#nas-page-next").click()
        page.wait_for_function("document.getElementById('nas-status').textContent !== '列出中'")
        assert ("list", "页", 2) in gate.calls
        page.locator("#nas-crumb button", has_text="个人空间").click()
        page.locator("#nas-list button", has_text="很多").click()
        page.wait_for_selector("#nas-here")
        before = engine.db().execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
        page.locator("#nas-here").click()
        page.wait_for_function("document.getElementById('nas-status').textContent.includes('一次最多 50')")
        after = engine.db().execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
        assert after == before
        assert ("dir", "很多") in gate.calls
        engine.NAS_PAGE = 200
        page.locator("#nas-crumb button", has_text="个人空间").click()
        page.locator("#nas-list input[value='已有.md']").check()
        page.locator("#nas-selected").click()
        page.wait_for_selector("#nas-result a")
        assert page.locator("#nas-import").evaluate("node => node.open") is True
        page.keyboard.press("Escape")
        page.wait_for_function("document.activeElement.id === 'import-open'")
        assert page.evaluate("window.__stay") == 1
        context.close()
    finally:
        close()


def _chain(page) -> dict:
    return page.evaluate(
        """() => {
          const dialog = document.getElementById('nas-import');
          const list = document.getElementById('nas-list');
          const foot = document.querySelector('#nas-import .nas-foot');
          const scope = document.getElementById('nas-scope');
          const body = document.querySelector('#nas-import .nas-body');
          const lr = list.getBoundingClientRect();
          const fr = foot.getBoundingClientRect();
          const sr = scope.getBoundingClientRect();
          return {
            dialogClient: dialog.clientHeight,
            dialogScroll: dialog.scrollHeight,
            listClient: list.clientHeight,
            listScroll: list.scrollHeight,
            listBottom: lr.bottom,
            footTop: fr.top,
            scopeTop: sr.height ? sr.top : fr.top,
            bodyOverflow: getComputedStyle(body).overflowY,
            browse: dialog.classList.contains('nas-browse'),
            rows: list.querySelectorAll('.nas-row').length
          };
        }"""
    )


def _assert_list_scrolls_above_footer(page):
    data = _chain(page)
    assert data["browse"] is True, data
    assert data["rows"] >= 200, data
    assert data["listScroll"] > data["listClient"] + 40, data
    assert data["listClient"] > 80, data
    assert data["dialogScroll"] <= data["dialogClient"] + 2, data
    assert data["listBottom"] <= data["footTop"] + 1, data
    assert data["listBottom"] <= data["scopeTop"] + 1, data
    first = page.locator("#nas-list .nas-row").first
    first.locator("input").click()
    assert first.locator("input").is_checked()
    last = page.locator("#nas-list .nas-row").nth(199)
    last.scroll_into_view_if_needed()
    last_box = last.bounding_box()
    foot_box = page.locator("#nas-import .nas-foot").bounding_box()
    scope_box = page.locator("#nas-scope").bounding_box()
    assert last_box["y"] + last_box["height"] <= foot_box["y"] + 1
    if scope_box:
        assert last_box["y"] + last_box["height"] <= scope_box["y"] + 1
    last.locator("input").click()
    assert last.locator("input").is_checked()


def test_two_hundred_real_entries_scroll_inside_the_dialog(tmp_path, browser):
    nas = tmp_path / "nas"
    nas.mkdir()
    for index in range(201):
        (nas / f"f{index:03d}.md").write_text("一句。", encoding="utf-8")
    origin, gate, close = _boot(tmp_path, nas)
    try:
        for width, height, theme in ((390, 844, "dark"), (844, 390, "dark"), (1440, 900, "light")):
            context, page = _page(browser, origin, width, height, theme)
            _open_nas(page)
            page.wait_for_selector("#nas-list .nas-row")
            page.wait_for_function("document.querySelectorAll('#nas-list .nas-row').length >= 200")
            _assert_list_scrolls_above_footer(page)
            page.locator("#nas-page-next").click()
            page.wait_for_function("document.getElementById('nas-status').textContent !== '列出中'")
            assert ("list", "", 200) in gate.calls
            lone = page.locator("#nas-list .nas-row")
            assert lone.count() == 1
            lone.locator("input").click()
            assert lone.locator("input").is_checked()
            context.close()
    finally:
        close()


def test_aborted_import_unlocks_this_session_and_a_stale_abort_does_not(tmp_path, browser):
    nas = tmp_path / "nas"
    (nas / "下一层").mkdir(parents=True)
    (nas / "下一层" / "里面.md").write_text("里面的一句。", encoding="utf-8")
    (nas / "样例.md").write_text("样例的一句。", encoding="utf-8")
    origin, gate, close = _boot(tmp_path, nas)
    held = []

    def handle(route):
        if route.request.method == "POST" and route.request.url.endswith("/import-nas"):
            held.append(route)
            return
        route.fallback()

    try:
        context, page = _page(browser, origin, 390)
        page.route("**/admin/api/sources/import-nas", handle)
        _open_nas(page)
        page.wait_for_selector("#nas-here")
        page.locator("#nas-here").click()
        page.wait_for_function("document.getElementById('nas-here').disabled === true")
        assert len(held) == 1
        held[0].abort()
        page.wait_for_function("document.getElementById('nas-status').textContent === '没有导入'")
        assert page.locator("#nas-here").is_enabled()
        assert page.locator("#nas-list button", has_text="下一层").is_enabled()
        page.locator("#nas-list button", has_text="下一层").click()
        page.wait_for_selector("text=里面.md")
        page.locator("#nas-crumb button", has_text="个人空间").click()
        page.wait_for_selector("#nas-here")
        page.locator("#nas-here").click()
        page.wait_for_function("() => window.__posts === undefined || true")
        assert len(held) == 2
        page.keyboard.press("Escape")
        page.wait_for_function("document.activeElement.id === 'import-open'")
        _open_nas(page)
        page.wait_for_selector("#nas-here")
        page.locator("#nas-here").click()
        page.wait_for_function("document.getElementById('nas-here').disabled === true")
        assert len(held) == 3
        held[1].abort()
        page.wait_for_timeout(200)
        assert page.locator("#nas-here").is_disabled()
        held[2].fulfill(
            status=500,
            content_type="text/plain",
            body="not-json",
        )
        page.wait_for_function("document.getElementById('nas-status').textContent === '没有导入'")
        assert page.locator("#nas-here").is_enabled()
        page.unroute("**/admin/api/sources/import-nas")
        page.locator("#nas-here").click()
        page.wait_for_function("document.getElementById('nas-status').textContent !== '导入中'")
        assert any(item[0] == "dir" and item[1] == "" for item in gate.calls)
        context.close()
    finally:
        close()
