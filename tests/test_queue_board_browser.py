"""队列状态页：同一份合成种子上，浏览器看到的数字和接口、库一致。"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from datetime import timedelta  # noqa: E402

from tests.test_queue_read_model import LIVE, NOW, _insert, _row, open_engine, seed_screenshot  # noqa: E402

ARTIFACTS = Path("/opt/cursor/artifacts")


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _Server:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.engine = open_engine(tmp_path, monkeypatch)
        self.seeded = seed_screenshot(self.engine)
        from rogmem.app import build_app
        import uvicorn

        self.port = _free_port()
        config = uvicorn.Config(
            build_app(),
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            lifespan="on",
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            self.thread.join(0.05)
        assert self.server.started, "队列页没有起来"

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=3)


def _queued_figure(page) -> str:
    card = page.locator("#queue-overview .ov-figures > div").filter(has_text="排队中")
    return card.locator(".money").inner_text().strip()


def _tab_text(page, name: str) -> str:
    return page.locator("#queue-tasks .tabs a", has_text=name).inner_text().strip()


def test_desktop_and_phone_show_the_same_live_line(tmp_path, monkeypatch, browser):
    server = _Server(tmp_path, monkeypatch)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    context = browser.new_context()
    page = context.new_page()
    try:
        url = f"{server.base}/admin/settings/queue?span=24h&tab=queued"
        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto(url, wait_until="domcontentloaded")
        page.locator("#queue-overview").wait_for()
        desktop = _queued_figure(page)
        page.screenshot(path=str(ARTIFACTS / "queue-status-desktop.png"), full_page=True)
        assert desktop == f"{LIVE} 项"
        assert _tab_text(page, "排队中") == f"排队中 {LIVE}"
        assert page.locator("#queue-tasks .list-foot").get_attribute("data-total") == str(LIVE)
        page.get_by_role("link", name="近 7 天").click()
        page.locator("#queue-overview").wait_for()
        assert _queued_figure(page) == f"{LIVE} 项"
        assert page.locator("#queue-overview .ov-figures > div").filter(has_text="已完成").locator(".money").inner_text().strip() == "2 项"

        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(url, wait_until="domcontentloaded")
        page.locator("#queue-overview").wait_for()
        phone = _queued_figure(page)
        box = page.locator("#queue-overview .ov-figures > div").filter(has_text="排队中").locator(".money").bounding_box()
        page.screenshot(path=str(ARTIFACTS / "queue-status-390.png"), full_page=True)
        assert phone == f"{LIVE} 项"
        assert box is not None and box["width"] > 40
        assert box["x"] >= 0 and box["x"] + box["width"] <= 390 + 1
        overflow = page.evaluate(
            """() => {
              const root = document.documentElement;
              const scroller = document.querySelector('#queue-tasks .table-scroll');
              return {
                page: root.scrollWidth - root.clientWidth,
                table: scroller.scrollWidth - scroller.clientWidth
              };
            }"""
        )
        report = page.evaluate(
            """() => {
              const box = (sel) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return {w: Math.round(r.width), right: Math.round(r.right), sw: el.scrollWidth};
              };
              return {
                cls: document.documentElement.className,
                client: document.documentElement.clientWidth,
                header: box('header'),
                brand: box('header .brand svg'),
                nav: box('header nav'),
                filters: box('.ov-filters'),
                figures: box('.ov-figures'),
                money: [...document.querySelectorAll('.ov-figures .money')].map((el) => Math.round(el.getBoundingClientRect().right)),
                table: box('#queue-tasks .table-scroll'),
                pager: box('.list-foot')
              };
            }"""
        )
        assert overflow["page"] <= 1, report

        dated = (NOW - timedelta(hours=2)).isoformat()
        _insert(
            server.engine,
            [
                _row("dated-visible", state="done", when=dated, body="甲乙丙"),
                _row("blank-visible", state="done", when=dated, body="丁戊", finished_at=""),
            ],
        )
        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto(f"{url.split('?')[0]}?span=all&tab=done", wait_until="domcontentloaded")
        page.locator("#queue-overview").wait_for()
        done = page.locator("#queue-overview .ov-figures > div").filter(has_text="已完成").locator(".money")
        caption = page.locator(".ov-caption")
        page.screenshot(path=str(ARTIFACTS / "queue-untimed-desktop.png"), full_page=True)
        assert done.inner_text().strip() == "5 项"
        assert "未标时间 1 项" in caption.inner_text()
    finally:
        context.close()
        server.close()
