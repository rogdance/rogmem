"""结构化指标导出：Prometheus 文本 + JSON，不含密钥原值。"""

from __future__ import annotations

from rogmem import metrics


def test_render_prometheus_contains_core_series():
    snap = {
        "schema_version": 1,
        "sources": 3,
        "crystals_active": 2,
        "queue_leased": 1,
        "distill_paused": 0,
        "distill_concurrency": 3,
        "defense_decisions": {"allow": 1, "redact": 2, "block": 0},
        "model_calls": 10,
    }
    text = metrics.render_prometheus(snap)
    assert "rogmem_schema_version 1" in text
    assert "rogmem_sources 3" in text
    assert 'rogmem_defense_decisions_total{action="redact"} 2' in text
    assert "rogmem_queue_leased 1" in text


def test_metrics_snapshot_from_connect(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine
    from rogmem import migrate

    engine.connect(tmp_path, tmp_path / "scan")
    snap = metrics.snapshot(engine.db())
    assert snap["schema_version"] == migrate.SCHEMA_VERSION
    assert "sources" in snap
    assert "crystals_active" in snap
    assert set(snap["defense_decisions"]) >= {"allow", "redact", "block"}
    body = metrics.render_prometheus(snap)
    assert "rogmem_schema_version" in body
    assert "sk-" not in body


def test_metrics_http_route(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from starlette.testclient import TestClient

    from rogmem import engine
    from rogmem.app import mcp

    engine.connect(tmp_path, tmp_path / "scan")
    client = TestClient(mcp.streamable_http_app())
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "rogmem_schema_version" in resp.text
    assert "text/plain" in resp.headers.get("content-type", "")

    resp_json = client.get("/metrics.json")
    assert resp_json.status_code == 200
    data = resp_json.json()
    assert "schema_version" in data
