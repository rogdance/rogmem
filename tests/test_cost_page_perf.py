"""成本页读模型分离：卡片身份不含明细；翻页/改明细不折卡片。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from rogmem import cost


@pytest.fixture(autouse=True)
def _isolate_cost_observations():
    cost._clear_observations()
    yield
    cost._clear_observations()


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    return engine


def test_card_identity_ignores_detail_filters():
    left = cost._board_snap_key(
        cost._normalize_board_kwargs({"span": "30d", "detail": "30d", "dworker": "草稿", "page": 2})
    )
    right = cost._board_snap_key(
        cost._normalize_board_kwargs({"span": "30d", "detail": "24h", "dworker": "并页", "page": 9})
    )
    other = cost._board_snap_key(cost._normalize_board_kwargs({"span": "7d", "detail": "30d"}))
    assert left == right
    assert left != other


def test_overview_gather_does_not_read_call_rows(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    cost.record(
        at=(now - timedelta(hours=1)).isoformat(),
        worker="草稿",
        provider_id="p",
        model="m",
        usage=(10, 0, 0),
        prompt="卡片材料",
        answer="卡片答复",
    )

    def boom(*_args, **_kwargs):
        raise AssertionError("card gather must not read call rows")

    monkeypatch.setattr(cost, "_detail_rows", boom)
    with cost._store() as con:
        board = cost._overview(con, now=now, span="24h", detail="24h", page=1)
    assert board["summary"]["tokens"] == 10
    assert board["rows"] == []
    assert board["count"] == 0


def test_detail_partial_does_not_wait_on_cards(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    cost.record(
        at=(now - timedelta(hours=1)).isoformat(),
        worker="草稿",
        provider_id="p",
        model="m",
        usage=(10, 0, 0),
        prompt="列表材料",
        answer="列表答复",
    )
    monkeypatch.setattr(cost, "VIEW_WAIT_SECONDS", 2.0)

    def stuck(*_args, **_kwargs):
        raise AssertionError("detail partial must not wait for card gather")

    monkeypatch.setattr(cost, "wait_for_view", stuck)
    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    resp = client.get("/admin/costs?span=30d&detail=24h&part=calls")
    assert resp.status_code == 200
    assert "列表材料" in resp.text
    assert "list-foot" in resp.text


def test_overview_partial_returns_card_json(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    moment = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    cost.record(
        at=(moment - timedelta(hours=1)).isoformat(),
        worker="草稿",
        provider_id="p",
        model="m",
        usage=(2_500_000, 0, 0),
        prompt="近一次",
        answer="",
    )
    cost._refresh_board_snap({"now": moment, "span": "24h", "worker": "", "model": ""})
    monkeypatch.setattr(cost, "VIEW_WAIT_SECONDS", 0)
    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    resp = client.get("/admin/costs?span=24h&detail=30d&part=overview")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["observed"] is True
    assert "2.50 百万" in body["token_text"]
    assert body["chart"]["series"]
    assert "近一次" not in resp.text


def test_pending_cards_still_show_call_rows(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    moment = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    cost.record(
        at=(moment - timedelta(hours=1)).isoformat(),
        worker="草稿",
        provider_id="p",
        model="m",
        usage=(10, 0, 0),
        prompt="仍可见",
        answer="",
    )
    monkeypatch.setattr(cost, "VIEW_WAIT_SECONDS", 0)
    monkeypatch.setattr(cost, "_start_board_refresher", lambda: None)
    monkeypatch.setattr(cost, "_refresh_board_snap", lambda *_a, **_k: None)
    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    resp = client.get("/admin/costs?span=7d&detail=30d")
    assert resp.status_code == 200
    money = resp.text[resp.text.find('id="ov-money-box"') : resp.text.find('id="ov-money-box"') + 200]
    assert "正在汇总" in money
    assert "仍可见" in resp.text
