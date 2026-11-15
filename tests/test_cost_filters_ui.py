"""成本页前端契约：筛选变化走 POST 再导航，失败可见；翻页保持纯 href；首渲染不写入。"""

from __future__ import annotations

from rogmem import cost
from rogmem.ui import cost_page


def _board(**changes):
    board = {
        "span": "30d",
        "worker": "",
        "model": "",
        "detail": "30d",
        "dworker": "",
        "dmodel": "",
        "page": 1,
        "pages": 1,
        "count": 0,
        "summary": {"money": None, "tokens": 0, "input": 0, "output": 0, "cache": 0, "unpriced": 0, "bare": 0},
        "series": [],
        "rows": [],
        "workers": list(cost.WORKERS),
        "models": [],
        "rate": None,
    }
    board.update(changes)
    return board


def test_select_does_not_use_location_shortcut():
    html = cost_page(_board(), [], {})
    # 旧的 "onchange='location=this.value'" 形式必须完全消失。
    assert "onchange='location=this.value'" not in html
    assert 'onchange="location=this.value"' not in html


def test_select_calls_save_cost_filter():
    html = cost_page(_board(), [], {})
    assert "saveCostFilter" in html
    # 至少有一个 select 把 onchange 绑给 saveCostFilter。
    assert "onchange='saveCostFilter(this)'" in html or 'onchange="saveCostFilter(this)"' in html


def test_tabs_intercept_click():
    html = cost_page(_board(), [], {})
    # 概览和明细 tab 的 <a> 带 data-cost-filter 钩子。
    assert "data-cost-filter" in html


def test_call_pager_shares_the_count_line():
    """调用明细的条数和翻页在同一行：左边计数，右边同一套翻页控件。"""

    html = cost_page(_board(page=2, pages=4, count=180), [], {})
    assert "class='list-foot'" in html
    foot = html.split("class='list-foot'", 1)[1].split("</form></div>", 1)[0]
    assert foot.index("共 180 条") < foot.index("<form class='pager'")
    assert "data-total='180'" in foot
    assert "data-page-size='20'" in foot
    assert "aria-label='页码'" in foot
    assert "value='2'" in foot
    assert "data-pages>9</span>" in foot
    assert "data-pages>4</span>" not in foot
    assert "href='/admin/costs?" in foot
    style = " ".join(html.split("</style>", 1)[0].split())
    assert ".list-foot { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-top: 8px; }" in style
    assert ".list-foot > .note { margin: 0; }" in style
    assert ".list-foot > .pager { margin: 0; flex: none; }" in style


def test_pagination_stays_plain_anchor():
    html = cost_page(_board(page=2, count=220), [], {})
    # 上/下一页仍是普通 <a href=...>，不带 data-cost-filter。
    assert "上一页" in html
    assert "下一页" in html
    # 具体：上/下一页按钮上不应该挂 data-cost-filter。
    for marker in ("上一页", "下一页"):
        idx = html.find(marker)
        window = html[max(0, idx - 200) : idx + 200]
        assert "data-cost-filter" not in window, f"翻页 `{marker}` 不该走保存通道"


def test_page_render_has_no_implicit_write_call():
    html = cost_page(_board(), [], {})
    # 首渲染脚本不应在顶层 IIFE 里立即调用 /admin/api/costs/filters。
    # 该路径应只出现在 saveCostFilter / saveCostFilterThen 的函数体里。
    body = html
    # 粗判：所有 /admin/api/costs/filters 出现位置必须在 saveCostFilter 定义之后。
    first = body.find("/admin/api/costs/filters")
    assert first > 0
    def_idx = body.find("function saveCostFilter")
    assert def_idx > 0 and def_idx < first, "fetch 调用必须出现在函数体内，不在顶层立即执行"


def test_post_helper_supports_then_url():
    """post(url, body, thenUrl) 新签名：传 thenUrl 则成功后 location=thenUrl，不传则 location.reload()。"""
    from rogmem.ui import page

    html = page("x", "crystals", "<p></p>")
    # post 定义里应能看到 thenUrl 分支。
    assert "thenUrl" in html
    assert "location.reload()" in html  # 旧默认路径保留


def test_select_option_values_still_carry_target_url():
    html = cost_page(_board(), [], {})
    # saveCostFilter 从 this.value 读取目标 URL；option 的 value 仍然是 /admin/costs?... 形式。
    assert "value='/admin/costs?" in html or 'value="/admin/costs?' in html
