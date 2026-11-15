"""成本柱图真实浏览器行为验收。

用 Playwright (chromium headless) 直接把 `cost_page(board, [], {})` 的 HTML
灌到空白页里，验证坐标轴 / 网格 / tick / tooltip / 键盘 / 触摸 / 主题配色 / resize
/ 展示桶合并守恒 / 无数据 / 全零 等。

跳过策略：没有 playwright 包或 chromium 不可用时整个文件 skip，不假装通过。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from rogmem import cost  # noqa: E402
from rogmem.ui import cost_page  # noqa: E402


# Shared `browser` fixture comes from tests/conftest_browser.py via conftest.pytest_plugins.


def _board(series, *, span="24h", scope="", summary=None, window=None):
    summary = summary or {
        "money": sum((s.get("money") or 0) for s in series) or 0.0,
        "tokens": sum(s["tokens"] for s in series),
        "input": sum(s["input"] for s in series),
        "output": sum(s["output"] for s in series),
        "cache": sum(s["cache"] for s in series),
        "unpriced": 0,
        "bare": 0,
    }
    # Tests in this file are chart-only; we still must give a window so the UI does not refuse to
    # draw. We compose one from the fixture inputs — this is a test-only board builder, not a
    # production code path, so no fallback hides inside the chart script. The real end-to-end
    # window ≡ span_bounds check lives in tests/test_cost_chart_integration.py.
    if window is None and series:
        grain = series[0].get("grain") or "hour"
        from datetime import timedelta as _td  # local to avoid polluting import order

        last_at = _dt_parse(series[-1]["at"])
        step = _td(hours=1) if grain == "hour" else _td(days=1)
        window = {
            "start": series[0]["at"],
            "end": (last_at + step).isoformat(),
            "grain": grain,
            "tz": "UTC+8",
        }
    elif window is None:
        now = _dt_now()
        window = {"start": now.isoformat(), "end": now.isoformat(), "grain": "hour", "tz": "UTC+8"}
    return {
        "span": span,
        "worker": "",
        "model": "",
        "detail": "30d",
        "dworker": "",
        "dmodel": "",
        "page": 1,
        "pages": 1,
        "count": 0,
        "summary": summary,
        "series": series,
        "window": window,
        "rows": [],
        "workers": list(cost.WORKERS),
        "models": [],
        "rate": None,
    }


def _dt_parse(iso: str):
    from datetime import datetime as _dt
    return _dt.fromisoformat(iso.replace("Z", "+00:00"))


def _dt_now():
    from datetime import datetime as _dt
    return _dt.now(timezone.utc)


def _hour_series(start_utc: datetime, count: int, values=None):
    values = values if values is not None else [100 * (i + 1) for i in range(count)]
    return [
        {
            "at": (start_utc + timedelta(hours=k)).isoformat(),
            "label": (start_utc + timedelta(hours=k)).strftime("%m-%d %H:00"),
            "grain": "hour",
            "money": float(values[k]) / 100.0,
            "tokens": int(values[k]),
            "input": int(values[k] * 0.6),
            "output": int(values[k] * 0.4),
            "cache": 0,
            "unpriced": 0,
            "bare": 0,
        }
        for k in range(count)
    ]


def _day_series(start_utc: datetime, count: int, tokens_fn=None):
    tokens_fn = tokens_fn or (lambda k: (k + 1) * 1000)
    return [
        {
            "at": (start_utc + timedelta(days=k)).isoformat(),
            "label": (start_utc + timedelta(days=k)).strftime("%m-%d"),
            "grain": "day",
            "money": None,
            "tokens": int(tokens_fn(k)),
            "input": int(tokens_fn(k)),
            "output": 0,
            "cache": 0,
            "unpriced": 1,
            "bare": 0,
        }
        for k in range(count)
    ]


def _open(page, html, viewport=(1280, 800), theme="dark"):
    page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
    # inject localStorage hint so page() boot script picks light/dark deterministically
    page.add_init_script(f"try{{localStorage.setItem('rogmem-theme', {json.dumps(theme)});}}catch(e){{}}")
    page.set_content(html, wait_until="domcontentloaded")
    page.wait_for_function("window.__COST_CHART__ && typeof window.__COST_CHART__.render === 'function'")
    # ensure resize-scheduled renders have flushed
    page.evaluate("window.__COST_CHART__.render()")


# ======================================================================
# §9.3 bars / axis / colors / resize / empty / zero / 10000-bounded
# ======================================================================


def test_bars_rendered_with_no_negative_width(browser):
    start = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)
    html = cost_page(_board(_hour_series(start, 24), span="24h"), [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html)
    widths = page.evaluate("Array.from(document.querySelectorAll('rect.bar')).map(r => r.width.baseVal.value)")
    assert len(widths) == 24
    for w in widths:
        assert w > 0, f"bar width must be positive, got {w}"
    ctx.close()


_MILLION_MONEY = re.compile(r"^¥\d[\d,]*\.\d{2} 百万$")
_MILLION_TOKEN = re.compile(r"^\d[\d,]*\.\d{2} 百万$")


def _y_labels(page):
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('#ov-svg text.axis'))
            .filter(t => t.getAttribute('text-anchor') === 'end')
            .map(t => t.textContent)"""
    )


def test_y_axis_money_label_uses_million_unit(browser):
    start = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)
    html = cost_page(_board(_hour_series(start, 24), span="24h"), [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html)
    labels = _y_labels(page)
    assert labels, "money unit 下 y 轴必须有刻度"
    for lab in labels:
        assert _MILLION_MONEY.match(lab), f"金额轴必须写成百万，实际 {lab}"
    ctx.close()


def test_y_axis_token_label_uses_million_unit(browser):
    start = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)
    series = _hour_series(start, 24, values=[10_000_000 * (i + 1) for i in range(24)])
    html = cost_page(_board(series, span="24h"), [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html)
    page.click("#ov-unit button[data-unit='token']")
    page.wait_for_timeout(100)
    labels = _y_labels(page)
    assert labels, "token unit 下 y 轴必须有刻度"
    joined = "".join(labels)
    for lab in labels:
        assert _MILLION_TOKEN.match(lab), f"token 轴必须写成百万，实际 {lab}"
    assert "50.00 百万" in labels
    assert "¥" not in joined, f"token 轴不能出现 ¥：{joined}"
    assert "10,000,000" not in joined and "10000000" not in joined
    ctx.close()


def test_dark_theme_bar_is_pink_computed_fill(browser):
    start = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)
    html = cost_page(_board(_hour_series(start, 24), span="24h"), [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html, theme="dark")
    fill = page.evaluate(
        "getComputedStyle(document.querySelector('rect.bar')).fill"
    )
    # chromium reports rgb()
    assert fill.replace(" ", "") in {"rgb(255,55,99)"}, f"暗色柱必须是 #ff3763，实际 {fill}"
    ctx.close()


def test_light_theme_bar_is_also_pink_computed_fill(browser):
    start = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)
    html = cost_page(_board(_hour_series(start, 24), span="24h"), [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html, theme="light")
    fill = page.evaluate(
        "getComputedStyle(document.querySelector('rect.bar')).fill"
    )
    assert fill.replace(" ", "") in {"rgb(255,55,99)"}, (
        f"浅色主题下柱也必须沿用粉红 #ff3763（不换 --brand），实际 {fill}"
    )
    ctx.close()


def test_x_tick_labels_do_not_overlap_at_three_widths(browser):
    start = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)
    html = cost_page(_board(_hour_series(start, 24), span="24h"), [], {})
    for width in (360, 820, 1280):
        ctx = browser.new_context()
        page = ctx.new_page()
        _open(page, html, viewport=(width, 800))
        rects = page.evaluate(
            """() => {
                const nodes = Array.from(document.querySelectorAll('text.axis'))
                    .filter(t => {
                        const y = Number(t.getAttribute('y'));
                        const svg = document.getElementById('ov-svg');
                        const cb = svg.getBoundingClientRect().bottom;
                        // keep only x-axis labels (below baseline); y labels sit higher
                        return y > 200;
                    });
                return nodes.map(t => {
                    const r = t.getBoundingClientRect();
                    return {x: r.left, right: r.right, text: t.textContent};
                }).sort((a,b) => a.x - b.x);
            }"""
        )
        for a, b in zip(rects, rects[1:]):
            assert a["right"] <= b["x"] + 0.5, (
                f"viewport={width} 下 tick 文本重叠：{a} 与 {b}"
            )
        ctx.close()


# Tooltip clip tests moved to tests/test_cost_chart_browser_integration.py — those use a real
# cost.overview() window with microsecond edges (2026-11-11 03:37:42.123Z) so the acceptance can
# tell the difference between "window.start clipping" and the old "integer-hour bucket start".


def test_keyboard_arrows_update_active_descendant(browser):
    start = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)
    html = cost_page(_board(_hour_series(start, 24), span="24h"), [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html)
    page.focus("#ov-svg")
    page.keyboard.press("Home")
    first = page.get_attribute("#ov-svg", "aria-activedescendant")
    page.keyboard.press("ArrowRight")
    second = page.get_attribute("#ov-svg", "aria-activedescendant")
    assert first == "ov-bar-0"
    assert second == "ov-bar-1"
    page.keyboard.press("End")
    last = page.get_attribute("#ov-svg", "aria-activedescendant")
    assert last == "ov-bar-23"
    ctx.close()


def test_touch_tap_opens_tooltip_and_persists(browser):
    start = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)
    html = cost_page(_board(_hour_series(start, 24), span="24h"), [], {})
    ctx = browser.new_context(has_touch=True)
    page = ctx.new_page()
    _open(page, html)
    bar = page.query_selector("rect.bar[id='ov-bar-5']")
    box = bar.bounding_box()
    page.touchscreen.tap(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_timeout(100)
    # Touch pointer always fires `pointerleave` after `pointerdown`; the tip must survive it.
    state = page.evaluate(
        "({hidden: document.getElementById('ov-tip').hidden, html: document.getElementById('ov-tip').innerHTML})"
    )
    assert state["hidden"] is False, f"触摸后 tooltip 应保持可见，实际 state={state}"
    assert "UTC+8" in state["html"]
    ctx.close()


def test_resize_redraws_within_bounds(browser):
    start = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)
    html = cost_page(_board(_hour_series(start, 24), span="24h"), [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html, viewport=(1280, 800))
    page.set_viewport_size({"width": 360, "height": 640})
    # wait for debounced ResizeObserver
    page.wait_for_timeout(200)
    offenders = page.evaluate(
        """() => {
            const svg = document.getElementById('ov-svg');
            const sb = svg.getBoundingClientRect();
            return Array.from(svg.querySelectorAll('rect.bar, text.axis')).map(n => {
                const r = n.getBoundingClientRect();
                return {text: n.textContent || n.getAttribute('class'), left: r.left - sb.left, right: r.right - sb.right};
            }).filter(x => x.right > 1 || x.left < -1);
        }"""
    )
    assert offenders == [], f"resize 后仍有元素溢出 svg: {offenders}"
    ctx.close()


def test_10000_day_history_dom_rect_bounded_and_conserves_sum(browser):
    start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    series = _day_series(start, 10000, tokens_fn=lambda k: (k % 7) + 1)
    total_tokens = sum(s["tokens"] for s in series)
    html = cost_page(_board(series, span="all"), [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html, viewport=(1280, 800))
    state = page.evaluate(
        """() => {
            const s = window.__COST_CHART__.snapshot();
            const bars = document.querySelectorAll('rect.bar');
            const tokens = s.displayBuckets.reduce((a,b)=>a+b.tokens,0);
            const svg = document.getElementById('ov-svg');
            const w = svg.getBoundingClientRect().width;
            return {barCount: bars.length, displayCount: s.displayBuckets.length, tokens: tokens, width: w};
        }"""
    )
    # DOM 恒界：rect.bar 数 ≤ floor(trackWidth / 4) + 一个保守 margin
    # trackWidth = width - 16 - 56 = width - 72
    track_w = state["width"] - 72
    assert state["barCount"] <= int(track_w / 4) + 2, (
        f"DOM 柱数必须按宽度预算有界，实际 bar={state['barCount']} / track_w={track_w}"
    )
    assert state["tokens"] == total_tokens, (
        f"展示桶合并必须守恒：原始 sum={total_tokens} 合并后 sum={state['tokens']}"
    )
    ctx.close()


def test_chart_speaks_millions_while_table_keeps_exact_figures(browser):
    """柱、轴、悬停只写百万。底下调用明细仍是精确 token 和金额。"""

    start = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)
    tokens = 2_345_678
    money = 1_234_567.89
    series = _hour_series(start, 4, values=[tokens] * 4)
    for item in series:
        item["money"] = money
        item["tokens"] = tokens
        item["input"] = tokens
        item["output"] = 0
    board = _board(series, span="24h")
    board["rows"] = [
        {
            "at": "2026-11-11 11:00",
            "worker": "初始蒸馏",
            "model": "演示",
            "prompt": "问",
            "answer": "答",
            "input": tokens,
            "output": 0,
            "cache": 0,
            "total": tokens,
            "money": money,
        }
    ]
    board["count"] = 1
    html = cost_page(board, [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html)
    money_labels = _y_labels(page)
    assert money_labels
    for lab in money_labels:
        assert _MILLION_MONEY.match(lab), lab
        assert "1,234,567" not in lab
        assert "2,345,678" not in lab
    titles = page.evaluate("Array.from(document.querySelectorAll('rect.bar title')).map(t => t.textContent)")
    assert titles
    for title in titles:
        assert "百万" in title
        assert "2,345,678" not in title and "2345678" not in title
        assert "1,234,567" not in title
    page.focus("#ov-svg")
    page.keyboard.press("Home")
    page.wait_for_timeout(50)
    tip = page.inner_text("#ov-tip")
    assert re.search(r"¥\d[\d,]*\.\d{2} 百万", tip), tip
    assert "2,345,678" not in tip and "1,234,567" not in tip
    page.click("#ov-unit button[data-unit='token']")
    page.wait_for_timeout(50)
    for lab in _y_labels(page):
        assert _MILLION_TOKEN.match(lab), lab
        assert "2,345,678" not in lab
    page.keyboard.press("Home")
    page.wait_for_timeout(50)
    token_tip = page.inner_text("#ov-tip")
    assert "2.35 百万" in token_tip, token_tip
    assert "2,345,678" not in token_tip and "2345678" not in token_tip
    table = page.inner_text("table.calls")
    assert "2,345,678" in table
    assert "¥1,234,567.89" in table
    ctx.close()


def test_empty_series_shows_caption_and_no_bars(browser):
    html = cost_page(_board([], span="24h"), [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html)
    count = page.evaluate("document.querySelectorAll('rect.bar').length")
    caption = page.inner_text("#ov-caption")
    assert count == 0
    assert "无数据" in caption
    ctx.close()


def test_full_zero_shows_only_zero_y_axis(browser):
    start = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)
    series = _hour_series(start, 24, values=[0] * 24)
    html = cost_page(_board(series, span="24h"), [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html)
    y_labels = page.evaluate(
        """() => Array.from(document.querySelectorAll('text.axis'))
            .filter(t => Number(t.getAttribute('y')) < 200)
            .map(t => t.textContent)"""
    )
    # 全零：y 轴只有一条百万刻度，不写个位 0 冒充精确计数。
    assert y_labels == ["¥0.00 百万"], f"全零时 y 轴应只有 ¥0.00 百万，实际 {y_labels}"
    heights = page.evaluate(
        "Array.from(document.querySelectorAll('rect.bar')).map(r => r.height.baseVal.value)"
    )
    for h in heights:
        assert h == 0, f"零值柱高必须为 0（不再 2px 伪装），实际 {heights}"
    ctx.close()


# ======================================================================
# merge domain conservation unit (Task 5)
# ======================================================================


def test_merge_preserves_total_tokens_and_null_money(browser):
    """合并块含 money=null → 合并后 money=null；tokens 等字段守恒。"""

    start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    series = []
    for k in range(400):
        money = None if k % 50 == 0 else 0.01
        series.append(
            {
                "at": (start + timedelta(days=k)).isoformat(),
                "label": "x",
                "grain": "day",
                "money": money,
                "tokens": 10 + k,
                "input": 6,
                "output": 4,
                "cache": 1,
                "unpriced": 0 if money is not None else 1,
                "bare": 0,
            }
        )
    expected_tokens = sum(s["tokens"] for s in series)
    html = cost_page(_board(series, span="all"), [], {})
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html, viewport=(400, 800))  # narrow to force merge
    data = page.evaluate(
        """() => {
            const s = window.__COST_CHART__.snapshot();
            const anyNull = s.displayBuckets.some(b => b.money == null);
            const tokens = s.displayBuckets.reduce((a,b)=>a+b.tokens,0);
            return {anyNull, tokens, count: s.displayBuckets.length};
        }"""
    )
    assert data["tokens"] == expected_tokens, f"合并后 tokens 守恒失败: {data}"
    assert data["anyNull"], "含 null money 的 chunk 合并后应仍 null，不得被当作 0"
    ctx.close()
