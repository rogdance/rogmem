"""观测的身份和新鲜度是两件事。命中旧快照不能当成已经看完。"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from rogmem import cost
from rogmem.ui import cost_page


NOW = datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 11, 12, 4, 10, tzinfo=timezone.utc)


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


def _quiet_background(monkeypatch):
    real = cost._refresh_board_snap

    def wrapped(target=None):
        if threading.current_thread().name == "rogmem-cost-snap":
            return None
        return real(target)

    monkeypatch.setattr(cost, "_refresh_board_snap", wrapped)
    return real


def _view(span: str, now: datetime = NOW):
    return {"span": span, "detail": "30d", "page": 1, "now": now}


def _state(board: dict) -> str:
    return str((board.get("observation") or {}).get("state") or "")


def _fragment(html: str, marker: str) -> str:
    start = html.find(marker)
    assert start >= 0, marker
    return html[start : html.find("</div>", start)]


def _seed_call(at: str = "2026-11-12T02:00:00+00:00", tokens: int = 2_500_000) -> None:
    cost.record(
        at=at,
        worker="初始蒸馏",
        provider_id="p",
        model="m",
        usage=(tokens, 0, 0),
        prompt="材料",
        answer="",
    )


def _price(value: str, rate: str = "7") -> None:
    cost.save_book(
        rate,
        [{"provider": "p", "model": "m", "currency": "CNY", "input": value, "output": "0", "cache": "0"}],
    )


def test_price_change_is_not_a_finished_old_reading(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    _quiet_background(monkeypatch)
    _seed_call()
    _price("2")
    cost._refresh_board_snap(_view("24h"))
    first, old_prices = cost.board_and_prices(**_view("24h"))
    assert first["summary"]["money"] == pytest.approx(5)
    assert old_prices[("p", "m")].input == 2

    _price("4", rate="8")
    again, new_prices = cost.board_and_prices(**_view("24h"))
    assert _state(again) == "stale"
    assert again["summary"]["money"] == pytest.approx(5)
    assert new_prices[("p", "m")].input == 4
    assert again["rate"] == 8

    cost._refresh_board_snap(_view("24h"))
    done, prices = cost.board_and_prices(**_view("24h"))
    assert _state(done) == "ready"
    assert done["summary"]["money"] == pytest.approx(10)
    assert prices[("p", "m")].input == 4


def test_new_call_makes_the_cached_view_stale_until_regathered(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    _quiet_background(monkeypatch)
    _seed_call(tokens=100)
    cost._refresh_board_snap(_view("24h"))
    cost._refresh_board_snap(_view("30d"))
    _seed_call(at="2026-11-12T02:30:00+00:00", tokens=50)

    stale, _prices = cost.board_and_prices(**_view("24h"))
    assert _state(stale) == "stale"
    assert stale["summary"]["tokens"] == 100
    other, _prices = cost.board_and_prices(**_view("30d"))
    assert _state(other) == "stale"
    assert other["summary"]["tokens"] == 100

    cost._refresh_board_snap(_view("24h"))
    fresh, _prices = cost.board_and_prices(**_view("24h"))
    assert _state(fresh) == "ready"
    assert fresh["summary"]["tokens"] == 150
    held, _prices = cost.board_and_prices(**_view("30d"))
    assert held["summary"]["tokens"] == 100
    assert _state(held) == "stale"


def test_rolling_window_is_not_a_finished_reading(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    _quiet_background(monkeypatch)
    _seed_call()
    cost._refresh_board_snap(_view("24h", NOW))
    moved, _prices = cost.board_and_prices(**_view("24h", LATER))
    assert _state(moved) == "stale"
    cost._refresh_board_snap(_view("24h", LATER))
    done, _prices = cost.board_and_prices(**_view("24h", LATER))
    assert _state(done) == "ready"
    assert done["window"]["end"].startswith("2026-11-12T04:10")


def test_true_zero_stays_ready_and_missing_stays_pending(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    _quiet_background(monkeypatch)
    missing, _prices = cost.board_and_prices(**_view("24h"))
    assert _state(missing) == "pending"
    missing_html = cost_page(missing, [], {})
    assert "正在汇总" in _fragment(missing_html, 'id="ov-money-box"')
    assert "¥" not in _fragment(missing_html, 'id="ov-money-box"')

    cost._refresh_board_snap(_view("24h"))
    zero, _prices = cost.board_and_prices(**_view("24h"))
    assert _state(zero) == "ready"
    assert zero["summary"]["tokens"] == 0
    zero_html = cost_page(zero, [], {})
    assert "¥0.00" in _fragment(zero_html, 'id="ov-money-box"')
    assert "X-Cost-Observe" not in zero_html


def test_older_gather_cannot_overwrite_a_newer_one(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    real = _quiet_background(monkeypatch)
    started = threading.Event()
    release = threading.Event()
    phase = {"old": True}

    def overview(_con, **kwargs):
        if phase["old"]:
            started.set()
            assert release.wait(3)
            return {"summary": {"tokens": 1, "money": 1.0}, "observed": True, "series": [], "rows": [], "count": 1}
        return {"summary": {"tokens": 2, "money": 2.0}, "observed": True, "series": [], "rows": [], "count": 1}

    monkeypatch.setattr(cost, "_overview", overview)
    worker = threading.Thread(target=lambda: real(_view("24h")), name="old-gather")
    worker.start()
    assert started.wait(2)
    phase["old"] = False
    _seed_call(tokens=1)
    real(_view("24h"))
    release.set()
    worker.join(3)
    board, _prices = cost.board_and_prices(**_view("24h"))
    assert board["summary"]["tokens"] == 2


def test_cached_identity_change_schedules_the_refresher(monkeypatch):
    calls = []
    started = threading.Event()

    def fake(target=None):
        span = "30d"
        if isinstance(target, dict) and target.get("span"):
            span = str(target["span"])
        calls.append(span)
        if len(calls) == 1:
            started.set()
        return None

    monkeypatch.setattr(cost, "_refresh_board_snap", fake)
    moment = NOW
    filtered = cost._normalize_board_kwargs(_view("24h", moment))
    cost._remember_observation(
        cost._board_snap_key(filtered),
        {
            "summary": {"tokens": 1, "money": 1},
            "series": [],
            "rows": [],
            "observed": True,
            "window": {"grain": "hour", "end": moment.isoformat(), "start": moment.isoformat(), "tz": "UTC+8"},
            "rate": None,
        },
        {},
    )
    cost._BOARD_WAKE.set()
    cost._start_board_refresher()
    assert started.wait(2)
    cost._BOARD_WAKE.clear()
    time.sleep(0.05)
    before = len(calls)
    cost.board_and_prices(**_view("24h", moment))
    deadline = time.time() + 1
    while time.time() < deadline and len(calls) == before:
        time.sleep(0.02)
    assert len(calls) > before


def test_lock_is_not_failure_or_a_zero_and_then_recovers(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    _quiet_background(monkeypatch)
    real_store = cost._store
    _seed_call(tokens=80)
    cost._refresh_board_snap(_view("24h"))
    locked = {"left": 2}

    def flaky(*args, **kwargs):
        if locked["left"] > 0:
            locked["left"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real_store(*args, **kwargs)

    monkeypatch.setattr(cost, "_store", flaky)
    first = cost._refresh_board_snap(_view("24h"))
    held, _prices = cost.board_and_prices(**_view("24h"))
    assert _state(held) != "failed"
    assert held["summary"]["tokens"] == 80
    second = cost._refresh_board_snap(_view("24h"))
    third = cost._refresh_board_snap(_view("24h"))
    done, _prices = cost.board_and_prices(**_view("24h"))
    assert third in (None, "ok")
    assert _state(done) == "ready"
    assert done["summary"]["tokens"] == 80
    assert first in (None, "locked")
    assert second in (None, "locked", "ok")


def test_persistent_gather_failure_stops_instead_of_polling_forever(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    _quiet_background(monkeypatch)

    def boom(*_args, **_kwargs):
        raise RuntimeError("汇总坏了")

    monkeypatch.setattr(cost, "_overview", boom)
    for _ in range(3):
        cost._refresh_board_snap(_view("24h"))
    board, _prices = cost.board_and_prices(**_view("24h"))
    assert _state(board) == "failed"
    html = cost_page(board, [], {})
    assert "X-Cost-Observe" not in html
    assert "¥" not in _fragment(html, 'id="ov-money-box"')
    calls = {"n": 0}

    def count(*_args, **_kwargs):
        calls["n"] += 1
        raise RuntimeError("还在打")

    monkeypatch.setattr(cost, "_overview", count)
    cost._refresh_board_snap(_view("24h"))
    assert calls["n"] == 0


def test_price_post_then_get_ends_on_the_new_amount(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    real = _quiet_background(monkeypatch)
    moment = datetime.now(timezone.utc)
    _seed_call(at=(moment - timedelta(hours=1)).isoformat())
    _price("2")
    real({"span": "24h", "detail": "30d", "page": 1, "now": moment})
    from starlette.testclient import TestClient

    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    saved = client.post(
        "/admin/api/costs",
        json={"rate": "8", "items": [{"provider": "p", "model": "m", "currency": "CNY", "input": "4", "output": "0", "cache": "0"}]},
    )
    assert saved.status_code == 200
    url = "/admin/costs?span=24h&detail=30d"
    probe = client.get(url, headers={"X-Cost-Observe": "1"})
    body = client.get(url).text
    assert probe.json()["state"] == "stale"
    assert "value=\"8\"" in body
    assert 'data-cost-state="stale"' in body
    real({"span": "24h", "detail": "30d", "page": 1, "now": datetime.now(timezone.utc)})
    probe = client.get(url, headers={"X-Cost-Observe": "1"})
    body = client.get(url).text
    assert probe.json()["state"] == "ready"
    assert "¥10.00" in _fragment(body, 'id="ov-money-box"')
    assert "X-Cost-Observe" not in body


def test_filter_click_sets_leave_before_the_post_and_failure_resumes():
    html = cost_page(
        {
            "span": "24h",
            "detail": "30d",
            "worker": "",
            "model": "",
            "dworker": "",
            "dmodel": "",
            "page": 1,
            "pages": 1,
            "count": 1,
            "observed": True,
            "summary": {"money": 5, "tokens": 100, "input": 100, "output": 0, "cache": 0, "unpriced": 0, "bare": 0},
            "series": [],
            "rows": [],
            "workers": [],
            "models": [],
            "rate": 7,
            "window": {
                "start": "2026-11-11T03:00:00+00:00",
                "end": "2026-11-12T03:00:00+00:00",
                "grain": "hour",
                "tz": "UTC+8",
            },
            "observation": {"state": "stale", "epoch": 1, "as_of": "2026-11-12T03:00:00+00:00"},
        },
        [],
        {},
    )
    handler = html.split('a[data-cost-filter]")', 1)[1]
    assert handler.find("__costLeave = true") < handler.find("saveCostFilterThen")
    assert "__costLeave = false" in html
    assert "__costResume" in html
