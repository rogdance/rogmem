"""成本筛选跨端服务端持久化。settings KV 存全组合；GET 只读不写锁；URL 显式优先只影响本次视图。"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    return engine


# ---- engine 层 ----


def test_cost_filters_round_trips_full_combo(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    assert engine.cost_filters() is None
    ok = engine.save_cost_filters(
        {"span": "7d", "detail": "24h", "worker": "w1", "model": "p\tm", "dworker": "w2", "dmodel": "p\tn"}
    )
    assert ok == {"ok": True}
    assert engine.cost_filters() == {
        "span": "7d",
        "detail": "24h",
        "worker": "w1",
        "model": "p\tm",
        "dworker": "w2",
        "dmodel": "p\tn",
    }


def test_cost_filters_defaults_missing_keys_to_empty(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    assert engine.save_cost_filters({"span": "all"}) == {"ok": True}
    assert engine.cost_filters() == {
        "span": "all",
        "detail": "30d",  # 没给 detail 时保存端用内置默认
        "worker": "",
        "model": "",
        "dworker": "",
        "dmodel": "",
    }


def test_cost_filters_rejects_unknown_span(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    result = engine.save_cost_filters({"span": "forever", "detail": "30d"})
    assert result["ok"] is False
    assert engine.cost_filters() is None  # 失败时不写


def test_cost_filters_rejects_unknown_detail(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    result = engine.save_cost_filters({"span": "30d", "detail": "7d"})  # 明细没 7d 这档
    assert result["ok"] is False
    assert engine.cost_filters() is None


def test_cost_filters_rejects_overlong_model(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    result = engine.save_cost_filters({"span": "30d", "detail": "30d", "model": "x" * 500})
    assert result["ok"] is False
    assert engine.cost_filters() is None


def test_cost_filters_drops_unknown_fields(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.save_cost_filters(
        {"span": "30d", "detail": "30d", "worker": "w", "page": 99, "garbage": "x"}
    )
    stored = engine.cost_filters()
    assert "page" not in stored
    assert "garbage" not in stored
    assert stored["worker"] == "w"


def test_cost_filters_returns_none_for_bad_json(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.set_setting("cost_filters", "not json {")
    engine.db().commit()
    assert engine.cost_filters() is None


def test_cost_filters_returns_none_when_value_is_not_dict(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.set_setting("cost_filters", json.dumps(["list", "not", "dict"]))
    engine.db().commit()
    assert engine.cost_filters() is None


def test_cost_filters_read_does_not_hold_write_lock(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.save_cost_filters({"span": "7d", "detail": "30d"})
    # Warm WAL so no DDL/init remains on next read.
    assert engine.cost_filters()["span"] == "7d"
    writer = sqlite3.connect(tmp_path / "rogmem.sqlite", timeout=1)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO settings(k,v) VALUES('lockprobe','x')")
        started = time.perf_counter()
        value = engine.cost_filters()
        elapsed = time.perf_counter() - started
        assert value["span"] == "7d"
        assert elapsed < 1.0, f"read waited {elapsed:.3f}s behind the write lock"
    finally:
        writer.rollback()
        writer.close()


def test_cost_filters_survive_reopen_same_data_dir(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.save_cost_filters({"span": "24h", "detail": "30d", "worker": "w"})
    # Simulate a process restart: force a fresh connection on the same files.
    engine._DB.close()
    engine._LOCAL.con = None
    engine._DB = None
    engine.connect(tmp_path, tmp_path / "scan")
    assert engine.cost_filters()["worker"] == "w"


# ---- /admin/api/costs/filters ----


def _client(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from starlette.testclient import TestClient

    from rogmem.app import mcp

    client = TestClient(mcp.streamable_http_app())
    login = client.post("/admin/login", data={"password": "whatever"})
    assert login.status_code in (200, 303, 307, 302)
    return engine, client


def test_api_reuses_guard_decorator(tmp_path, monkeypatch):
    """成本筛选复用 `_guard`。临时目录的口令豁免仍放行；正式目录收紧 `_admin_ok` 时这里跟着收紧。"""
    _open(tmp_path, monkeypatch)
    import inspect

    from rogmem import app as app_module

    src = inspect.getsource(app_module.save_cost_filters)
    assert "_guard(request)" in src


def test_api_saves_combo(tmp_path, monkeypatch):
    engine, client = _client(tmp_path, monkeypatch)
    resp = client.post(
        "/admin/api/costs/filters",
        json={"span": "7d", "detail": "24h", "worker": "w1", "model": "", "dworker": "", "dmodel": ""},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert engine.cost_filters()["span"] == "7d"


def test_api_rejects_bad_span(tmp_path, monkeypatch):
    engine, client = _client(tmp_path, monkeypatch)
    resp = client.post("/admin/api/costs/filters", json={"span": "nope", "detail": "30d"})
    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    assert engine.cost_filters() is None


def test_api_rejects_non_dict(tmp_path, monkeypatch):
    engine, client = _client(tmp_path, monkeypatch)
    resp = client.post("/admin/api/costs/filters", json=["not", "dict"])
    assert resp.status_code == 400


# ---- /admin/costs URL mode ----


def test_url_mode_ignores_storage(tmp_path, monkeypatch):
    engine, client = _client(tmp_path, monkeypatch)
    engine.save_cost_filters({"span": "7d", "detail": "30d", "worker": "w1"})
    resp = client.get("/admin/costs?span=24h")
    assert resp.status_code == 200
    body = resp.text
    # 当前 span 应该是 24h，worker 应该回落「全部」
    assert "span=24h" in body
    assert "worker=w1" not in body
    # 访问没改存储
    assert engine.cost_filters()["worker"] == "w1"


def test_bare_url_restores_storage(tmp_path, monkeypatch):
    engine, client = _client(tmp_path, monkeypatch)
    engine.save_cost_filters({"span": "7d", "detail": "30d", "worker": "w1"})
    resp = client.get("/admin/costs")
    body = resp.text
    assert "span=7d" in body
    assert "worker=w1" in body


def test_bare_url_with_only_page_still_restores(tmp_path, monkeypatch):
    engine, client = _client(tmp_path, monkeypatch)
    engine.save_cost_filters({"span": "24h", "detail": "30d"})
    resp = client.get("/admin/costs?page=2")
    assert "span=24h" in resp.text


def test_get_does_not_write_settings(tmp_path, monkeypatch):
    engine, client = _client(tmp_path, monkeypatch)
    engine.save_cost_filters({"span": "7d", "detail": "30d"})
    before = engine.db().execute("SELECT v FROM settings WHERE k='cost_filters'").fetchone()["v"]
    for _ in range(5):
        client.get("/admin/costs")
        client.get("/admin/costs?span=24h")
    after = engine.db().execute("SELECT v FROM settings WHERE k='cost_filters'").fetchone()["v"]
    assert before == after


def test_concurrent_saves_last_write_wins(tmp_path, monkeypatch):
    engine, client = _client(tmp_path, monkeypatch)
    client.post("/admin/api/costs/filters", json={"span": "7d", "detail": "30d"})
    client.post("/admin/api/costs/filters", json={"span": "24h", "detail": "30d"})
    assert engine.cost_filters()["span"] == "24h"
