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
    # The probation watch is process state, not a row: a provider id reused by another
    # test must not inherit the previous test's window.
    mod._PROBATION.clear()
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


# 9 — a cooling provider is still checked every probe interval, not once per rung.
def test_a_cooling_provider_is_checked_every_probe_interval(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    monkeypatch.setattr(engine_mod, "probe_interval_seconds", lambda: 300.0)
    calls = []
    monkeypatch.setattr(
        engine_mod,
        "_probe_provider",
        lambda item: calls.append(item["id"]) or {"health": "warn", "health_detail": "HTTP 429", "remark": "HTTP 429"},
    )
    engine_mod.gate_hold(provider, "quota_window", jitter=0)
    # 这一轮退避是 5 分钟：刚上锁时还没到探活时刻。
    assert engine_mod.gate_next_probe(provider) - time.time() == pytest.approx(300, abs=5)
    assert engine_mod.probe_providers_if_due() == 0
    assert calls == []

    # 探活时刻一到就该探，哪怕退避还没结束。
    reading = engine_mod.gate_reading(provider)
    assert reading["until_unix"] > time.time()
    engine_mod.db().execute(
        "UPDATE settings SET v=? WHERE k=?",
        (json.dumps({**reading, "probe_unix": time.time() - 1}), f"provider_gate:{provider}"),
    )
    engine_mod.db().commit()
    assert engine_mod.probe_providers_if_due() == 1
    assert calls == [provider]


# 9b — the ladder holds dispatch back; it must not hold the next check back.
def test_the_ladder_does_not_delay_the_next_check(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    monkeypatch.setattr(engine_mod, "probe_interval_seconds", lambda: 300.0)
    for _ in range(6):
        reading = engine_mod.gate_hold(provider, "quota_window", jitter=0)
    assert reading["level"] == 5
    assert reading["until_unix"] - time.time() == pytest.approx(18000, abs=10)
    # 退避是 5 小时，下一次探活仍然只有 5 分钟。
    assert engine_mod.gate_next_probe(provider) - time.time() == pytest.approx(300, abs=5)
    assert engine_mod.gate_cooling(provider) is True


# 9c — a record written before the probe clock existed still reads.
def test_a_record_without_the_probe_clock_falls_back_to_the_deadline(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    legacy = {"state": "cooling", "until_unix": time.time() + 18000, "level": 5, "reason": "quota_window", "code": "", "at": ""}
    engine_mod.set_setting(f"provider_gate:{provider}", json.dumps(legacy))
    engine_mod.db().commit()
    monkeypatch.setattr(engine_mod, "probe_interval_seconds", lambda: 300.0)
    calls = []
    monkeypatch.setattr(
        engine_mod,
        "_probe_provider",
        lambda item: calls.append(item["id"]) or {"health": "ok", "health_detail": "正常", "remark": ""},
    )
    assert engine_mod.gate_next_probe(provider) == pytest.approx(legacy["until_unix"], abs=1)
    assert engine_mod.probe_providers_if_due() == 0
    assert calls == []


# 9d — the quota comes back mid-rung: the next check opens the gate at once.
def test_a_recovered_quota_is_noticed_within_the_probe_interval(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    monkeypatch.setattr(engine_mod, "probe_interval_seconds", lambda: 300.0)
    assert engine_mod.gate_hold(provider, "quota_window", jitter=0)
    for _ in range(5):
        engine_mod.db().execute(
            "UPDATE settings SET v=? WHERE k=?",
            (
                json.dumps({**engine_mod.gate_reading(provider), "probe_unix": time.time() - 1}),
                f"provider_gate:{provider}",
            ),
        )
        engine_mod.db().commit()
        monkeypatch.setattr(
            engine_mod,
            "_probe_provider",
            lambda item: {"health": "warn", "health_detail": "HTTP 429", "remark": "HTTP 429"},
        )
        engine_mod.probe_providers_if_due()
    held = engine_mod.gate_reading(provider)
    assert held["level"] == 5
    assert held["until_unix"] - time.time() == pytest.approx(18000, rel=0.12)

    # 配额恢复了：下一次探活（一个间隔之后）就该开闸，而不是等 5 小时。
    engine_mod.db().execute(
        "UPDATE settings SET v=? WHERE k=?",
        (json.dumps({**held, "probe_unix": time.time() - 1}), f"provider_gate:{provider}"),
    )
    engine_mod.db().commit()
    monkeypatch.setattr(
        engine_mod, "_probe_provider", lambda item: {"health": "ok", "health_detail": "正常", "remark": ""}
    )
    assert engine_mod.probe_providers_if_due() == 1
    cleared = engine_mod.gate_reading(provider)
    assert cleared["state"] == "open"
    assert cleared["level"] == 0
    assert engine_mod.gate_cooling(provider) is False


# 10 — a good probe clears the gate outright.
def test_a_good_probe_clears_the_gate(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    engine_mod.gate_hold(provider, "quota_window", jitter=0)
    reading = engine_mod.gate_reading(provider)
    engine_mod.db().execute(
        "UPDATE settings SET v=? WHERE k=?",
        (json.dumps({**reading, "probe_unix": time.time() - 1}), f"provider_gate:{provider}"),
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
    monkeypatch.setattr(engine_mod, "probe_interval_seconds", lambda: 300.0)
    engine_mod.gate_hold(provider, "quota_window", jitter=0)
    assert engine_mod.gate_reading(provider)["level"] == 0
    reading = engine_mod.gate_reading(provider)
    engine_mod.db().execute(
        "UPDATE settings SET v=? WHERE k=?",
        (json.dumps({**reading, "probe_unix": time.time() - 1}), f"provider_gate:{provider}"),
    )
    engine_mod.db().commit()
    monkeypatch.setattr(
        engine_mod, "_probe_provider", lambda item: {"health": "warn", "health_detail": "HTTP 429", "remark": "HTTP 429"}
    )
    assert engine_mod.probe_providers_if_due() == 1
    stepped = engine_mod.gate_reading(provider)
    assert stepped["level"] == 1
    assert stepped["reason"] == "quota_window"
    # 爬梯子只推后派单，下一次探活照旧是一个间隔。
    assert stepped["until_unix"] - time.time() == pytest.approx(900, rel=0.12)
    assert engine_mod.gate_next_probe(provider) - time.time() == pytest.approx(300, abs=10)


# 10 — a down probe keeps the step, only the clock moves (so no tight re-poll).
def test_a_down_probe_keeps_the_step(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    monkeypatch.setattr(engine_mod, "probe_interval_seconds", lambda: 300.0)
    engine_mod.gate_hold(provider, "quota_window", jitter=0)
    reading = engine_mod.gate_reading(provider)
    engine_mod.db().execute(
        "UPDATE settings SET v=? WHERE k=?",
        (json.dumps({**reading, "probe_unix": time.time() - 1}), f"provider_gate:{provider}"),
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
    monkeypatch.setattr(engine_mod, "probe_interval_seconds", lambda: 300.0)
    engine_mod.gate_hold(provider, "quota_window", jitter=0)
    status = engine_mod.gate_status(provider)
    assert status["state"] == "cooling"
    assert status["cooling"] is True
    assert status["level"] == 0
    assert status["reason"] == "quota_window"
    # 下一次探活是一个探活间隔，不是这一档退避。
    assert status["next_probe_unix"] - time.time() == pytest.approx(300, abs=10)
    assert status["probation_until_unix"] == 0.0


def _queued_file(engine_mod, tmp_path, name: str, text: str = "甲在南港工作多年。") -> str:
    path = tmp_path / "scan" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    opened = engine_mod.ingest_file(path)
    engine_mod.db().execute(
        "UPDATE queue SET state='queued', next_at=NULL, leased_until=NULL WHERE source_id=?",
        (opened["id"],),
    )
    engine_mod.db().commit()
    return opened["id"]


# F — the ladder is a setting, not a constant in the code.
def test_the_ladder_comes_from_settings(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    engine_mod.set_setting("gate_quota_steps", "10,20")
    engine_mod.db().commit()
    assert engine_mod.quota_gate_steps() == (10, 20)

    before = time.time()
    first = engine_mod.gate_hold("lp", "quota_window", jitter=0)
    assert first["until_unix"] - before == pytest.approx(10, abs=1)
    second = engine_mod.gate_hold("lp", "quota_window", jitter=0)
    assert second["until_unix"] - before == pytest.approx(20, abs=1)
    third = engine_mod.gate_hold("lp", "quota_window", jitter=0)
    assert third["level"] == 1
    assert third["until_unix"] - before == pytest.approx(20, abs=1)

    # 把阶梯改短后，旧的长档位必须夹回来，不是 IndexError。
    engine_mod.set_setting("gate_quota_steps", "30")
    engine_mod.db().commit()
    clamped = engine_mod.gate_hold("lp", "quota_window", jitter=0)
    assert clamped["level"] == 0
    assert clamped["until_unix"] - time.time() == pytest.approx(30, abs=1)


# F — the flat throttle wait is a setting too.
def test_the_throttle_wait_comes_from_settings(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    engine_mod.set_setting("gate_throttle_seconds", "5")
    engine_mod.db().commit()
    before = time.time()
    reading = engine_mod.gate_hold("th", "throttle", jitter=0)
    assert reading["until_unix"] - before == pytest.approx(5, abs=1)
    assert engine_mod.throttle_gate_seconds() == 5


# G — a reopened quota window is watched; a plain throttle is not.
def test_a_reopened_quota_window_is_watched(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    engine_mod.set_setting("gate_probation_units", "1")
    engine_mod.set_setting("gate_probation_seconds", "600")
    engine_mod.db().commit()

    engine_mod.gate_hold(provider, "throttle", jitter=0)
    engine_mod.gate_clear(provider)
    assert engine_mod.probation_until(provider) == 0.0

    unit = _queued_file(engine_mod, tmp_path, "看着.md")
    engine_mod.gate_hold(provider, "quota_window", jitter=0)
    engine_mod.gate_clear(provider)
    assert engine_mod.probation_until(provider) > time.time()
    assert engine_mod.gate_status(provider)["probation_until_unix"] > 0

    assert engine_mod.lease_next()["source_id"] == unit
    assert engine_mod.lease_next() is None  # 已经在跑一条，第二条等
    engine_mod.db().execute(
        "UPDATE queue SET state='queued', leased_until=NULL WHERE source_id=?", (unit,)
    )
    engine_mod.db().commit()
    assert engine_mod.lease_next() is not None  # 前一条跑完了才接上

    engine_mod._PROBATION[provider] = (time.time() - 1, 1)
    assert engine_mod._probation_blocks(engine_mod.db(), provider) is False
    assert engine_mod.probation_until(provider) == 0.0


# G — zero turns the watch off, so the behaviour is exactly the old one.
def test_probation_zero_turns_the_watch_off(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    provider = _gate_provider(engine_mod)
    engine_mod.set_setting("gate_probation_units", "0")
    engine_mod.db().commit()
    engine_mod.gate_hold(provider, "quota_window", jitter=0)
    engine_mod.gate_clear(provider)
    assert engine_mod.probation_until(provider) == 0.0


# H — the distill header says which host and which conversation the words came from.
def test_the_header_carries_the_host_and_the_session(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    from rogmem.prompts import distill_header, speaker_for

    row = {
        "id": "src-1",
        "kind": "user",
        "agent_name": "WANGS-MacBook-Pro-408 · Cursor",
        "session_id": "8e6549b8-328c-485f-a2dd-298695e32cac",
        "event_at": "2026-11-17T02:00:00+00:00",
        "event_basis": "utterance",
    }
    inject = engine_mod._prompt_injection(row)
    assert inject["agent_name"] == "WANGS-MacBook-Pro-408 · Cursor"
    assert inject["session_id"] == "8e6549b8-328c-485f-a2dd-298695e32cac"

    header = distill_header(
        material_kind="会话",
        speaker=speaker_for("user"),
        owner_name="",
        owner_aliases="",
        source_id="src-1",
        filename="",
        agent_name=inject["agent_name"],
        session_id=inject["session_id"],
    )
    assert "来自哪个智能体：WANGS-MacBook-Pro-408 · Cursor" in header
    assert "会话：8e6549b8-328c-485f-a2dd-298695e32cac" in header

    # 没有这两个值时不留空行。
    bare = distill_header(
        material_kind="会话", speaker=speaker_for("user"), owner_name="", owner_aliases="",
        source_id="src-1", filename="",
    )
    assert "来自哪个智能体" not in bare
    assert "会话：" not in bare


# H — the merge prompt says the same thing about where the material came from.
def test_the_merge_prompt_carries_the_host_and_the_session():
    from rogmem.prompts import merge_user

    text = merge_user(
        "旧页", "新页", "原文",
        page_name="记忆主人", page_type="人物",
        agent_name="WANGS-MacBook-Pro-408 · Cursor",
        session_id="8e6549b8-328c-485f-a2dd-298695e32cac",
    )
    assert "来自哪个智能体：WANGS-MacBook-Pro-408 · Cursor" in text
    assert "会话：8e6549b8-328c-485f-a2dd-298695e32cac" in text
    plain = merge_user("旧页", "新页", "原文", page_name="记忆主人", page_type="人物")
    assert "来自哪个智能体" not in plain
    assert "会话：" not in plain


# I — the gate panel's fields save, echo back, and refuse bad input without changing anything.
def test_pipeline_settings_carry_the_gate_and_concurrency(tmp_path, monkeypatch):
    engine_mod = _open(tmp_path, monkeypatch)
    base = {"scan_interval": "60", "probe_interval": "60", "reconcile_hours": "3", "sweep_at": "03:00"}
    saved = engine_mod.save_pipeline(
        {
            **base,
            "gate_quota_steps": "30,60",
            "gate_throttle_seconds": "5",
            "gate_probation_units": "1",
            "gate_probation_seconds": "120",
            "distill_concurrency": "2",
        }
    )
    assert saved["ok"] is True
    assert saved["config"]["gate_quota_steps"] == "30,60"
    assert saved["config"]["gate_throttle_seconds"] == "5"
    assert saved["config"]["gate_probation_units"] == "1"
    assert saved["config"]["gate_probation_seconds"] == "120"
    assert saved["config"]["distill_concurrency"] == "2"
    assert engine_mod.quota_gate_steps() == (30, 60)
    assert engine_mod.throttle_gate_seconds() == 5
    assert engine_mod.probation_units() == 1
    assert engine_mod.probation_seconds() == 120
    assert engine_mod.distill_concurrency() == 2

    # 老调用方只给那四个键，照样过，而且不动闸门设置。
    old = engine_mod.save_pipeline(base)
    assert old["ok"] is True
    assert engine_mod.quota_gate_steps() == (30, 60)

    assert engine_mod.save_pipeline({**base, "gate_quota_steps": "abc"})["ok"] is False
    assert engine_mod.save_pipeline({**base, "distill_concurrency": "99"})["ok"] is False
    assert engine_mod.quota_gate_steps() == (30, 60)

    # 这些键别的用例会当默认值读，别把这条用例的输入留成全局状态。
    for key in (
        "gate_quota_steps",
        "gate_throttle_seconds",
        "gate_probation_units",
        "gate_probation_seconds",
        "distill_concurrency",
    ):
        engine_mod.db().execute("DELETE FROM settings WHERE k=?", (key,))
    engine_mod.db().commit()
    assert engine_mod.distill_concurrency() == 3
