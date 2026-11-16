"""Provider gate: one shared, stored verdict on whether a model provider may be called.

The live call and the probe both classify a fault the same way. A wait-it-out fault
shuts the gate and hands every leased unit back at once; the whole line stops until
the gate's own clock lets a probe through.
"""

import io
import json
import sqlite3
import time
import urllib.error
import urllib.request

import pytest

from rogmem import engine


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.delenv("ROGMEM_LLM_BASE", raising=False)
    monkeypatch.delenv("ROGMEM_LLM_KEY", raising=False)
    from rogmem import engine as mod

    mod.connect(tmp_path, tmp_path / "scan")
    mod.set_embed_mode("off")
    return mod


def _gate_provider(engine_mod, provider_id="gate-prov"):
    engine_mod._store_providers(
        [
            {
                "id": provider_id,
                "name": "闸门供应商",
                "base_url": "https://gate.test/v1",
                "api_key": "sk-test",
                "models": ["m"],
                "model_kinds": {"m": "chat"},
                "health": "ok",
                "health_unix": time.time(),
            }
        ]
    )
    engine_mod.set_setting("model_distill", f"{provider_id}|m")
    engine_mod.db().commit()
    return provider_id


def _body(status: int, raw: bytes):
    class _Resp:
        def __init__(self):
            self.status = status

        def read(self, _n=-1):
            return raw

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    return _Resp()


def _http_error(code: int, body: bytes):
    def respond(req, timeout=60):
        raise urllib.error.HTTPError(getattr(req, "full_url", "https://x.invalid"), code, "error", {}, io.BytesIO(body))

    return respond


def _counts(engine_mod, sid):
    queued = engine_mod.db().execute(
        "SELECT state, attempts, next_at, leased_until FROM queue WHERE source_id=?", (sid,)
    ).fetchone()
    source = engine_mod.db().execute("SELECT status, last_error FROM sources WHERE id=?", (sid,)).fetchone()
    return queued, source


# 1 — every business code lands on the contract's reason.
def test_business_codes_map_to_reasons():
    for code in (1002, 1039, 1041, 2045, 2046, 2047):
        assert engine._classify_provider_failure(429, code, "") == "throttle"
    for code in (2056, 2062):
        assert engine._classify_provider_failure(200, code, "") == "quota_window"
    for code in (1004, 2049):
        assert engine._classify_provider_failure(200, code, "") == "auth"
    assert engine._classify_provider_failure(401, None, "") == "auth"
    assert engine._classify_provider_failure(403, None, "") == "auth"
    for code in (1008, 2053, 2067):
        assert engine._classify_provider_failure(200, code, "") == "billing"
    for code in (1000, 1001, 1024, 1033, 2064, 2066):
        assert engine._classify_provider_failure(200, code, "") == "transient"
    assert engine._classify_provider_failure(200, None, "") == "unknown"


# 1 — a code inside a 200 body is read too (base_resp.status_code == 2056).
def test_a_200_body_with_base_resp_2056_is_a_quota_window(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_a, **_k: _body(200, b'{"choices":[],"base_resp":{"status_code":2056,"status_msg":"usage limit"}}'),
    )
    with pytest.raises(engine_mod.ProviderRefused) as caught:
        engine_mod._complete_chat(
            "https://gate.test/v1", "sk", {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        )
    assert caught.value.reason == "quota_window"


# 2 — the ladder climbs 300/900/1800/3600/7200/18000 and stops at the top.
def test_quota_window_climbs_the_ladder_and_caps(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = "gate-prov"
    expected = [300, 900, 1800, 3600, 7200, 18000, 18000]
    levels = [0, 1, 2, 3, 4, 5, 5]
    reading = None
    for want, want_level in zip(expected, levels):
        before = time.time()
        reading = engine_mod.gate_hold(provider, "quota_window", jitter=0)
        assert reading["until_unix"] - before == pytest.approx(want, abs=1)
        assert reading["level"] == want_level
    assert reading["level"] == 5
    assert engine_mod.gate_reading(provider)["until_unix"] == reading["until_unix"]


# 3 — the step is on disk, so a fresh read (a restart) does not drop it to zero.
def test_the_ladder_step_survives_a_reopen(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    engine_mod.gate_hold("gate-prov", "quota_window", jitter=0)  # 0
    engine_mod.gate_hold("gate-prov", "quota_window", jitter=0)  # 1
    other = sqlite3.connect(tmp_path / "rogmem.sqlite")
    try:
        stored = other.execute("SELECT v FROM settings WHERE k='provider_gate:gate-prov'").fetchone()[0]
    finally:
        other.close()
    assert json.loads(stored)["level"] == 1
    assert engine_mod.gate_reading("gate-prov")["level"] == 1
    third = engine_mod.gate_hold("gate-prov", "quota_window", jitter=0)
    assert third["level"] == 2


# 4 — a Retry-After beats the default rung.
def test_retry_after_wins_over_the_default_tier(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    before = time.time()
    reading = engine_mod.gate_hold("gate-prov", "quota_window", retry_after=42, jitter=0)
    assert reading["until_unix"] - before == pytest.approx(42, abs=1)
    before = time.time()
    throttled = engine_mod.gate_hold("gate-prov", "throttle", retry_after=7, jitter=0)
    assert throttled["until_unix"] - before == pytest.approx(7, abs=1)


# 5 — throttle with no Retry-After waits 60 seconds.
def test_throttle_first_tier_is_sixty_seconds(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    before = time.time()
    reading = engine_mod.gate_hold("gate-prov", "throttle", jitter=0)
    assert reading["until_unix"] - before == pytest.approx(60, abs=1)
    assert reading["level"] == 0


# 6 — a cooling gate dispatches no unit at all.
def test_lease_next_holds_every_unit_while_cooling(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    assert engine_mod._distill_provider_id() == provider
    path = tmp_path / "scan" / "闸门.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("甲在南港工作多年。", encoding="utf-8")
    opened = engine_mod.ingest_file(path)
    engine_mod.db().execute(
        "UPDATE queue SET state='queued', next_at=NULL, leased_until=NULL WHERE source_id=?", (opened["id"],)
    )
    engine_mod.db().commit()
    engine_mod.gate_hold(provider, "quota_window", jitter=0)
    assert engine_mod.gate_cooling(provider) is True
    assert engine_mod.lease_next() is None
    engine_mod.gate_clear(provider)
    again = engine_mod.lease_next()
    assert again is not None and again["source_id"] == opened["id"]


# 7 — throttle/quota is a wait, not a verdict: no attempt spent, no auth alert.
def test_a_throttle_waits_without_spending_an_attempt(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    monkeypatch.setattr(urllib.request, "urlopen", _http_error(429, b'{"error":"rate limit"}'))
    path = tmp_path / "scan" / "限流.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("甲在南港工作多年。", encoding="utf-8")
    opened = engine_mod.ingest_file(path)
    assert engine_mod.pump_one() == opened["id"]
    queued, source = _counts(engine_mod, opened["id"])
    assert queued["state"] == "queued"
    assert queued["attempts"] == 0
    assert queued["leased_until"] is None
    assert source["status"] == "蒸馏中"
    assert engine_mod.gate_cooling(provider) is True


# 7 (cont.) — the queue's next_at is the gate's own time, and no auth alert opened.
def test_a_throttle_sets_next_at_to_the_gate_time(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    monkeypatch.setattr(urllib.request, "urlopen", _http_error(429, b'{"error":"rate limit"}'))
    path = tmp_path / "scan" / "限流2.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("乙在北城工作多年。", encoding="utf-8")
    opened = engine_mod.ingest_file(path)
    assert engine_mod.pump_one() == opened["id"]
    queued, _source = _counts(engine_mod, opened["id"])
    from datetime import datetime

    assert datetime.fromisoformat(queued["next_at"]).timestamp() == pytest.approx(
        engine_mod.gate_next_at(provider), abs=1e-6
    )
    rows = engine_mod.db().execute("SELECT message FROM alerts WHERE open=1").fetchall()
    assert not any(str(row["message"]).startswith("外部模型鉴权失败") for row in rows)


# 8 — opening the gate returns the leased rows at once, and only for that provider.
def test_opening_the_gate_returns_leased_rows(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    for sid in ("leak-a", "leak-b"):
        engine_mod.db().execute(
            """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pair_ready)
               VALUES(?,?,?,?,?,?,?,1)""",
            (sid, "file", "2026-11-10T00:00:00+00:00", "h", "甲在南港。", "蒸馏中", "p3"),
        )
        engine_mod.db().execute(
            "INSERT INTO queue(source_id,lane,state,leased_until) VALUES(?,?, 'leased', ?)",
            (sid, "p3", "2099-01-01T00:00:00+00:00"),
        )
    engine_mod.db().commit()

    engine_mod.gate_hold("别的供应商", "throttle", jitter=0)
    still = engine_mod.db().execute("SELECT COUNT(*) AS n FROM queue WHERE state='leased'").fetchone()["n"]
    assert still == 2  # a gate for another provider must not touch these rows

    engine_mod.gate_hold(provider, "throttle", jitter=0)
    rows = engine_mod.db().execute("SELECT state, leased_until FROM queue").fetchall()
    assert len(rows) == 2
    assert all(row["state"] == "queued" and row["leased_until"] is None for row in rows)


# 9 — a cooling provider probes only on the gate's clock, never sooner.
def test_a_cooling_provider_probes_on_the_gate_clock(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    monkeypatch.setattr(engine_mod, "probe_interval_seconds", lambda: 300.0)
    calls = []
    monkeypatch.setattr(
        engine_mod,
        "_probe_provider",
        lambda item: calls.append(item["id"]) or {"health": "ok", "health_detail": "正常", "remark": ""},
    )
    engine_mod.gate_hold(provider, "quota_window", jitter=0)
    assert engine_mod.probe_providers_if_due() == 0
    assert calls == []

    reading = engine_mod.gate_reading(provider)
    engine_mod.db().execute(
        "UPDATE settings SET v=? WHERE k=?",
        (json.dumps({**reading, "until_unix": time.time() - 1}), f"provider_gate:{provider}"),
    )
    engine_mod.db().commit()
    assert engine_mod.probe_providers_if_due() == 1
    assert calls == [provider]


# 10 — a good probe clears the gate outright.
def test_a_good_probe_clears_the_gate(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    engine_mod.gate_hold(provider, "quota_window", jitter=0)
    reading = engine_mod.gate_reading(provider)
    engine_mod.db().execute(
        "UPDATE settings SET v=? WHERE k=?",
        (json.dumps({**reading, "until_unix": time.time() - 1}), f"provider_gate:{provider}"),
    )
    engine_mod.db().commit()
    monkeypatch.setattr(
        engine_mod, "_probe_provider", lambda item: {"health": "ok", "health_detail": "正常", "remark": ""}
    )
    assert engine_mod.probe_providers_if_due() == 1
    cleared = engine_mod.gate_reading(provider)
    assert cleared["state"] == "open"
    assert cleared["level"] == 0


# 10 — a warn probe pushes the same ladder one more step.
def test_a_warn_probe_steps_the_ladder_up(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    engine_mod.gate_hold(provider, "quota_window", jitter=0)
    assert engine_mod.gate_reading(provider)["level"] == 0
    reading = engine_mod.gate_reading(provider)
    engine_mod.db().execute(
        "UPDATE settings SET v=? WHERE k=?",
        (json.dumps({**reading, "until_unix": time.time() - 1}), f"provider_gate:{provider}"),
    )
    engine_mod.db().commit()
    monkeypatch.setattr(
        engine_mod, "_probe_provider", lambda item: {"health": "warn", "health_detail": "HTTP 429", "remark": "HTTP 429"}
    )
    assert engine_mod.probe_providers_if_due() == 1
    stepped = engine_mod.gate_reading(provider)
    assert stepped["level"] == 1
    assert stepped["reason"] == "quota_window"


# 10 — a down probe keeps the step, only the clock moves (so no tight re-poll).
def test_a_down_probe_keeps_the_step(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    monkeypatch.setattr(engine_mod, "probe_interval_seconds", lambda: 300.0)
    engine_mod.gate_hold(provider, "quota_window", jitter=0)
    reading = engine_mod.gate_reading(provider)
    engine_mod.db().execute(
        "UPDATE settings SET v=? WHERE k=?",
        (json.dumps({**reading, "until_unix": time.time() - 1}), f"provider_gate:{provider}"),
    )
    engine_mod.db().commit()
    calls = []
    monkeypatch.setattr(
        engine_mod,
        "_probe_provider",
        lambda item: calls.append(item["id"]) or {"health": "down", "health_detail": "连不上", "remark": "连不上"},
    )
    assert engine_mod.probe_providers_if_due() == 1
    kept = engine_mod.gate_reading(provider)
    assert kept["level"] == 0
    assert kept["until_unix"] - time.time() == pytest.approx(300, abs=5)
    assert engine_mod.probe_providers_if_due() == 0  # pushed into the future, not the next tick
    assert calls == [provider]


# 11 — auth, billing and unknown never open a cooling gate.
def test_auth_billing_and_unknown_never_open_a_gate(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    for reason in ("auth", "billing", "transient", "unknown"):
        engine_mod.gate_hold(provider, reason, jitter=0)
        assert engine_mod.gate_reading(provider)["state"] == "open"
        assert engine_mod.gate_cooling(provider) is False
    assert engine_mod._classify_provider_failure(200, 2067, "") == "billing"


# E — the small read for the console.
def test_gate_status_reports_the_step_and_next_probe(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    engine_mod.gate_hold(provider, "quota_window", jitter=0)
    status = engine_mod.gate_status(provider)
    assert status["state"] == "cooling"
    assert status["cooling"] is True
    assert status["level"] == 0
    assert status["reason"] == "quota_window"
    assert status["next_probe_unix"] == engine_mod.gate_next_at(provider)
