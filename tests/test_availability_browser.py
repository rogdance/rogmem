"""探活色块在两条趋势图里都贴着时间刻度，并且颜色跟着这一格的时长走。"""

from __future__ import annotations

from datetime import timedelta

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from rogmem.ui import cost_page, queue_page  # noqa: E402
from tests.test_availability import (  # noqa: E402
    _cost_board,
    _open,
    _queue_board,
    _seed,
    _series,
    _window,
)


def _pages(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    start, end = _seed(engine)
    series = _series(start, 4)
    window = _window(start, end)
    return (
        cost_page(_cost_board(series, window), [], {}),
        queue_page(_queue_board(series, window)),
    )


def _open_page(page, html, viewport):
    page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
    page.add_init_script("try{localStorage.setItem('rogmem-theme','light');}catch(e){}")
    page.set_content(html, wait_until="domcontentloaded")


_MARKS = """(svgId) => {
  const svg = document.getElementById(svgId);
  const axis = svg.querySelector('line.axis');
  const axisBox = axis.getBoundingClientRect();
  const svgBox = svg.getBoundingClientRect();
  const bars = Array.from(svg.querySelectorAll('rect.bar'));
  const marks = Array.from(svg.querySelectorAll('rect.avail'));
  const base = Number(axis.getAttribute('y1'));
  const labels = Array.from(svg.querySelectorAll('text.axis')).filter(node => Number(node.getAttribute('y')) > base + 6);
  return {
    bars: bars.length,
    marks: marks.map((mark, i) => {
      const box = mark.getBoundingClientRect();
      const bar = bars[i].getBoundingClientRect();
      const center = box.left + box.width / 2;
      const near = labels.filter(label => {
        const lb = label.getBoundingClientRect();
        return Math.abs(lb.left + lb.width / 2 - center) <= Math.max(18, box.width);
      });
      const labelBottom = near.reduce((acc, label) => Math.max(acc, label.getBoundingClientRect().bottom), 0);
      return {
        tone: mark.getAttribute('data-tone'),
        depth: Number(mark.getAttribute('data-depth')),
        fill: mark.getAttribute('fill') || '',
        width: mark.width.baseVal.value,
        height: mark.height.baseVal.value,
        barW: bars[i].width.baseVal.value,
        dx: Math.abs(center - (bar.left + bar.width / 2)),
        belowAxis: box.top >= axisBox.bottom - 0.5,
        belowLabel: !near.length || box.top + 0.5 >= labelBottom,
        inside: box.left >= svgBox.left - 0.5 && box.right <= svgBox.right + 0.5 && box.bottom <= svgBox.bottom + 0.5
      };
    })
  };
}"""


def _expect(drawn):
    assert drawn["bars"] == 4
    assert len(drawn["marks"]) == 4
    tones = [item["tone"] for item in drawn["marks"]]
    assert tones == ["unknown", "ok", "warn", "down"], tones
    assert drawn["marks"][1]["depth"] == 1
    assert drawn["marks"][2]["depth"] == 0.75
    assert drawn["marks"][3]["depth"] == 1
    assert drawn["marks"][1]["fill"].startswith("hsl(152")
    assert drawn["marks"][2]["fill"].startswith("hsl(42")
    assert drawn["marks"][3]["fill"].startswith("hsl(352")
    for item in drawn["marks"]:
        assert abs(item["width"] - item["barW"]) < 0.2, item
        assert item["height"] == 8, item
        assert item["dx"] <= 1.5, item
        assert item["belowAxis"], item
        assert item["belowLabel"], item
        assert item["inside"], item


@pytest.mark.parametrize("viewport", [(1280, 800), (390, 844)])
def test_squares_sit_under_the_time_labels_on_both_charts(browser, tmp_path, monkeypatch, viewport):
    cost_html, queue_html = _pages(tmp_path, monkeypatch)
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        _open_page(page, cost_html, viewport)
        page.wait_for_function("window.__COST_CHART__")
        page.evaluate("window.__COST_CHART__.render()")
        _expect(page.evaluate(_MARKS, "ov-svg"))
        page.focus("#ov-svg")
        page.keyboard.press("Home")
        page.keyboard.press("ArrowRight")
        page.keyboard.press("ArrowRight")
        assert "降级 45 分钟" in page.locator("#ov-tip").inner_text()
        _open_page(page, queue_html, viewport)
        page.wait_for_function("window.__QUEUE_CHART__")
        page.evaluate("window.__QUEUE_CHART__.render()")
        _expect(page.evaluate(_MARKS, "q-svg"))
        page.focus("#q-svg")
        page.keyboard.press("End")
        assert "不可用" in page.locator("#q-tip").inner_text()
        assert "1 小时" in page.locator("#q-tip").inner_text()
    finally:
        ctx.close()
