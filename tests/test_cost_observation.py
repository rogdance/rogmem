"""成本视图按身份观测。未观测不是零用量，换筛选不能把另一份观测抹掉。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from rogmem import cost
from rogmem.ui import cost_page


@pytest.fixture(autouse=True)
def _isolate_cost_observations():
    cost._clear_observations()
    yield
    cost._clear_observations()


def _reset_views() -> None:
    with cost._BOARD_SNAP_LOCK:
        if hasattr(cost, "_BOARD_SNAP"):
            cost._BOARD_SNAP["at"] = 0.0
            cost._BOARD_SNAP["key"] = None
            cost._BOARD_SNAP["board"] = None
            cost._BOARD_SNAP["prices"] = None
        observed = getattr(cost, "_BOARD_OBS", None)
        if isinstance(observed, dict):
            observed.clear()
        cost._BOARD_SNAP_WANT = None
    wake = getattr(cost, "_BOARD_WAKE", None)
    if wake is not None:
        wake.clear()


def _fragment(html: str, marker: str) -> str:
    start = html.find(marker)
    assert start >= 0, marker
    return html[start : html.find("</div>", start)]


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
        "workers": [],
        "models": [],
        "rate": None,
    }
    board.update(changes)
    return board


def test_missing_view_is_not_a_zero_reading(monkeypatch):
    """没有这份视图的观测时，读到的是未观测，不是一条全零的用量。"""

    here = threading.get_ident()

    def boom(*_args, **_kwargs):
        if threading.get_ident() != here:
            return None
        raise AssertionError("request path gathered")

    monkeypatch.setattr(cost, "_refresh_board_snap", boom)
    monkeypatch.setattr(cost, "_start_board_refresher", lambda: None)
    _reset_views()
    board, prices = cost.board_and_prices(span="7d", detail="30d")
    assert prices == {}
    assert board["observed"] is False
    assert board["series"] == []
    assert board["rows"] == []
    assert board["span"] == "7d"


def test_one_view_stays_after_another_is_published(monkeypatch):
    """发布 24h 不能让已经观测过的 30d 消失。请求路径不再打开账本。"""

    def overview(_con, **kwargs):
        span = kwargs["span"]
        tokens = 24 if span == "24h" else 30
        return {
            "span": span,
            "summary": {"tokens": tokens},
            "rows": [],
            "series": [{"tokens": tokens}],
            "count": 1,
            "observed": True,
        }

    class _Ctx:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    here = threading.get_ident()
    real_refresh = cost._refresh_board_snap

    def quiet_refresh(target=None):
        if threading.get_ident() != here:
            return None
        return real_refresh(target)

    monkeypatch.setattr(cost, "_overview", overview)
    monkeypatch.setattr(cost, "_prices", lambda _con: {})
    monkeypatch.setattr(cost, "_store", lambda write=False: _Ctx())
    monkeypatch.setattr(cost, "_refresh_board_snap", quiet_refresh)
    monkeypatch.setattr(cost, "_start_board_refresher", lambda: None)
    _reset_views()
    quiet_refresh({"span": "30d", "detail": "30d", "page": 1})
    quiet_refresh({"span": "24h", "detail": "24h", "page": 1})

    def boom(*_args, **_kwargs):
        if threading.get_ident() != here:
            return None
        raise AssertionError("request path gathered")

    monkeypatch.setattr(cost, "_overview", boom)
    monkeypatch.setattr(cost, "_store", boom)
    month, _prices = cost.board_and_prices(span="30d", detail="30d")
    day, _prices = cost.board_and_prices(span="24h", detail="24h")
    assert month["summary"]["tokens"] == 30
    assert day["summary"]["tokens"] == 24
    assert month["observed"] is True
    assert day["observed"] is True


def test_identity_change_signals_gather_once(monkeypatch):
    """新身份立刻叫醒汇总；同一身份的重复读取不再次叫醒，避免空转打账本。"""

    monkeypatch.setattr(cost, "_start_board_refresher", lambda: None)
    monkeypatch.setattr(cost, "_refresh_board_snap", lambda *_args, **_kwargs: None)
    _reset_views()
    with cost._BOARD_SNAP_LOCK:
        cost._BOARD_SNAP_WANT = {
            "span": "30d",
            "worker": "",
            "model": "",
            "detail": "30d",
            "dworker": "",
            "dmodel": "",
            "page": 1,
        }
    board, _prices = cost.board_and_prices(span="7d", detail="30d")
    assert board["observed"] is False
    assert cost._BOARD_WAKE.is_set()
    cost._BOARD_WAKE.clear()
    cost.board_and_prices(span="7d", detail="30d")
    assert not cost._BOARD_WAKE.is_set()


def test_hover_frame_is_the_bar_itself():
    """选中框就是柱子的描边，不再另画一个矩形。"""

    html = cost_page(_board(observed=True), [], {})
    start = html.find("function highlight")
    body = html[start : html.find("function setActive", start)]
    assert "rect.cursor" not in body
    assert "L.hitH" not in body
    assert "rect.bar.on" in html
    assert "clip-path: inset(0)" in html


def test_unobserved_board_is_not_painted_as_zero_usage():
    board = _board(
        span="24h",
        observed=False,
        window={
            "start": "2026-11-11T03:00:00+00:00",
            "end": "2026-11-12T03:00:00+00:00",
            "grain": "hour",
            "tz": "UTC+8",
        },
    )
    html = cost_page(board, [], {})
    money = _fragment(html, 'id="ov-money-box"')
    token = _fragment(html, 'id="ov-token-box"')
    assert "正在汇总" in money
    assert "money gathering" in money
    assert "¥" not in money
    assert "正在汇总" in token
    assert "money gathering" in token
    assert "0 百万" not in token
    assert "还没有调用" not in html
    assert "共 0 条" not in html
    assert "正在汇总这一时段" in html
    assert 'data-cost-observed="0"' in html
    assert '"observed": false' in html
    assert html.find("COST.observed === false") < html.find("所选时段无数据")
    assert "observe=1" in html
    assert "location.reload" in html
    assert 'id="open-prices" disabled' in html


def test_observed_zero_still_reads_as_zero():
    html = cost_page(_board(observed=True), [], {})
    assert "共 0 条" in html
    assert "money gathering" not in html
    assert "¥0.00" in html
    assert "X-Cost-Observe" not in html
    assert 'id="open-prices" disabled' not in html
    assert 'data-cost-observed="0"' not in html
    assert "所选时段无数据" in html or "所选时段总用量" in html


def test_switch_response_waits_until_that_view_is_gathered(tmp_path, monkeypatch):
    """切换后的第一次页面要带上这份视图的用量，不能先交一张空壳再指望旁路把它换掉。"""

    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    _reset_views()
    moment = datetime.now(timezone.utc)
    cost.record(
        at=(moment - timedelta(hours=1)).isoformat(),
        worker="草稿",
        provider_id="p",
        model="m",
        usage=(2_500_000, 0, 0),
        prompt="近一次",
        answer="",
    )
    real = cost._overview

    def slow(_con, **kwargs):
        import time

        time.sleep(0.3)
        return real(_con, **kwargs)

    monkeypatch.setattr(cost, "_overview", slow)
    from starlette.testclient import TestClient

    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    resp = client.get("/admin/costs?span=7d&detail=30d")
    assert resp.status_code == 200
    money = _fragment(resp.text, 'id="ov-money-box"')
    token = _fragment(resp.text, 'id="ov-token-box"')
    assert "正在汇总" not in money
    assert "2.50 百万" in token
    assert "近一次" in resp.text
    assert resp.headers["cache-control"] == "no-store"


def test_observe_query_reports_the_same_view(tmp_path, monkeypatch):
    """汇总进度走页面自己的地址，不依赖会被入口丢掉的请求头。"""

    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    _reset_views()
    cost._refresh_board_snap({"span": "24h", "detail": "30d", "page": 1})
    from starlette.testclient import TestClient

    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    resp = client.get("/admin/costs?span=24h&detail=30d&observe=1")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["state"] in {"ready", "stale"}


def test_switch_then_reread_shows_each_view(tmp_path, monkeypatch):
    """先看到 30 天的用量，切到 24 小时不能变成 0；24 小时汇总完成后，30 天仍在。"""

    main = threading.get_ident()
    real_refresh = cost._refresh_board_snap

    def gated(target=None):
        if threading.get_ident() != main:
            return None
        return real_refresh(target)

    monkeypatch.setattr(cost, "_refresh_board_snap", gated)
    monkeypatch.setattr(cost, "VIEW_WAIT_SECONDS", 0)
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    moment = datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)
    cost.record(
        at="2026-11-12T02:00:00+00:00",
        worker="草稿",
        provider_id="p",
        model="m",
        usage=(2_500_000, 0, 0),
        prompt="近一次",
        answer="",
    )
    cost.record(
        at="2026-11-01T02:00:00+00:00",
        worker="草稿",
        provider_id="p",
        model="m",
        usage=(5_000_000, 0, 0),
        prompt="远一次",
        answer="",
    )
    _reset_views()

    def publish(span: str) -> None:
        real_refresh({"now": moment, "span": span, "detail": "30d", "page": 1})

    from starlette.testclient import TestClient

    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    publish("30d")
    warm = client.get("/admin/costs?span=30d")
    assert warm.status_code == 200
    assert "7.50 百万" in warm.text
    assert "正在汇总" not in _fragment(warm.text, 'id="ov-money-box"')

    pending = client.get("/admin/costs?span=24h")
    assert pending.status_code == 200
    assert "span=24h" in pending.text
    money = _fragment(pending.text, 'id="ov-money-box"')
    assert "正在汇总" in money
    assert "¥" not in money
    assert "2.50 百万" not in pending.text
    assert "7.50 百万" not in pending.text
    assert pending.headers["cache-control"] == "no-store"
    probe = client.get("/admin/costs?span=24h", headers={"X-Cost-Observe": "1"})
    assert probe.json()["state"] == "pending"
    assert probe.json()["observed"] is False

    publish("24h")
    probe = client.get("/admin/costs?span=24h", headers={"X-Cost-Observe": "1"})
    assert probe.json()["state"] in {"ready", "stale"}
    fresh = client.get("/admin/costs?span=24h")
    assert "2.50 百万" in fresh.text
    assert "7.50 百万" not in fresh.text
    assert "正在汇总" not in _fragment(fresh.text, 'id="ov-money-box"')
    again = client.get("/admin/costs?span=30d")
    assert "7.50 百万" in again.text
    assert "正在汇总" not in _fragment(again.text, 'id="ov-money-box"')
