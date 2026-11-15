"""队列柱图在浏览器里要有纵轴刻度、时间横轴和虚线网格。

跟成本明细同一套几何：真实像素、nice 刻度、首末时间一定留下、相邻刻度不重叠。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import re

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from rogmem.ui import queue_page  # noqa: E402


def _series(start: datetime, count: int, tasks, chars) -> list[dict]:
    return [
        {
            "at": (start + timedelta(hours=i)).isoformat(),
            "label": "slot",
            "tasks": int(tasks[i]),
            "chars": int(chars[i]),
        }
        for i in range(count)
    ]


def _board(series, *, span="24h", grain="hour", window=None) -> dict:
    start = datetime.fromisoformat(series[0]["at"])
    end = datetime.fromisoformat(series[-1]["at"])
    step = timedelta(hours=1) if grain == "hour" else timedelta(days=1)
    if window is None:
        window = {
            "start": start.isoformat(),
            "end": (end + step).isoformat(),
            "grain": grain,
            "tz": "UTC+8",
        }
    return {
        "span": span,
        "tab": "all",
        "page": 1,
        "pages": 1,
        "page_size": 50,
        "total": 0,
        "rows": [],
        "counts": {"all": 0, "done": 0, "queued": 0, "leased": 0},
        "summary": {
            "done_tasks": sum(item["tasks"] for item in series),
            "done_chars": sum(item["chars"] for item in series),
            "queued_tasks": 0,
            "queued_chars": 0,
            "per_hour_tasks": 0,
            "per_hour_chars": 0,
            "running": 1,
            "stopped": 0,
            "untimed_tasks": 0,
        },
        "series": series,
        "window": window,
    }


def _open(page, html, viewport=(1280, 800)):
    page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
    page.add_init_script("try{localStorage.setItem('rogmem-theme','dark');}catch(e){}")
    page.set_content(html, wait_until="domcontentloaded")
    page.wait_for_function("window.__QUEUE_CHART__ && typeof window.__QUEUE_CHART__.render === 'function'")
    page.evaluate("window.__QUEUE_CHART__.render()")


def _hour_html():
    # 02:00Z = 10:00 北京时间，同一天内 8 个小时桶。任务数最高 4，方便认纵轴。
    start = datetime(2026, 11, 12, 2, tzinfo=timezone.utc)
    tasks = [0, 2, 0, 4, 0, 1, 0, 3]
    chars = [0, 200, 0, 400, 0, 100, 0, 300]
    return queue_page(_board(_series(start, 8, tasks, chars)))


_AXIS = """() => {
  const svg = document.getElementById('q-svg');
  const texts = Array.from(svg.querySelectorAll('text.axis'));
  const baseLine = svg.querySelector('line.axis');
  const baseY = baseLine ? Number(baseLine.getAttribute('y1')) : 0;
  const yLabels = texts.filter(t => Number(t.getAttribute('y')) <= baseY + 6).map(t => ({
    text: t.textContent,
    y: Number(t.getAttribute('y')),
    right: t.getBoundingClientRect().right
  }));
  const xLabels = texts.filter(t => Number(t.getAttribute('y')) > baseY + 6).map(t => {
    const r = t.getBoundingClientRect();
    return {text: t.textContent, x: r.left, right: r.right, y: Number(t.getAttribute('y'))};
  }).sort((a, b) => a.x - b.x);
  const grids = Array.from(svg.querySelectorAll('line.grid')).map(line => ({
    x1: Number(line.getAttribute('x1')),
    x2: Number(line.getAttribute('x2')),
    y: Number(line.getAttribute('y1')),
    dash: getComputedStyle(line).strokeDasharray
  }));
  const bars = Array.from(svg.querySelectorAll('rect.bar')).map(bar => ({
    x: Number(bar.getAttribute('x')),
    h: bar.height.baseVal.value,
    tasks: Number(bar.getAttribute('data-tasks'))
  }));
  const base = svg.querySelector('line.axis');
  return {
    viewBox: svg.getAttribute('viewBox'),
    yLabels,
    xLabels,
    grids,
    bars,
    baseline: base ? {x1: Number(base.getAttribute('x1')), x2: Number(base.getAttribute('x2'))} : null
  };
}"""


def test_axes_grid_and_time_ticks_match_the_cost_chart(browser):
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, _hour_html())
    drawn = page.evaluate(_AXIS)
    assert drawn["viewBox"] in (None, ""), drawn
    assert drawn["baseline"] and drawn["baseline"]["x2"] > drawn["baseline"]["x1"]
    assert len(drawn["grids"]) >= 2
    for grid in drawn["grids"]:
        assert grid["x2"] > grid["x1"]
        assert "2" in grid["dash"] and "4" in grid["dash"]
        assert grid["dash"] != "none"
    y_text = [item["text"] for item in drawn["yLabels"]]
    assert "0" in y_text
    assert "4" in y_text
    zero = next(item for item in drawn["yLabels"] if item["text"] == "0")
    top = next(item for item in drawn["yLabels"] if item["text"] == "4")
    assert top["y"] < zero["y"]
    x_text = [item["text"] for item in drawn["xLabels"]]
    assert "10:00" in x_text
    assert "17:00" in x_text
    for item in drawn["xLabels"]:
        assert ":" in item["text"] or "-" in item["text"]
    for left, right in zip(drawn["xLabels"], drawn["xLabels"][1:]):
        assert left["right"] <= right["x"] + 0.5, (left, right)
    assert drawn["bars"][0]["h"] == 0
    assert drawn["bars"][3]["h"] > 0
    assert zero["right"] <= drawn["bars"][3]["x"] + 0.5
    ctx.close()


def test_tooltip_names_the_bucket_clock(browser):
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, _hour_html())
    page.focus("#q-svg")
    page.keyboard.press("Home")
    page.keyboard.press("ArrowRight")
    tip = page.locator("#q-tip")
    assert tip.is_visible()
    text = tip.inner_text()
    assert "11:00" in text
    assert "12:00" in text
    assert "UTC+8" in text
    assert "2 项" in text
    ctx.close()


def test_char_unit_redraws_the_y_axis(browser):
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, _hour_html())
    page.click("[data-qunit='chars']")
    drawn = page.evaluate(_AXIS)
    y_text = [item["text"] for item in drawn["yLabels"]]
    assert "0" in y_text
    assert "400" in y_text
    assert any(grid["dash"] != "none" for grid in drawn["grids"])
    page.focus("#q-svg")
    page.keyboard.press("Home")
    for _ in range(3):
        page.keyboard.press("ArrowRight")
    assert "400 字" in page.locator("#q-tip").inner_text()
    ctx.close()


def test_x_ticks_stay_apart_on_a_narrow_screen(browser):
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, _hour_html(), viewport=(390, 844))
    drawn = page.evaluate(_AXIS)
    x_text = [item["text"] for item in drawn["xLabels"]]
    assert "10:00" in x_text
    assert "17:00" in x_text
    for left, right in zip(drawn["xLabels"], drawn["xLabels"][1:]):
        assert left["right"] <= right["x"] + 0.5, (left, right)
    assert drawn["yLabels"]
    ctx.close()


def test_long_history_merges_bars_without_dropping_counts(browser):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    count = 2000
    series = [
        {
            "at": (start + timedelta(days=i)).isoformat(),
            "label": "day",
            "tasks": 1,
            "chars": 10,
        }
        for i in range(count)
    ]
    html = queue_page(_board(series, span="all", grain="day"))
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html, viewport=(820, 800))
    total = page.evaluate(
        """() => Array.from(document.querySelectorAll('#q-svg rect.bar'))
            .reduce((sum, bar) => sum + Number(bar.getAttribute('data-tasks')), 0)"""
    )
    bar_count = page.evaluate("document.querySelectorAll('#q-svg rect.bar').length")
    assert total == count
    assert 1 < bar_count < count
    x_text = " ".join(
        page.evaluate("Array.from(document.querySelectorAll('#q-svg text.axis')).map(t => t.textContent)")
    )
    assert "-" in x_text
    ctx.close()


def _partial_day_html():
    """24 小时窗口的两头都落在 11:37，不是整点。首桶从 11:00 开始，末桶在 11:37 被裁掉。"""
    start = datetime(2026, 11, 11, 3, 37, tzinfo=timezone.utc)
    end = datetime(2026, 11, 12, 3, 37, tzinfo=timezone.utc)
    first = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)
    series = [
        {
            "at": (first + timedelta(hours=i)).isoformat(),
            "label": "slot",
            "tasks": 1,
            "chars": 10,
        }
        for i in range(25)
    ]
    return queue_page(
        _board(
            series,
            span="24h",
            grain="hour",
            window={"start": start.isoformat(), "end": end.isoformat(), "grain": "hour", "tz": "UTC+8"},
        )
    )


def _x_labels(page):
    return page.evaluate(_AXIS)["xLabels"]


def test_cross_day_labels_follow_the_ticks_that_stay_visible(browser):
    html = _partial_day_html()
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, html, viewport=(1280, 800))
    wide = [item["text"] for item in _x_labels(page)]
    assert wide[0].startswith("11-11"), wide
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("window.__QUEUE_CHART__.render()")
    narrow = _x_labels(page)
    texts = [item["text"] for item in narrow]
    previous = None
    for text in texts:
        found = re.search(r"(\d{2}):(\d{2})", text)
        assert found, text
        minute = int(found.group(1)) * 60 + int(found.group(2))
        if previous is not None and minute < previous and not re.search(r"\d{2}-\d{2}", text):
            raise AssertionError(f"新的一天没有写在留下的刻度上：{texts}")
        previous = minute
    for left, right in zip(narrow, narrow[1:]):
        assert left["right"] <= right["x"] + 0.5, (left, right, texts)
    ctx.close()


def test_tooltip_clips_to_the_real_window_edges(browser):
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, _partial_day_html())
    page.focus("#q-svg")
    page.keyboard.press("Home")
    opening = page.locator("#q-tip").inner_text()
    assert "11:37" in opening, opening
    assert "11:00 –" not in opening, opening
    page.keyboard.press("End")
    closing = page.locator("#q-tip").inner_text()
    assert "11:37" in closing, closing
    assert "– 12:00" not in closing, closing
    ctx.close()


def test_pointer_reads_the_column_under_the_cursor(browser):
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, _hour_html())
    box = page.locator("#q-svg rect.hit").nth(3).bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    text = page.locator("#q-tip").inner_text()
    assert "4 项" in text, text
    assert "13:00" in text and "14:00" in text, text
    ctx.close()


def test_a_short_chart_still_draws_axes(browser):
    ctx = browser.new_context()
    page = ctx.new_page()
    _open(page, _hour_html())
    width = page.evaluate(
        """() => {
          const fig = document.querySelector('.ov-chart');
          fig.style.boxSizing = 'border-box';
          fig.style.width = '148px';
          fig.style.paddingLeft = '24px';
          fig.style.paddingRight = '24px';
          window.__QUEUE_CHART__.render();
          return document.getElementById('q-svg').getBoundingClientRect().width;
        }"""
    )
    drawn = page.evaluate(_AXIS)
    assert width >= 80, width
    assert drawn["yLabels"], (width, drawn)
    assert drawn["baseline"]
    assert drawn["xLabels"]
    ctx.close()
