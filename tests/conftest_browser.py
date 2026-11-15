"""共享 Playwright 浏览器会话。

两个 chart 浏览器测试文件都靠这里的 session-scope browser，避免在同一 pytest 会话里
`sync_playwright()` 被开两次（第二次会抛 "Sync API inside the asyncio loop"）。
"""

from __future__ import annotations

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402


@pytest.fixture(scope="session")
def _playwright_ctx():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(_playwright_ctx):
    br = _playwright_ctx.chromium.launch(headless=True)
    try:
        yield br
    finally:
        br.close()
