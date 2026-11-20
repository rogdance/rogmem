"""导入菜单和极空间目录窗的真实浏览器断言。合成目录，不连接极空间。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright

from rogmem.ui import sources_page

_ROWS = [
    {
        "id": "synth01ab",
        "kind": "file",
        "filename": "样例说明.md",
        "device_name": "合成设备",
        "agent_name": "极空间",
        "project_ref": "",
        "ingested_at": "2026-11-12T03:00:00+00:00",
        "pipeline": "解析:ok,草稿:ok,并页:ok,速写:ok",
        "status": "萃取已结晶",
        "note": "",
    },
    {
        "id": "synth02cd",
        "kind": "file",
        "filename": "抽不出正文的样例.txt",
        "device_name": "合成设备",
        "agent_name": "极空间",
        "project_ref": "",
        "ingested_at": "2026-11-12T02:00:00+00:00",
        "pipeline": "解析:fail",
        "status": "无正文",
        "note": "没有抽出正文",
    },
]


def _html() -> str:
    return sources_page(
        _ROWS,
        "file",
        {},
        {"devices": ["合成设备"], "agents": ["极空间"]},
        {"total": len(_ROWS), "page": 1, "page_size": 50},
    )


def _settings_html() -> str:
    from rogmem.ui import basic_page

    return basic_page(
        {
            "nas_path": "/nas",
            "scan_path": "/scan",
            "active_scan": "/scan",
            "file_types": "md,txt",
            "turn_wait": "600",
            "sketch_char_limit": "3500",
            "core_budget": "15000",
            "graph_auto_labels": "0",
            "graph_label_budget": "50",
            "store_path": "/data/rogmem.sqlite",
            "store_bytes": 0,
        }
    )


def _serve(html: str, routes: dict[str, str] | None = None) -> tuple[ThreadingHTTPServer, int]:
    pages = {"default": html}
    if routes:
        pages.update(routes)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            page_html = pages.get(path, pages["default"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page_html)))
            self.end_headers()
            self.wfile.write(page_html)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def _box(page, selector: str) -> dict:
    return page.locator(selector).bounding_box()


def _metrics(page) -> dict:
    return page.evaluate(
        """() => {
          const doc = document.documentElement;
          const btn = document.getElementById('import-open').getBoundingClientRect();
          const scroller = document.querySelector('.table-scroll');
          const panel = document.querySelector('.import-panel');
          const local = document.getElementById('import-local').getBoundingClientRect();
          const nas = document.getElementById('import-nas').getBoundingClientRect();
          const prect = panel ? panel.getBoundingClientRect() : null;
          return {
            inner: window.innerWidth,
            doc: [doc.scrollWidth, doc.clientWidth],
            btn: [btn.left, btn.right, btn.top, btn.bottom],
            table: [scroller.scrollWidth, scroller.clientWidth],
            panel: prect ? [prect.left, prect.right, prect.top, prect.bottom] : null,
            local: [local.left, local.top, local.width, local.height],
            nas: [nas.left, nas.top, nas.width, nas.height],
            btnBg: getComputedStyle(document.getElementById('import-open')).backgroundColor,
            itemBg: getComputedStyle(document.getElementById('import-local')).backgroundColor,
            fine: matchMedia('(hover: hover) and (pointer: fine)').matches,
            coarse: matchMedia('(pointer: coarse)').matches,
            hidden: document.getElementById('import-menu').hidden
          };
        }"""
    )


@pytest.fixture(scope="module")
def origin():
    server, port = _serve(_html(), {"/admin/settings/basic": _settings_html()})
    yield f"http://127.0.0.1:{port}/admin/sources?tab=file"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        br = pw.chromium.launch(
            channel="chrome",
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            yield br
        finally:
            br.close()


def _open(browser, origin, width, height, theme="dark", mobile=False):
    context = browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=mobile,
        color_scheme="dark" if theme == "dark" else "light",
    )
    page = context.new_page()
    page.add_init_script(
        f"localStorage.setItem('rogmem-theme', {json.dumps(theme)});"
    )
    if mobile:
        session = context.new_cdp_session(page)
        session.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": True},
        )
        session.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 1})
    page.goto(origin)
    page.wait_for_selector("#import-open")
    return context, page


def test_narrow_file_page_keeps_the_import_inside_the_viewport(browser, origin):
    context, page = _open(browser, origin, 390, 844)
    data = _metrics(page)
    assert data["inner"] == 390
    assert data["doc"][0] <= data["doc"][1] + 1, data
    assert 0 <= data["btn"][0] and data["btn"][1] <= 390 + 1, data
    assert data["table"][0] > data["table"][1], data
    context.close()


def test_menu_is_a_light_horizontal_pair_under_the_primary_button(browser, origin):
    context, page = _open(browser, origin, 1440, 900)
    page.click("#import-open")
    data = _metrics(page)
    assert data["hidden"] is False
    assert data["btnBg"] != data["itemBg"]
    assert data["itemBg"] in {"rgba(0, 0, 0, 0)", "transparent"}
    assert data["panel"][0] >= -1 and data["panel"][1] <= data["inner"] + 1
    assert abs(data["local"][1] - data["nas"][1]) <= 1
    assert data["nas"][0] >= data["local"][0] + data["local"][2] - 1
    assert data["local"][1] >= data["btn"][3] - 1
    assert data["local"][3] >= 44
    context.close()


def test_fine_pointer_hover_survives_the_gap_and_click_pins(browser, origin):
    context, page = _open(browser, origin, 1440, 900)
    button = _box(page, "#import-open")
    page.mouse.move(button["x"] + button["width"] / 2, button["y"] + button["height"] / 2)
    page.wait_for_function("document.getElementById('import-menu').hidden === false")
    assert page.evaluate("document.activeElement.id") != "import-local"
    item = _box(page, "#import-nas")
    page.mouse.move(item["x"] + 8, item["y"] + item["height"] / 2, steps=12)
    assert page.evaluate("document.getElementById('import-menu').hidden") is False
    page.mouse.move(20, 20)
    page.wait_for_timeout(80)
    assert page.evaluate("document.getElementById('import-menu').hidden") is False
    page.wait_for_function("document.getElementById('import-menu').hidden === true", timeout=1000)

    page.mouse.move(button["x"] + 8, button["y"] + 8)
    page.wait_for_function("document.getElementById('import-menu').hidden === false")
    page.mouse.click(button["x"] + 8, button["y"] + 8)
    page.mouse.move(20, 20)
    page.wait_for_timeout(350)
    assert page.evaluate("document.getElementById('import-menu').hidden") is False
    page.mouse.click(button["x"] + 8, button["y"] + 8)
    assert page.evaluate("document.getElementById('import-menu').hidden") is True
    context.close()


def test_pinning_a_preview_cancels_the_close_timer(browser, origin):
    context, page = _open(browser, origin, 1440, 900)
    button = _box(page, "#import-open")
    page.mouse.move(button["x"] + 8, button["y"] + 8)
    page.wait_for_function("importPhase === 'preview'")
    page.mouse.move(8, 8)
    page.wait_for_timeout(40)
    page.evaluate("document.getElementById('import-open').click()")
    page.wait_for_timeout(450)
    assert page.evaluate("importPhase") == "pinned"
    assert page.evaluate("document.getElementById('import-menu').hidden") is False
    context.close()


def test_keyboard_escape_and_outside_click_close_the_menu(browser, origin):
    context, page = _open(browser, origin, 1440, 900)
    page.focus("#import-open")
    page.keyboard.press("ArrowDown")
    assert page.evaluate("document.activeElement.id") == "import-local"
    page.keyboard.press("ArrowRight")
    assert page.evaluate("document.activeElement.id") == "import-nas"
    page.keyboard.press("Escape")
    assert page.evaluate("document.getElementById('import-menu').hidden") is True
    assert page.evaluate("document.activeElement.id") == "import-open"
    page.keyboard.press("Enter")
    assert page.evaluate("document.getElementById('import-menu').hidden") is False
    page.locator("h1").click()
    assert page.evaluate("document.getElementById('import-menu').hidden") is True
    context.close()


def test_coarse_pointer_does_not_open_on_hover(browser, origin):
    context, page = _open(browser, origin, 390, 844, mobile=True)
    data = _metrics(page)
    assert data["inner"] == 390, data
    assert data["coarse"] is True, data
    assert data["fine"] is False, data
    button = _box(page, "#import-open")
    page.mouse.move(button["x"] + 4, button["y"] + 4)
    page.wait_for_timeout(250)
    assert page.evaluate("document.getElementById('import-menu').hidden") is True
    page.touchscreen.tap(button["x"] + 4, button["y"] + 4)
    page.wait_for_function("document.getElementById('import-menu').hidden === false")
    panel = _metrics(page)["panel"]
    assert panel[0] >= -1 and panel[1] <= 391
    context.close()


def test_light_menu_items_stay_lighter_than_the_trigger(browser, origin):
    context, page = _open(browser, origin, 1440, 900, theme="light")
    page.click("#import-open")
    data = _metrics(page)
    assert data["btnBg"] != data["itemBg"]
    assert data["itemBg"] in {"rgba(0, 0, 0, 0)", "transparent"}
    context.close()


def _fulfill(route, payload, status=200):
    route.fulfill(
        status=status,
        content_type="application/json",
        body=json.dumps(payload, ensure_ascii=False),
    )


def test_nas_states_and_a_slow_listing_cannot_overwrite_a_newer_one(browser, origin):
    context, page = _open(browser, origin, 390, 844)
    held = {}

    def handle(route):
        if route.request.method == "POST":
            body = json.loads(route.request.post_data or "{}")
            if body.get("paths"):
                _fulfill(
                    route,
                    {
                        "ok": False,
                        "imported": [
                            {"ok": False, "filename": "样例说明.md", "error": "文件太大"},
                            {"ok": True, "filename": "另一份.md"},
                        ],
                    },
                )
            else:
                _fulfill(route, {"ok": False, "error": "这一层没有可导入的文件"})
            return
        path = parse_qs(urlparse(route.request.url).query).get("path", [""])[0]
        if path == "甲":
            held["route"] = route
            return
        if path == "乙":
            _fulfill(
                route,
                {
                    "ok": True,
                    "path": "乙",
                    "entries": [{"name": "乙文件.md", "kind": "file", "accepted": True}],
                    "offset": 0,
                    "limit": 200,
                    "more": False,
                    "truncated": False,
                },
            )
            return
        if path == "空":
            _fulfill(
                route,
                {
                    "ok": True,
                    "path": "空",
                    "entries": [],
                    "offset": 0,
                    "limit": 200,
                    "more": False,
                    "truncated": False,
                },
            )
            return
        _fulfill(
            route,
            {
                "ok": True,
                "path": "",
                "entries": [
                    {"name": "甲", "kind": "dir"},
                    {"name": "乙", "kind": "dir"},
                    {"name": "空", "kind": "dir"},
                    {"name": "样例说明.md", "kind": "file", "accepted": True},
                    {"name": "封面.png", "kind": "file", "accepted": False},
                ],
                "offset": 0,
                "limit": 200,
                "more": False,
                "truncated": False,
            },
        )

    def missing(route):
        _fulfill(route, {"ok": False, "error": "极空间目录还没挂进容器"})

    page.route("**/admin/api/sources/nas**", missing)
    page.click("#import-open")
    page.click("#import-nas")
    page.wait_for_function(
        "document.getElementById('nas-status').textContent === '个人空间还没挂上。在基础设置里填写。'"
    )
    assert page.locator("#nas-status a").get_attribute("href") == "/admin/settings/basic"
    assert page.locator("#nas-title").inner_text() == "从极空间导入"
    assert page.locator("#nas-status").evaluate("node => node.classList.contains('empty')")
    quiet = _box(page, "#nas-import")
    assert 120 < quiet["height"] < 480, quiet
    assert not page.locator("#nas-import").evaluate("node => node.classList.contains('nas-browse')")
    dialog_text = page.locator("#nas-import").inner_text()
    assert "挂进容器" not in dialog_text
    assert page.locator("#nas-up").is_hidden()
    assert page.locator("#nas-here").is_hidden()
    assert page.locator("#nas-retry").is_hidden()
    assert page.locator("#nas-close").is_visible()
    page.keyboard.press("Escape")
    assert page.evaluate("document.getElementById('nas-import').open") is False
    page.wait_for_function("document.activeElement.id === 'import-open'")
    page.click("#import-open")
    page.click("#import-nas")
    page.wait_for_function(
        "document.getElementById('nas-status').textContent === '个人空间还没挂上。在基础设置里填写。'"
    )
    page.locator("#nas-status a").click()
    page.wait_for_selector("input[name=nas_path]")
    assert "/admin/settings/basic" in page.url
    assert page.locator("input[name=nas_path]").input_value() == "/nas"
    page.goto(origin)
    page.wait_for_selector("#import-open")
    page.unroute("**/admin/api/sources/nas**")

    page.route("**/admin/api/sources/**", handle)
    page.click("#import-open")
    page.click("#import-nas")
    page.wait_for_selector("#nas-status")
    page.wait_for_function("document.getElementById('nas-status').textContent === ''")
    assert page.locator("#nas-selected").is_hidden()
    assert page.locator("#nas-here").is_visible()
    assert "不含子目录，一次最多 50 个" in page.locator("#nas-scope").inner_text()
    assert "只列出前 5000 项" not in page.locator("#nas-scope").inner_text()
    dialog = _box(page, "#nas-import")
    assert dialog["width"] >= 360
    assert dialog["height"] >= 700
    page.locator("#nas-list input").check()
    assert page.locator("#nas-selected").is_visible()
    page.locator("#nas-selected").click()
    page.wait_for_function("document.getElementById('nas-result').textContent.includes('文件太大')")
    assert page.locator("#nas-import").evaluate("node => node.open") is True

    page.locator("#nas-list button", has_text="甲").click()
    page.wait_for_function("document.getElementById('nas-status').textContent === '列出中'")
    assert "暂无文件" not in page.locator("#nas-status").inner_text()
    page.locator("#nas-list button", has_text="乙").click()
    page.wait_for_selector("text=乙文件.md")
    held["route"].fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(
            {
                "ok": True,
                "path": "甲",
                "entries": [{"name": "甲文件.md", "kind": "file", "accepted": True}],
                "offset": 0,
                "limit": 200,
                "more": False,
                "truncated": False,
            },
            ensure_ascii=False,
        ),
    )
    page.wait_for_timeout(150)
    assert "乙文件.md" in page.locator("#nas-list").inner_text()
    assert "甲文件.md" not in page.locator("#nas-list").inner_text()

    page.locator("#nas-crumb button", has_text="个人空间").click()
    page.locator("#nas-list button", has_text="空").click()
    page.wait_for_function("document.getElementById('nas-status').textContent === '暂无文件'")
    assert page.locator("#nas-here").is_hidden()
    assert page.locator("#nas-selected").is_hidden()

    context.close()


def test_basic_settings_mount_field_matches_the_page(browser, origin):
    base = origin.split("/admin/sources", 1)[0]
    for width, height in ((1440, 900), (390, 844)):
        context = browser.new_context(
            viewport={"width": width, "height": height},
            color_scheme="dark",
        )
        page = context.new_page()
        page.goto(base + "/admin/settings/basic")
        page.wait_for_selector("input[name=nas_path]")
        plate = page.locator("form.settings-form", has=page.locator("input[name=nas_path]"))
        assert plate.locator("h2").inner_text() == "个人空间"
        assert plate.locator(".setting-name").inner_text() == "挂载路径"
        assert plate.locator(".note").inner_text() == "知识导入从这里读。"
        field = plate.locator("input[name=nas_path]")
        assert field.input_value() == "/nas"
        assert field.get_attribute("aria-label") == "个人空间挂载路径"
        save = plate.locator("button[type=submit]")
        assert save.is_disabled()
        field.fill("/mnt/personal")
        assert save.is_enabled()
        field.fill("/nas")
        assert save.is_disabled()
        assert page.locator("input[name=scan_path]").input_value() == "/scan"
        assert plate.locator("input[name=scan_path]").count() == 0
        fits = page.evaluate(
            "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
        )
        assert fits
        context.close()
