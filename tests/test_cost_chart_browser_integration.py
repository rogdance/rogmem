"""真浏览器 + 真实 cost.overview 端到端验收。

与 test_cost_chart_browser.py 的区别：
- 不手工造整点 series；直接通过 `engine.connect` + `cost.record` 落库 + `cost.overview(now=NOW)` 得 board。
- tooltip 首尾裁剪断言的是真实 window.start/end（含微秒）。
- 主题通过 `goto('about:blank') + evaluate(localStorage.setItem) + set_content` 时序保证生效；
  断言 `getComputedStyle(documentElement).backgroundColor` 与 bar 的 fill 都真实匹配。
- tick 防碰撞测得的是 `getBBox()` 的真实文字宽，不是 CHAR_PX 常量近似。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from rogmem import cost  # noqa: E402
from rogmem.ui import cost_page  # noqa: E402


NOW = datetime(2026, 11, 11, 3, 37, 42, 123_000, tzinfo=timezone.utc)
# UTC+8 版本：2026-11-11 11:37:42.123


class _StaticHTML:
    """Tiny single-page HTTP server. Serves whatever HTML was set last.

    Needed so Playwright has a real origin (http://127.0.0.1:port) where localStorage is writable;
    about:blank would deny set_content-based localStorage writes, which previously let a bad
    theme-init pass acceptance in disguise.
    """

    def __init__(self):
        self.html = "<html><body></body></html>"
        self.server = HTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = outer.html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args, **kwargs):
                pass

        return H

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(scope="session")
def html_server():
    srv = _StaticHTML()
    try:
        yield srv
    finally:
        srv.close()


# Shared `browser` fixture comes from tests/conftest_browser.py via conftest.pytest_plugins.


@pytest.fixture
def engine_with_data(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine as eng

    eng.connect(tmp_path, tmp_path / "scan")
    # Seed 真实、非整点、包含未计价与跨日的调用。
    moments = [
        ("2026-11-10T04:15:33+00:00", "paid", (1234, 456, 20)),
        ("2026-11-10T09:50:11+00:00", "paid", (5000, 2000, 100)),
        ("2026-11-10T22:50:11+00:00", "paid", (3200, 1100, 50)),
        ("2026-11-10T22:50:12+00:00", "unpaid", (900, 300, 0)),
        ("2026-11-11T01:02:00+00:00", "paid", (4800, 1900, 80)),
        ("2026-11-11T03:35:00+00:00", "paid", (2600, 1000, 40)),
    ]
    for at, model, usage in moments:
        cost.record(at=at, worker="初始蒸馏", provider_id="p", model=model, usage=usage, prompt="材料", answer="回复")
    cost.save_book(
        "7.2",
        [{"provider": "p", "model": "paid", "currency": "USD", "input": "2", "output": "3", "cache": "0.5"}],
    )
    return eng


def _open(page, server, html, *, viewport, theme):
    """Serve HTML on a real http origin so localStorage is writable and the inline theme bootstrap
    actually picks up our chosen theme before first paint."""

    server.html = html
    page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
    # First visit: bare page, set localStorage, then re-navigate so the inline <script>
    # at document parse reads the stored theme.
    page.goto(server.url)
    page.evaluate(f"localStorage.setItem('rogmem-theme', {json.dumps(theme)});")
    page.goto(server.url, wait_until="load")
    page.wait_for_function(
        "window.__COST_CHART__ && typeof window.__COST_CHART__.snapshot === 'function'"
    )
    page.evaluate("window.__COST_CHART__.render()")


# -----------------------------------------------------------------------
# 真实 window: tooltip 首/末 clip
# -----------------------------------------------------------------------


def test_first_bucket_tooltip_clips_to_window_start_microseconds(browser, html_server, engine_with_data):
    board = cost.overview(now=NOW, span="24h")
    html = cost_page(board, [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html_server, html, viewport=(1280, 800), theme="dark")
    page.focus("#ov-svg")
    page.keyboard.press("Home")
    page.wait_for_timeout(50)
    content = page.inner_text("#ov-tip")
    # window.start = (NOW - 24h) 在 UTC+8 下 = 11:37:42.123 — tooltip 精确到分钟 11:37
    assert "11:37" in content, f"首桶 tooltip 必须含 window.start (UTC+8 11:37)：{content}"
    # 严禁出现 11:00、00:00 这种"整小时"伪区间开头（这是上一版 bug）
    assert "– 11:00" not in content, f"首桶 tooltip 不得伪造整点结束：{content}"
    ctx.close()


def test_last_bucket_tooltip_clips_to_window_end_microseconds(browser, html_server, engine_with_data):
    board = cost.overview(now=NOW, span="24h")
    html = cost_page(board, [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html_server, html, viewport=(1280, 800), theme="dark")
    page.focus("#ov-svg")
    page.keyboard.press("End")
    page.wait_for_timeout(50)
    content = page.inner_text("#ov-tip")
    # window.end = NOW 在 UTC+8 下 = 11:37:42.123；tooltip 以 11:37 结束，不能是 23:59 也不能是未来 12:00
    assert "11:37" in content, f"末桶 tooltip 必须含 window.end (UTC+8 11:37)：{content}"
    assert "– 23:59" not in content, "末桶绝不能伪造 00:00–23:59 整天区间"
    assert "12:00" not in content, "末桶不得标到 now 之后（未来时刻）"
    ctx.close()


# -----------------------------------------------------------------------
# 主题：computed backgroundColor + bar fill 双重真实断言
# -----------------------------------------------------------------------


def _parse_rgb(s: str) -> tuple:
    m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", s)
    assert m, f"无法解析 rgb: {s}"
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


@pytest.mark.parametrize(
    "theme, expected_bg_rgb",
    [
        ("dark", (33, 33, 33)),   # #212121 = var(--surface-base) 深色
        ("light", (255, 255, 255)),  # #fff 浅色
    ],
)
def test_theme_background_and_bar_fill_match_spec(browser, html_server, engine_with_data, theme, expected_bg_rgb):
    board = cost.overview(now=NOW, span="24h")
    html = cost_page(board, [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html_server, html, viewport=(1280, 800), theme=theme)
    data_theme = page.evaluate("document.documentElement.getAttribute('data-theme')")
    panel_bg = page.evaluate("getComputedStyle(document.querySelector('#cost')).backgroundColor")
    bar_fill = page.evaluate("getComputedStyle(document.querySelector('rect.bar')).fill")
    grid_stroke = page.evaluate("getComputedStyle(document.querySelector('line.grid')).stroke")
    axis_fill = page.evaluate("getComputedStyle(document.querySelector('text.axis')).fill")

    actual_theme = "dark" if data_theme == "dark" else "light"
    assert actual_theme == theme, f"初始化时序 bug：期望 {theme}，实际 data-theme={data_theme}"

    assert _parse_rgb(panel_bg) == expected_bg_rgb, (
        f"{theme} 主题下成本面板背景必须 == {expected_bg_rgb}, 实际 {panel_bg}"
    )
    assert _parse_rgb(bar_fill) == (255, 55, 99), f"{theme} 柱色必须 #ff3763, 实际 {bar_fill}"
    expected_grid = (61, 61, 61) if theme == "dark" else (228, 230, 232)
    assert _parse_rgb(grid_stroke) == expected_grid, f"{theme} 网格线色错：期望 {expected_grid} 实际 {grid_stroke}"
    expected_axis = (173, 173, 173) if theme == "dark" else (92, 96, 102)
    assert _parse_rgb(axis_fill) == expected_axis, f"{theme} 轴文字色错：期望 {expected_axis} 实际 {axis_fill}"
    ctx.close()


# -----------------------------------------------------------------------
# tick 防碰撞：用 getBBox 真实宽度，三 viewport + y 轴长金额数不撞绘图区
# -----------------------------------------------------------------------


@pytest.mark.parametrize("width", [360, 820, 1280])
def test_axis_tick_real_bbox_no_overlap(browser, html_server, engine_with_data, width):
    board = cost.overview(now=NOW, span="24h")
    html = cost_page(board, [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html_server, html, viewport=(width, 800), theme="dark")
    rects = page.evaluate(
        """() => {
            const nodes = Array.from(document.querySelectorAll('text.axis'))
                // x-axis labels are text-anchor="middle"; y-axis labels use "end".
                .filter(t => t.getAttribute('text-anchor') === 'middle');
            return nodes.map(t => {
                const bb = t.getBBox();
                return {x: bb.x, right: bb.x + bb.width, width: bb.width, text: t.textContent};
            }).sort((a,b) => a.x - b.x);
        }"""
    )
    for a, b in zip(rects, rects[1:]):
        assert a["right"] <= b["x"] + 0.5, (
            f"viewport={width}，真实 bbox 下 tick 重叠：{a} 与 {b}"
        )
    # y 轴文字不得跨过绘图左边界（即不裁进 bar 区）
    svg_metrics = page.evaluate(
        """() => {
            const svg = document.getElementById('ov-svg');
            const grid = svg.querySelector('line.grid');
            const cap = document.getElementById('ov-caption');
            if (!grid) return {degraded: cap ? cap.textContent : '', labels: []};
            const yNodes = Array.from(svg.querySelectorAll('text.axis')).filter(t => t.getAttribute('text-anchor') === 'end');
            const left = parseFloat(grid.getAttribute('x1'));
            return {degraded: '', left, labels: yNodes.map(t => {
                const bb = t.getBBox();
                return {x: bb.x, right: bb.x + bb.width, text: t.textContent};
            })};
        }"""
    )
    if svg_metrics.get("degraded"):
        # 真·过窄 → 必须 degrade 到 caption，不得偷偷留下 label 侵入绘图区
        assert "过窄" in svg_metrics["degraded"] or "无数据" in svg_metrics["degraded"], (
            f"viewport={width} 下无 grid 但 caption 没说明：{svg_metrics['degraded']}"
        )
        return
    for lab in svg_metrics["labels"]:
        assert lab["right"] <= svg_metrics["left"], (
            f"viewport={width} 下 y 轴 label {lab} 侵入绘图区 (chartLeft={svg_metrics['left']})"
        )
    ctx.close()


# -----------------------------------------------------------------------
# 金额精度：小 step、大数、全零；token 小值准确计数
# -----------------------------------------------------------------------


def test_money_axis_stays_in_millions_when_amounts_are_tiny(browser, html_server, engine_with_data, tmp_path, monkeypatch):
    # 极小金额不再把个位或科学计数写到轴上，只留百万。
    cost.save_book(
        "1",
        [{"provider": "p", "model": "paid", "currency": "CNY", "input": "0.0001", "output": "0.0001", "cache": "0"}],
    )
    board = cost.overview(now=NOW, span="24h")
    html = cost_page(board, [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html_server, html, viewport=(1280, 800), theme="dark")
    labels = page.evaluate(
        """() => Array.from(document.querySelectorAll('text.axis'))
            .filter(t => t.getAttribute('text-anchor') === 'end')
            .map(t => t.textContent)"""
    )
    assert labels, "金额轴必须有刻度"
    for lab in labels:
        assert re.fullmatch(r"¥\d[\d,]*\.\d{2} 百万", lab), f"极小金额也只用百万：{labels}"
        assert "e" not in lab.lower()
    ctx.close()


def test_token_tooltip_uses_millions_not_exact_count(browser, html_server, engine_with_data):
    # 几百到几千 token 在柱上写成百万；精确的 1,234 留在明细表。
    board = cost.overview(now=NOW, span="24h")
    html = cost_page(board, [], {})
    assert "1,234" in html
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html_server, html, viewport=(1280, 800), theme="dark")
    page.click("#ov-unit button[data-unit='token']")
    page.focus("#ov-svg")
    index = page.evaluate(
        """() => {
            const buckets = window.__COST_CHART__.snapshot().displayBuckets;
            return buckets.findIndex(b => (b.input || 0) === 1234 || (b.tokens || 0) >= 1234);
        }"""
    )
    assert index >= 0, "种子调用应当落在某个展示桶里"
    page.keyboard.press("Home")
    for _ in range(index):
        page.keyboard.press("ArrowRight")
    page.wait_for_timeout(50)
    content = page.inner_text("#ov-tip")
    assert re.search(r"\d+\.\d{2} 百万", content), f"tooltip 必须用百万：{content}"
    assert "1,234" not in content and "1234" not in content, content
    assert "1,234" in page.inner_text("table.calls")
    ctx.close()


# -----------------------------------------------------------------------
# 合并桶与 _public_sum 对齐（真实未计价混合数据）
# -----------------------------------------------------------------------


def test_merge_matches_public_sum_with_mixed_unpriced(browser, html_server, engine_with_data):
    # 让 n 很大以触发合并；复用 seed 的 unpriced 调用
    # 追加若干合成记录以让 series 长度 > maxBars
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(400):
        cost.record(
            at=(base + timedelta(days=i)).isoformat(),
            worker="初始蒸馏",
            provider_id="p",
            model="paid" if i % 5 else "unpaid",
            usage=(10 + i, 5, 0),
            prompt="x",
            answer="x",
        )
    board = cost.overview(now=NOW, span="all")
    html = cost_page(board, [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html_server, html, viewport=(640, 800), theme="dark")
    snap = page.evaluate(
        """() => {
            const s = window.__COST_CHART__.snapshot();
            return {
                displayTokens: s.displayBuckets.reduce((a,b)=>a+b.tokens,0),
                displayUnpriced: s.displayBuckets.reduce((a,b)=>a+b.unpriced,0),
                displayBare: s.displayBuckets.reduce((a,b)=>a+b.bare,0),
                anyNull: s.displayBuckets.some(b => b.money == null),
                partiallyKnown: s.displayBuckets.filter(b => b.money == null && b.moneyKnown > 0).length,
                count: s.displayBuckets.length
            };
        }"""
    )
    series_tokens = sum(item["tokens"] for item in board["series"])
    series_unpriced = sum(item.get("unpriced") or 0 for item in board["series"])
    series_bare = sum(item.get("bare") or 0 for item in board["series"])
    assert snap["displayTokens"] == series_tokens, "合并后 tokens 必须守恒"
    assert snap["displayUnpriced"] == series_unpriced, "合并后 unpriced 必须守恒（_public_sum 口径依赖）"
    assert snap["displayBare"] == series_bare, "合并后 bare 必须守恒"
    assert snap["anyNull"], "含未计价调用 → 合并桶 money 应为 null（与 summary.money=None 一致）"
    assert snap["partiallyKnown"] > 0, (
        "合并块同时含已计价+未计价时，moneyKnown>0 必须保留，以便 tooltip 说明已计价部分而不是丢数据"
    )
    # 和 summary 卡片一致
    assert board["summary"]["tokens"] == snap["displayTokens"]
    assert board["summary"]["money"] is None
    ctx.close()


# -----------------------------------------------------------------------
# resize 真实缩放 + 字体放大后不重叠不裁切
# -----------------------------------------------------------------------


def test_mixed_single_bucket_tooltip_shows_known_money(browser, html_server, engine_with_data):
    """单桶混合（n<maxBars 不触发合并）：tooltip 不得丢已计价部分金额。"""

    # Overwrite：同小时内一条 paid + 一条 unpaid
    cost.record(at="2026-11-11T03:10:00+00:00", worker="初始蒸馏", provider_id="p", model="paid", usage=(1_000_000, 500_000, 0), prompt="已计价", answer="")
    cost.record(at="2026-11-11T03:20:00+00:00", worker="初始蒸馏", provider_id="p", model="unpaid", usage=(100_000, 50_000, 0), prompt="未计价", answer="")
    board = cost.overview(now=NOW, span="24h")
    html = cost_page(board, [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html_server, html, viewport=(1280, 800), theme="dark")
    page.focus("#ov-svg")
    page.keyboard.press("End")  # 最后一个桶 = 11:00 UTC+8 当前时刻桶
    page.wait_for_timeout(50)
    content = page.inner_text("#ov-tip")
    assert "金额未完整计入" in content, (
        "混合桶 tip 必须说明 '未完整计入'，而不是 '未计入'：" + content
    )
    assert "已计价部分" in content and "¥" in content, (
        "混合桶 tip 必须带出已计价金额：" + content
    )
    ctx.close()


def test_render_reset_after_unit_switch_and_resize_clears_stale_tooltip(browser, html_server, engine_with_data):
    """render 重建时必须一致：切 unit 后旧 tip 不得残留。"""

    board = cost.overview(now=NOW, span="24h")
    html = cost_page(board, [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html_server, html, viewport=(1280, 800), theme="dark")
    # 聚焦 + 弹 tip
    page.focus("#ov-svg")
    page.keyboard.press("Home")
    page.wait_for_timeout(50)
    before = page.evaluate("document.getElementById('ov-tip').hidden")
    assert before is False
    # 切换 unit → render 重建
    page.click("#ov-unit button[data-unit='token']")
    page.wait_for_timeout(100)
    after = page.evaluate("({hidden: document.getElementById('ov-tip').hidden, cursor: document.querySelector('rect.cursor'), active: document.getElementById('ov-svg').getAttribute('aria-activedescendant')})")
    assert after["hidden"] is True, "切 unit 后 tip 必须被清除，避免显示旧桶的 money/tokens"
    assert after["cursor"] is None, "切 unit 后 cursor 高亮矩形必须被清除"
    assert after["active"] is None, "切 unit 后 aria-activedescendant 必须被清除"
    ctx.close()


def test_axis_font_scales_with_root_font_size(browser, html_server, engine_with_data):
    """axis 字号用相对单位；html{font-size} 真实影响 computed font-size，并影响 tick dodge。"""

    board = cost.overview(now=NOW, span="24h")
    html = cost_page(board, [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html_server, html, viewport=(1280, 800), theme="dark")
    before = page.evaluate("parseFloat(getComputedStyle(document.querySelector('text.axis')).fontSize)")
    page.evaluate("document.documentElement.style.fontSize = '24px'")
    page.evaluate("window.__COST_CHART__.render()")  # 强制重测 bbox
    page.wait_for_timeout(50)
    after = page.evaluate("parseFloat(getComputedStyle(document.querySelector('text.axis')).fontSize)")
    assert after > before + 2, f"axis 字号应随 html.font-size 变化：before={before} after={after}"


def test_all_tick_bbox_no_overlap_including_first_last_anchors(browser, html_server, engine_with_data):
    """包含 start / end anchor 的首末 tick 也要用真实 bbox 不重叠。"""

    board = cost.overview(now=NOW, span="24h")
    html = cost_page(board, [], {})
    for width in (820, 1280):
        ctx = browser.new_context()
        page = ctx.new_page()
        _open(page, html_server, html, viewport=(width, 800), theme="dark")
        rects = page.evaluate(
            """() => Array.from(document.querySelectorAll('text.axis'))
                // 关键：不再按 text-anchor 过滤——首末 anchor 被 clamp 到 start/end 时正是最危险位置
                .filter(t => t.parentNode.tagName.toLowerCase() === 'svg')
                .map(t => {
                    const bb = t.getBoundingClientRect();
                    return {x: bb.left, right: bb.right, width: bb.width, text: t.textContent, anchor: t.getAttribute('text-anchor'), y: bb.top};
                })"""
        )
        # 按行分组（y ≈ 相同的放一起），每组内按 x 排序断言不重叠
        rows = {}
        for r in rects:
            key = round(r["y"])
            rows.setdefault(key, []).append(r)
        for y, items in rows.items():
            items.sort(key=lambda r: r["x"])
            for a, b in zip(items, items[1:]):
                assert a["right"] <= b["x"] + 0.5, (
                    f"viewport={width} y={y} 行内 tick 重叠：{a} vs {b}"
                )
        ctx.close()


def test_large_amount_y_axis_labels_all_fit_real_bbox(browser, html_server, engine_with_data):
    """大额金额的 y 轴按真实文字宽度留白，刻度写成百万，不写个位金额。"""

    # Seed 极大金额：汇率 10，paid 单价 200 美元/M token，一个桶 ~1000M tokens
    cost.save_book(
        "10",
        [{"provider": "p", "model": "paid", "currency": "USD", "input": "200", "output": "300", "cache": "100"}],
    )
    for i in range(5):
        cost.record(
            at=f"2026-11-11T0{i}:30:00+00:00",
            worker="初始蒸馏", provider_id="p", model="paid",
            usage=(500_000_000 * (i + 1), 200_000_000 * (i + 1), 10_000_000),
            prompt="大", answer="大"
        )
    board = cost.overview(now=NOW, span="24h")
    html = cost_page(board, [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html_server, html, viewport=(1280, 800), theme="dark")
    info = page.evaluate(
        """() => {
            const svg = document.getElementById('ov-svg');
            const grid = svg.querySelector('line.grid');
            if (!grid) return {degraded: true, caption: document.getElementById('ov-caption').textContent};
            const chartLeft = parseFloat(grid.getAttribute('x1'));
            const yLabels = Array.from(svg.querySelectorAll('text.axis'))
                .filter(t => t.getAttribute('text-anchor') === 'end')
                .map(t => ({text: t.textContent, right: t.getBoundingClientRect().right, bbox: t.getBBox()}));
            const svgLeft = svg.getBoundingClientRect().left;
            return {chartLeft, svgLeft, yLabels};
        }"""
    )
    assert not info.get("degraded"), f"大额场景不应 degrade: {info}"
    assert info["yLabels"], "y 轴至少应有一条 tick"
    for lab in info["yLabels"]:
        assert lab["right"] <= info["svgLeft"] + info["chartLeft"] + 0.5, (
            f"真·大额 y label 越过绘图区左界：{lab} chartLeft={info['chartLeft']}"
        )
    labels = [l["text"] for l in info["yLabels"]]
    assert any(re.fullmatch(r"¥\d[\d,]*\.\d{2} 百万", lab) and lab != "¥0.00 百万" for lab in labels), (
        f"大额轴应出现非零的百万刻度：{labels}"
    )
    for lab in labels:
        assert re.fullmatch(r"¥\d[\d,]*\.\d{2} 百万", lab), lab
        assert "500,000,000" not in lab and "e" not in lab.lower()
    ctx.close()


def test_tiny_amount_axis_uses_millions_not_scientific(browser, html_server, engine_with_data, tmp_path, monkeypatch):
    """低于百万分辨率的金额不再写成科学计数或个位小数。精确用量留在明细。"""

    # 汇率 1，单价 0.0000001/M；一个桶约 10 token。金额远小于 0.01 百万。
    cost.save_book("1", [{"provider": "p", "model": "paid", "currency": "CNY", "input": "0.0000001", "output": "0", "cache": "0"}])
    for i in range(3):
        cost.record(
            at=f"2026-11-11T0{i}:30:00+00:00",
            worker="初始蒸馏", provider_id="p", model="paid",
            usage=(10 + i, 0, 0), prompt="微粒", answer="t"
        )
    board = cost.overview(now=NOW, span="24h")
    html = cost_page(board, [], {})
    assert "微粒" in html
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html_server, html, viewport=(1280, 800), theme="dark")
    labels = page.evaluate(
        """() => Array.from(document.getElementById('ov-svg').querySelectorAll('text.axis'))
            .filter(t => t.getAttribute('text-anchor') === 'end')
            .map(t => t.textContent)"""
    )
    assert labels, f"金额轴必须有刻度：{labels}"
    for lab in labels:
        assert re.fullmatch(r"¥\d[\d,]*\.\d{2} 百万", lab), lab
        assert "e" not in lab.lower()
    table = page.inner_text("table.calls")
    assert "微粒" in table
    ctx.close()


@pytest.mark.parametrize("width", [360, 820, 1280])
@pytest.mark.parametrize("span", ["24h", "all"])
def test_all_x_tick_anchors_no_overlap_at_three_widths(browser, html_server, engine_with_data, width, span):
    """R4：360/820/1280 三 viewport × 24h/all 两 span，不按 anchor 过滤——含 start/end clamp 后的首末。
    若 360 因 degrade 无法绘制，必须显式 caption 说明，而不是跳过。"""

    board = cost.overview(now=NOW, span=span)
    html = cost_page(board, [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html_server, html, viewport=(width, 800), theme="dark")
    info = page.evaluate(
        """() => {
            const svg = document.getElementById('ov-svg');
            const cap = document.getElementById('ov-caption').textContent;
            const bars = svg.querySelectorAll('rect.bar').length;
            if (bars === 0) return {degraded: true, cap};
            const ticks = Array.from(svg.querySelectorAll('text.axis'))
                .map(t => ({
                    x: t.getBoundingClientRect().left,
                    right: t.getBoundingClientRect().right,
                    y: Math.round(t.getBoundingClientRect().top),
                    anchor: t.getAttribute('text-anchor'),
                    text: t.textContent
                }));
            return {degraded: false, ticks};
        }"""
    )
    if info.get("degraded"):
        assert "过窄" in info["cap"] or "无数据" in info["cap"] or "缺失" in info["cap"], (
            f"width={width} span={span} 没画出图但 caption 没说明：{info['cap']}"
        )
        ctx.close()
        return
    rows = {}
    for t in info["ticks"]:
        rows.setdefault(t["y"], []).append(t)
    for y, items in rows.items():
        items.sort(key=lambda r: r["x"])
        for a, b in zip(items, items[1:]):
            assert a["right"] <= b["x"] + 0.5, (
                f"width={width} span={span} y={y} 相邻 tick 真实 bbox 重叠：{a} vs {b}"
            )
    ctx.close()


def test_all_children_mixed_forced_merge_conserves_bare_and_moneyKnown(browser, html_server, engine_with_data):
    """R4：合并块的每一个子桶都是 mixed（paid+unpaid+bare），合并后 moneyKnown / bare / unpriced 全部守恒。"""

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(300):
        at_paid = (base + timedelta(days=i, hours=1)).isoformat()
        at_unpaid = (base + timedelta(days=i, hours=2)).isoformat()
        at_bare = (base + timedelta(days=i, hours=3)).isoformat()
        cost.record(at=at_paid, worker="w", provider_id="p", model="paid", usage=(1000, 200, 0), prompt="p", answer="p")
        cost.record(at=at_unpaid, worker="w", provider_id="p", model="unpaid", usage=(500, 100, 0), prompt="u", answer="u")
        cost.record(at=at_bare, worker="w", provider_id="p", model="paid", usage=None, prompt="b", answer="b")
    cost.save_book("1", [{"provider": "p", "model": "paid", "currency": "CNY", "input": "1", "output": "1", "cache": "0"}])
    board = cost.overview(now=NOW, span="all")
    html = cost_page(board, [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html_server, html, viewport=(560, 800), theme="dark")  # 窄到强制 merge
    snap = page.evaluate(
        """() => {
            const s = window.__COST_CHART__.snapshot();
            return {
                tokens: s.displayBuckets.reduce((a,b)=>a+b.tokens,0),
                unpriced: s.displayBuckets.reduce((a,b)=>a+b.unpriced,0),
                bare: s.displayBuckets.reduce((a,b)=>a+b.bare,0),
                moneyKnown: s.displayBuckets.reduce((a,b)=>a+(b.moneyKnown||0),0),
                anyNull: s.displayBuckets.some(b => b.money == null),
                count: s.displayBuckets.length,
                merged: s.displayBuckets.filter(b => b.mergedCount > 1).length
            };
        }"""
    )
    series_tokens = sum(it["tokens"] for it in board["series"])
    series_unpriced = sum(it.get("unpriced") or 0 for it in board["series"])
    series_bare = sum(it.get("bare") or 0 for it in board["series"])
    series_known = sum(it.get("money_known") or 0 for it in board["series"])
    assert snap["merged"] > 0, "测试意图：必须触发合并"
    assert snap["tokens"] == series_tokens, f"合并后 tokens 守恒：{snap} vs series_tokens={series_tokens}"
    assert snap["unpriced"] == series_unpriced
    assert snap["bare"] == series_bare, f"bare 字段必须守恒：{snap} vs {series_bare}"
    assert abs(snap["moneyKnown"] - series_known) < 1e-6, f"moneyKnown 守恒：{snap} vs {series_known}"
    assert snap["anyNull"], "全含未计价 → merged.money=None"
    ctx.close()


def test_resize_and_font_zoom_preserve_layout(browser, html_server, engine_with_data):
    board = cost.overview(now=NOW, span="24h")
    html = cost_page(board, [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html_server, html, viewport=(1280, 800), theme="dark")
    # Zoom the page via CSS transform / font-size; our layout must still measure with real bbox.
    page.evaluate("document.documentElement.style.fontSize = '18px';")
    page.set_viewport_size({"width": 560, "height": 900})
    page.wait_for_timeout(200)
    overflow = page.evaluate(
        """() => {
            const svg = document.getElementById('ov-svg');
            const sb = svg.getBoundingClientRect();
            let bad = 0;
            svg.querySelectorAll('rect.bar, text.axis, line.grid').forEach(n => {
                const r = n.getBoundingClientRect();
                if (r.right > sb.right + 1 || r.left < sb.left - 1) bad++;
            });
            return bad;
        }"""
    )
    assert overflow == 0, f"字体放大 + 缩窄后仍有 {overflow} 元素溢出 svg 边界"
    ctx.close()
