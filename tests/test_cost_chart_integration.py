"""成本柱图——真数据端到端断言。

直接走 `engine.connect` + `cost.record` + 真实价格/汇率 + `cost.overview(now=...)` 产生 board，
再喂给 `ui.cost_page`，对比 **真实 `span_bounds` 的查询边界** 与 payload.window 是否完全一致。

不手工造整点 series，不构造任何伪窗口；now 冻结到 `2026-11-11 03:37:42.123Z`
（= Asia/Shanghai 11:37:42.123）以验证首/末小时都不是整点的场景。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rogmem import cost


NOW = datetime(2026, 11, 11, 3, 37, 42, 123_000, tzinfo=timezone.utc)
_PAYLOAD_RE = re.compile(r"var COST = (\{.*?\});", re.DOTALL)


def _payload(html: str) -> dict:
    m = _PAYLOAD_RE.search(html)
    assert m, "cost_page 必须注入 var COST = {...}"
    return json.loads(m.group(1))


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine as eng

    eng.connect(tmp_path, tmp_path / "scan")
    return eng


def _seed_realistic_calls(provider="p"):
    """Record a handful of 真实(非整点、跨日、含未计价、多模型) model calls."""

    # 跨 24h 窗口的若干非整点时刻
    moments = [
        "2026-11-10T06:15:33+00:00",  # 14:15:33 UTC+8
        "2026-11-10T10:50:11+00:00",
        "2026-11-10T22:50:11+00:00",  # 06:50:11 次日 UTC+8
        "2026-11-11T01:02:00+00:00",
        "2026-11-11T03:01:11+00:00",
        "2026-11-11T03:36:00+00:00",  # 11:36 UTC+8 — 和 now 同一小时桶
    ]
    for i, at in enumerate(moments):
        cost.record(
            at=at,
            worker="草稿" if i % 2 else "并页",
            provider_id=provider,
            model="paid" if i < 4 else "unpaid",
            usage=(1_000 + i * 123, 400 + i * 71, 50 * (i % 3)),
            prompt=f"材料 {i}",
            answer=f"回复 {i}",
        )
    # 老历史，用来让 span="all" 的 earliest 是真实非整点
    cost.record(
        at="2024-03-07T18:47:13+00:00",
        worker="速写",
        provider_id=provider,
        model="paid",
        usage=(200, 80, 0),
        prompt="旧",
        answer="旧",
    )


def _price_only_paid(provider="p"):
    cost.save_book(
        "7.2",
        [
            {"provider": provider, "model": "paid", "currency": "USD", "input": "2", "output": "3", "cache": "0.5"},
        ],
    )


# -----------------------------------------------------------------------
# P1：payload.window 严格 == cost.span_bounds(span, now, earliest) 的真实边界
# -----------------------------------------------------------------------


@pytest.mark.parametrize("span", ["24h", "7d", "30d", "all"])
def test_payload_window_strictly_equals_span_bounds(engine, span):
    _seed_realistic_calls()
    _price_only_paid()
    board = cost.overview(now=NOW, span=span)
    # board.window 必须等于 cost.span_bounds 的真实返回；不可被重新推算。
    from rogmem import cost as cost_mod
    from rogmem.cost import _store

    with _store() as con:
        earliest = cost_mod._earliest_at(con)
    expected_start, expected_grain = cost_mod.span_bounds(span, NOW, earliest)
    assert board["window"] == {
        "start": expected_start.isoformat(),
        "end": NOW.isoformat(),
        "grain": expected_grain,
        "tz": "UTC+8",
    }, f"span={span} 的 overview.window 必须 == span_bounds 真实边界"


def test_payload_window_survives_cost_page(engine):
    _seed_realistic_calls()
    _price_only_paid()
    board = cost.overview(now=NOW, span="24h")
    from rogmem.ui import cost_page

    html = cost_page(board, [], {})
    data = _payload(html)
    assert data["window"]["start"] == (NOW - timedelta(hours=24)).isoformat()
    assert data["window"]["end"] == NOW.isoformat()
    assert data["window"]["grain"] == "hour"
    assert data["window"]["tz"] == "UTC+8"
    # 微秒信息也必须保留（11:37:42.123，不是 11:37:00）
    assert ".123" in data["window"]["end"]


# -----------------------------------------------------------------------
# P1 衍生：summary 口径与 series 守恒；展示桶合并对齐 _public_sum
# -----------------------------------------------------------------------


def test_summary_tokens_equal_series_sum_real_overview(engine):
    _seed_realistic_calls()
    _price_only_paid()
    board = cost.overview(now=NOW, span="24h")
    total_tokens = sum(item["tokens"] for item in board["series"])
    assert board["summary"]["tokens"] == total_tokens


def test_summary_money_is_none_when_any_call_unpriced(engine):
    _seed_realistic_calls()
    _price_only_paid()  # 只给 model="paid" 定价；model="unpaid" 没价 → 窗口内 unpriced>0
    board = cost.overview(now=NOW, span="24h")
    assert board["summary"]["money"] is None, (
        "任意未计价调用 → summary.money 必须是 None（_public_sum 口径）"
    )


def test_mixed_single_bucket_preserves_known_money(engine):
    """24h 同一个小时桶里一条 paid + 一条 unpaid，n<maxBars 不触发合并。
    桶级 money=None（_public_sum 口径），但 money_known 必须保留已计价金额；前端 payload 原样透传。"""

    # 冻结到 2026-11-11 03:37:42.123Z；两条调用都落到 03:00Z 整点桶（Shanghai 11:00 整点桶）。
    cost.record(at="2026-11-11T03:10:00+00:00", worker="草稿", provider_id="p", model="paid", usage=(1_000_000, 500_000, 0), prompt="已计价", answer="")
    cost.record(at="2026-11-11T03:20:00+00:00", worker="草稿", provider_id="p", model="unpaid", usage=(100_000, 50_000, 0), prompt="未计价", answer="")
    _price_only_paid()
    board = cost.overview(now=NOW, span="24h")
    # 找到含两条 call 的那一桶
    bucket = next(item for item in board["series"] if item["unpriced"] > 0 and item["tokens"] > 0)
    # cost._public_sum R3：money=None 但 money_known>0 必须被保留
    assert bucket["money"] is None
    assert bucket["money_known"] > 0, f"单桶混合必须保留已计价金额，实际 {bucket}"
    # payload 到前端
    from rogmem.ui import cost_page

    html = cost_page(board, [], {})
    data = _payload(html)
    payload_item = next(item for item in data["series"] if item["unpriced"] > 0 and item["tokens"] > 0)
    assert payload_item["money"] is None
    assert payload_item["money_known"] == pytest.approx(bucket["money_known"]), (
        "payload.series 必须透传 money_known，不得静默丢失已计价部分"
    )


def test_summary_money_known_is_sum_of_series_money_known(engine):
    """summary 卡片的 money_known 必须 == sum(series.money_known)。"""

    _seed_realistic_calls()
    _price_only_paid()
    board = cost.overview(now=NOW, span="24h")
    total_known = sum(item.get("money_known") or 0.0 for item in board["series"])
    assert board["summary"]["money_known"] == pytest.approx(total_known)
    # 并且 summary.money 仍遵守 _public_sum: 有 unpriced → None
    assert board["summary"]["money"] is None
    assert board["summary"]["money_known"] > 0


def test_series_bare_is_transported_to_payload(engine):
    # bare 字段（没有 usage 返回的调用）必须随 payload 透传，否则合并桶 bare 会丢。
    cost.record(
        at="2026-11-11T02:50:00+00:00",
        worker="草稿",
        provider_id="p",
        model="bare-model",
        usage=None,
        prompt="没有 usage",
        answer="",
    )
    board = cost.overview(now=NOW, span="24h")
    from rogmem.ui import cost_page

    html = cost_page(board, [], {})
    data = _payload(html)
    assert any(item.get("bare", 0) > 0 for item in data["series"]), (
        "series 必须把后端的 bare 原样透传到前端；否则'合并守恒'口径不成立"
    )


# -----------------------------------------------------------------------
# P2：金额精度自适应
# -----------------------------------------------------------------------


def test_chart_axis_formatter_is_millions_only():
    from rogmem.ui import _CHART_SCRIPT

    # 走势只说百万。精确到个位的计数、K/M/B 和科学计数都不进轴或柱。
    assert "function fmtMillion" in _CHART_SCRIPT
    assert "function formatAxisValue(v, u)" in _CHART_SCRIPT
    assert ' + " 百万"' in _CHART_SCRIPT
    assert "toExponential" not in _CHART_SCRIPT
    assert "stepDigitsFor" not in _CHART_SCRIPT
    assert "fmtTokensCaption" not in _CHART_SCRIPT
    assert "fmtTokensCount" not in _CHART_SCRIPT
    assert "fmtTokensShort" not in _CHART_SCRIPT
    assert '+ "M"' not in _CHART_SCRIPT
    assert '+ "K"' not in _CHART_SCRIPT
    assert '+ "B"' not in _CHART_SCRIPT
    # 纵轴留白仍按真实刻度文字测宽，不退回固定样本。
    assert "measureTextWidth" in _CHART_SCRIPT
    assert "widestTickWidth" in _CHART_SCRIPT
    assert "longestYLabel" not in _CHART_SCRIPT
