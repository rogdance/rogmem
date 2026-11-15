"""Default admin views must not block on a fresh gather once a snapshot exists."""

from __future__ import annotations

import threading
import time

import pytest

from rogmem import cost, engine


def _request_only(calls: dict, message: str):
    """The refresher thread may still be alive from another test. Only the request thread counts."""

    here = threading.get_ident()

    def boom(*_args, **_kwargs):
        if threading.get_ident() != here:
            return None
        calls["n"] += 1
        raise AssertionError(message)

    return boom


@pytest.fixture(autouse=True)
def _isolate_cost_observations():
    cost._clear_observations()
    yield
    cost._clear_observations()


def test_operation_log_view_serves_existing_snap_without_refresh(monkeypatch):
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise AssertionError("request path must not rebuild when a snap exists")

    monkeypatch.setattr(engine, "_refresh_operation_log_snap", boom)
    with engine._LOG_SNAP_LOCK:
        engine._LOG_SNAP["at"] = time.time() - 30
        engine._LOG_SNAP["entries"] = [
            {
                "at": "2099-01-01T00:00:00+00:00",
                "id": "synth-1",
                "kind": "蒸馏",
                "outcome": "ok",
                "subject": "合成主体甲",
                "summary": "虚构条目仅供单测",
                "href": "",
                "detail": "",
                "step": "",
            }
        ]
        engine._LOG_SNAP["kinds"] = list(engine.RUN_KINDS)
    got = engine.operation_log_view()
    assert calls["n"] == 0
    assert got["entries"][0]["subject"] == "合成主体甲"


def test_board_and_prices_serves_existing_snap_without_refresh(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(cost, "_refresh_board_snap", _request_only(calls, "request path must not rebuild when a snap exists"))
    board = {"summary": {"cny": 0}, "rows": [], "series": [], "observed": True}
    prices = {("synth", "model-a"): cost.Price(currency="CNY", input=1, output=2, cache=0)}
    filtered = cost._normalize_board_kwargs({})
    cost._clear_observations()
    cost._remember_observation(cost._board_snap_key(filtered), board, prices)
    got_board, got_prices = cost.board_and_prices()
    assert calls["n"] == 0
    assert got_board is board
    assert got_prices is prices


def test_board_and_prices_cold_opens_shell_without_refresh(monkeypatch):
    """Cold console view must not block on a ledger gather under call writers."""

    calls = {"n": 0}
    monkeypatch.setattr(
        cost, "_refresh_board_snap", _request_only(calls, "request path must not rebuild when the snap is cold")
    )
    monkeypatch.setattr(cost, "_start_board_refresher", lambda: None)
    cost._clear_observations()
    got_board, got_prices = cost.board_and_prices(span="24h", detail="24h")
    assert calls["n"] == 0
    assert got_prices == {}
    assert got_board["observed"] is False
    assert got_board["span"] == "24h"
    assert got_board["detail"] == "24h"
    assert got_board["count"] == 0
    assert got_board["rows"] == []
    assert got_board["series"] == []
    assert got_board["summary"]["tokens"] == 0
    assert got_board["window"]["grain"] == "hour"
    assert got_board["window"]["start"]
    assert got_board["window"]["end"]


def test_board_and_prices_mismatched_snap_opens_shell_without_live(monkeypatch):
    """A snap for another filter combo must not force a live gather on the request path."""

    calls = {"n": 0}
    here = threading.get_ident()

    def store_boom(*_args, **_kwargs):
        if threading.get_ident() != here:
            raise RuntimeError("ledger is busy")
        raise AssertionError("request path must not open the ledger on a filter miss")

    monkeypatch.setattr(cost, "_refresh_board_snap", _request_only(calls, "request path must not rebuild on a filter miss"))
    monkeypatch.setattr(cost, "_start_board_refresher", lambda: None)
    monkeypatch.setattr(cost, "_store", store_boom)
    other = cost._normalize_board_kwargs({"span": "30d", "detail": "30d"})
    cost._clear_observations()
    cost._remember_observation(
        cost._board_snap_key(other),
        {"summary": {"tokens": 9}, "rows": [], "series": [], "observed": True},
        {("synth", "m"): cost.Price(currency="CNY", input=1, output=2, cache=0)},
    )
    got_board, got_prices = cost.board_and_prices(span="24h", detail="24h")
    assert calls["n"] == 0
    assert got_prices == {}
    assert got_board["observed"] is False
    assert got_board["span"] == "24h"
    assert got_board["rows"] == []
    assert got_board.get("summary", {}).get("tokens") != 9
