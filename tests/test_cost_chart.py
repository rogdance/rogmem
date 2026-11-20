"""成本柱图改造——payload 契约 + 四职责脚本 + SVG 卫生 + 主题配色。

设计规格：docs/superpowers/specs/2026-11-12-cost-chart-axes-and-grid.md
实施计划：docs/superpowers/plans/2026-11-12-cost-chart-axes-and-grid.md

断言只覆盖"数据契约 + 字符串卫生"这一层；几何、tooltip 区间裁剪、tick 防碰撞、
resize、键盘/触摸可达走真实浏览器测（tests/test_cost_chart_browser.py）。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rogmem import cost
from rogmem.ui import cost_page, page


_PAYLOAD_RE = re.compile(r"__rogmemCostChartBoot\((\{.*?\})\);", re.DOTALL)


def _payload(html: str) -> dict:
    match = _PAYLOAD_RE.search(html)
    assert match, "cost_page 应该在脚本里注入 `__rogmemCostChartBoot({...});`"
    return json.loads(match.group(1))


def _board(**changes) -> dict:
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
        "summary": {"money": 0.0, "tokens": 0, "input": 0, "output": 0, "cache": 0, "unpriced": 0, "bare": 0},
        "series": [],
        "rows": [],
        "workers": list(cost.WORKERS),
        "models": [],
        "rate": None,
    }
    board.update(changes)
    return board


# ----------------------------- 契约：payload 顶层 -----------------------------


def test_payload_has_window_and_span_and_grain():
    series = [
        {
            "at": "2026-11-11T03:00:00+00:00",
            "label": "11-11 11:00",
            "grain": "hour",
            "money": 0.1,
            "tokens": 100,
            "input": 60,
            "output": 40,
            "cache": 0,
            "unpriced": 0,
            "bare": 0,
        },
        {
            "at": "2026-11-11T04:00:00+00:00",
            "label": "11-11 12:00",
            "grain": "hour",
            "money": 0.2,
            "tokens": 200,
            "input": 120,
            "output": 80,
            "cache": 0,
            "unpriced": 0,
            "bare": 0,
        },
    ]
    html = cost_page(_board(span="24h", series=series), [], {})
    data = _payload(html)
    assert data["span"] == "24h"
    assert data["grain"] == "hour"
    assert "window" in data
    assert "start" in data["window"] and "end" in data["window"]
    assert data["window"]["tz"] == "UTC+8"
    assert data["unit"]["money"]["currency"] == "CNY"
    assert data["unit"]["token"]["name"]


def test_payload_series_carries_at_and_grain():
    series = [
        {
            "at": "2026-11-11T03:00:00+00:00",
            "label": "11-11 11:00",
            "grain": "hour",
            "money": 0.0,
            "tokens": 0,
            "input": 0,
            "output": 0,
            "cache": 0,
            "unpriced": 0,
            "bare": 0,
        }
    ]
    html = cost_page(_board(span="24h", series=series), [], {})
    data = _payload(html)
    assert len(data["series"]) == 1
    item = data["series"][0]
    assert item["at"] == "2026-11-11T03:00:00+00:00"
    assert item["grain"] == "hour"


def test_payload_series_shape_is_strictly_additive():
    """原有字段 label / money / tokens / input / output / cache / unpriced 必须全部保留且类型不变。"""

    series = [
        {
            "at": "2026-11-11T03:00:00+00:00",
            "label": "11-11 11:00",
            "grain": "hour",
            "money": 0.5,
            "tokens": 1000,
            "input": 600,
            "output": 400,
            "cache": 50,
            "unpriced": 0,
            "bare": 0,
        }
    ]
    html = cost_page(_board(span="24h", series=series), [], {})
    item = _payload(html)["series"][0]
    assert item["label"] == "11-11 11:00"
    assert item["money"] == pytest.approx(0.5)
    assert item["tokens"] == 1000
    assert item["input"] == 600
    assert item["output"] == 400
    assert item["cache"] == 50
    assert item["unpriced"] == 0


def test_handbuilt_board_without_window_is_marked_unavailable():
    """R2 协调审复审修正：手工 board（无 window）绝不允许前端推算边界冒充真实区间。

    真实权威的 window 必须来自 `cost.overview()`。缺 window → payload 必须显式标 available=False，
    前端会据此降级而非悄悄把 series[0].at / last.at+delta 当作真实查询边界。

    真实边界的恒等性由 `tests/test_cost_chart_integration.py::test_payload_window_strictly_equals_span_bounds`
    覆盖；那里用真实 cost.overview 产 board，断言 start/end 与 span_bounds 完全一致。
    """

    series = [
        {
            "at": "2026-11-10T03:00:00+00:00",
            "label": "x",
            "grain": "hour",
            "money": 0.0,
            "tokens": 0,
            "input": 0,
            "output": 0,
            "cache": 0,
            "unpriced": 0,
            "bare": 0,
        }
    ]
    html = cost_page(_board(span="24h", series=series), [], {})
    data = _payload(html)
    assert data["window"]["available"] is False
    assert data["window"]["start"] is None
    assert data["window"]["end"] is None


# ----------------------------- SVG 卫生 -----------------------------


def test_svg_does_not_use_preserve_aspect_ratio_none():
    html = cost_page(_board(), [], {})
    assert 'preserveAspectRatio="none"' not in html
    assert "preserveAspectRatio='none'" not in html


def test_svg_declares_aria_describedby_caption():
    html = cost_page(_board(), [], {})
    assert 'id="ov-svg"' in html
    assert 'aria-describedby="ov-caption"' in html
    assert 'id="ov-caption"' in html
    assert "UTC+8" in html


# ----------------------------- 职责分离硬证据 -----------------------------


def test_chart_script_has_four_responsibilities():
    """_CHART_SCRIPT 字符串包含四个职责的函数名，拒绝再出现一个巨大 draw。"""

    html = cost_page(_board(), [], {})
    for name in ("buildDisplayBuckets", "buildYScale", "buildXTicks", "renderTooltip"):
        assert name in html, f"职责函数 `{name}` 必须显式存在于脚本里"


def test_old_draw_function_is_gone():
    """确保柱图改造里不再残留老的 `function draw(values)`/`paint()`/`mil()` 风格的旧绘图入口。"""

    html = cost_page(_board(), [], {})
    # 这些是原柱图脚本独有的函数签名；如果还在，说明职责分离没落地或 draw 被二次嵌回去
    assert "function draw(values)" not in html


# ----------------------------- 配色契约 -----------------------------


def test_light_and_dark_bar_color_is_same_pink():
    html = page("x", "costs", "<p></p>")
    # 新增 CSS var `--chart-bar: #ff3763` 在 :root 与 html[data-theme="dark"] 两处都有；
    # 不是走 `var(--brand)`。
    occurrences = re.findall(r"--chart-bar:\s*#ff3763", html)
    assert len(occurrences) >= 2, "--chart-bar 必须在浅/深两个主题块都显式写 #ff3763"
    assert "--chart-bar: var(--brand)" not in html


def test_grid_and_axis_tokens_use_existing_vars():
    html = page("x", "costs", "<p></p>")
    assert "--chart-grid:" in html
    assert "--chart-axis:" in html


def test_no_chart_surface_var_leaked():
    """设计复核：不定义却不用的 `--chart-surface`；要不别定义，要不就用。规格里选择不定义。"""

    html = page("x", "costs", "<p></p>")
    assert "--chart-surface" not in html


# ----------------------------- 不动清单 -----------------------------


_COST_PY = Path(__file__).resolve().parent.parent / "rogmem" / "cost.py"
# 2026-11-13 修订：走势按桶和计价身份在库里加总，不再逐条把调用装进 Python。
# 记账不再叫醒没人在读的汇总。金额口径（money=None、分项先按次再加、span_bounds、buckets、window、money_known）未动。
# 2026-11-18 修订：工人常量改中文名（初始蒸馏→草稿，并按界面统一为萃取口径），新增 migrate_worker_names
# 迁移把旧的 worker='初始蒸馏' 行并到 '草稿'。金额口径仍未动。
# 2026-11-20 修订：卡片读模型与调用列表分离；snap 身份只含 span/worker/model；gather 不再读明细行。
# 金额 / token 精度与筛选范围未动。
_COST_PY_SHA_AT_BASELINE = "6dd70a7223bd8e19331c4748c682732fbc1e802c"


def test_cost_py_bytes_frozen():
    """新增字段是允许的；口径不得变。SHA 更新时必须在上一行注释里写变更范围。"""

    current = hashlib.sha1(_COST_PY.read_bytes()).hexdigest()
    assert current == _COST_PY_SHA_AT_BASELINE, (
        f"rogmem/cost.py 被改了；当前 sha1={current}。"
        " 本次改造仅允许最小必要新增字段；若动了口径必须由用户确认后更新 SHA。"
    )


# ----------------------------- PR #110 回归（一句话抽样）-----------------------------


def test_filter_chain_hooks_unchanged():
    html = cost_page(_board(), [], {})
    assert "data-cost-filter" in html
    assert "saveCostFilter" in html
    assert "/admin/api/costs/filters" in html


def test_call_table_keeps_exact_figures_the_chart_does_not_preprint():
    """明细表继续写精确 token 和金额。柱图脚本不再预写这些个位数字。"""

    html = cost_page(
        _board(
            rows=[
                {
                    "at": "2026-11-11 11:00",
                    "worker": "草稿",
                    "model": "演示",
                    "prompt": "问",
                    "answer": "答",
                    "input": 2_345_678,
                    "output": 9,
                    "cache": 1,
                    "total": 2_345_688,
                    "money": 1234.5,
                }
            ]
        ),
        [],
        {},
    )
    table = html.split('<table class="calls">', 1)[1].split("</table>", 1)[0]
    assert "2,345,678" in table
    assert "2,345,688" in table
    assert "¥1,234.50" in table
    from rogmem.ui import _CHART_SCRIPT

    assert "function fmtMillion" in _CHART_SCRIPT
    assert "2,345,678" not in _CHART_SCRIPT
    assert "¥1,234.50" not in _CHART_SCRIPT
