"""Engine-level release cache, scheduler, backoff, and manual cooldown.

Uses a real local HTTPS-less loopback HTTP server (allowed via
ROGMEM_ALLOW_LOOPBACK_SOURCE) so the engine runs the full fetch →
schema → write pipeline rather than a mocked shortcut.
"""

from __future__ import annotations

import http.server
import json
import threading
import time

import pytest


def _manifest(tag: str = "V1-2611121012-PUB") -> bytes:
    return json.dumps(
        {
            "schema": "rogmem-releases/1",
            "generated_at": "2026-11-12T04:30:00Z",
            "channels": {
                "stable": {"latest": tag, "released_at": "2026-11-12T02:14:10Z"}
            },
            "releases": [
                {"tag": tag, "channel": "stable", "released_at": "2026-11-12T02:14:10Z"}
            ],
        }
    ).encode("utf-8")


class _Handler(http.server.BaseHTTPRequestHandler):
    scenarios = {"path": "/ok.json", "status": 200, "body": _manifest(), "etag": '"v1"'}
    calls = []

    def log_message(self, *_a, **_k):
        return

    def do_GET(self):
        _Handler.calls.append((self.path, dict(self.headers)))
        s = _Handler.scenarios
        if s["status"] == 304 and self.headers.get("If-None-Match") == s["etag"]:
            self.send_response(304)
            self.send_header("ETag", s["etag"])
            self.end_headers()
            return
        self.send_response(s["status"])
        if s["etag"]:
            self.send_header("ETag", s["etag"])
        self.send_header("Content-Type", "application/json")
        body = s["body"] if s["status"] == 200 else b'{"m":"nope"}'
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


_server = None
_port = 0


def _start_server():
    global _server, _port
    if _server is not None:
        return
    _server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    _port = _server.server_port
    thread = threading.Thread(target=_server.serve_forever, daemon=True)
    thread.start()


@pytest.fixture
def engine(tmp_path, monkeypatch):
    _start_server()
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_ALLOW_LOOPBACK_SOURCE", "1")
    monkeypatch.delenv("ROGMEM_RELEASES_URL", raising=False)
    from rogmem import engine as engine_mod

    engine_mod.connect(tmp_path, tmp_path / "scan")
    engine_mod.set_embed_mode("off")
    _Handler.calls.clear()
    _Handler.scenarios.update(status=200, body=_manifest(), etag='"v1"')
    return engine_mod


def _ok_url():
    return f"http://127.0.0.1:{_port}/ok.json"


def test_view_is_config_missing_before_a_url_is_set(engine):
    view = engine.release_view()
    assert view["display"] == "尚未配置发布核对源"
    assert view["error_kind"] == "config_missing"
    assert view["latest"] == ""


def test_setting_an_invalid_url_is_rejected(engine):
    result = engine.set_release_source_url("http://example.com/x")
    assert result["ok"] is False
    assert result["error_kind"] == "config_invalid"
    assert engine.release_view()["error_kind"] == "config_missing"


def test_setting_a_valid_loopback_url_and_refreshing_reads_the_tag(engine):
    assert engine.set_release_source_url(_ok_url())["ok"] is True
    result = engine.refresh_release()
    assert result["ok"] is True
    view = result["view"]
    assert view["latest"] == "V1-2611121012-PUB"
    assert view["error_kind"] == "ok"


def test_etag_round_trip_keeps_latest_and_does_not_reset_tag(engine):
    assert engine.set_release_source_url(_ok_url())["ok"] is True
    engine.refresh_release()
    _Handler.scenarios.update(status=304)
    time.sleep(0.01)
    engine.set_setting("release_manual_unix", "0")
    result = engine.refresh_release()
    assert result["ok"] is True
    assert result["view"]["latest"] == "V1-2611121012-PUB"
    assert result["view"]["error_kind"] == "ok"
    sent = [h.get("If-None-Match") for _, h in _Handler.calls if "If-None-Match" in h]
    assert '"v1"' in sent


def test_failure_keeps_last_success_tag_as_history_not_as_current_latest(engine):
    assert engine.set_release_source_url(_ok_url())["ok"] is True
    engine.refresh_release()
    assert engine.get_setting("release_latest") == "V1-2611121012-PUB"
    _Handler.scenarios.update(status=503)
    engine.set_setting("release_manual_unix", "0")
    result = engine.refresh_release()
    view = result["view"]
    assert view["last_success_tag"] == "V1-2611121012-PUB", "历史 tag 必须保留"
    assert view["freshness"] == "stale"
    assert "V1-2611121012-PUB" in view["display"], "历史 tag 要可见"
    assert "上次成功" in view["display"], "必须标注为历史，而不是当前最新"
    assert view["relation"] == "", "失败态不得宣称 relation"
    assert "已是最新" not in view["display"]
    assert view["error_kind"] == "server_error"
    assert view["fail_detail"]
    assert int(engine.get_setting("release_fail_streak") or "0") == 1


def test_manual_cooldown_is_bounded_by_streak_on_failure(engine):
    """Reviewer blocker: without a failure-side cooldown, the user can
    hammer the source forever. Bound it: first failure retry is free,
    but subsequent rapid retries must be throttled with a short,
    streak-based cooldown that stays under a minute.
    """
    assert engine.set_release_source_url(_ok_url())["ok"] is True
    _Handler.scenarios.update(status=500)

    first = engine.refresh_release()
    assert first["view"]["error_kind"] == "server_error"
    # First retry is allowed so the user can confirm the failure mode:
    second = engine.refresh_release()
    assert "cooled" not in second
    # By now streak is 2; the next click must be throttled.
    third = engine.refresh_release()
    assert third.get("cooled") is True, "连续失败必须有节流，否则会一直打源"


def test_manual_cooldown_after_success_blocks_rapid_reclick(engine):
    assert engine.set_release_source_url(_ok_url())["ok"] is True
    first = engine.refresh_release()
    assert first["view"]["error_kind"] == "ok"
    cooled = engine.refresh_release()
    assert cooled.get("cooled") is True


def test_backoff_grows_with_streak_but_is_capped(engine):
    assert engine.set_release_source_url(_ok_url())["ok"] is True
    for streak in range(1, 8):
        engine.set_setting("release_fail_streak", str(streak))
        wait = engine._next_probe_due_seconds()
        assert wait >= 5 * 60
        assert wait <= 30 * 60


def test_probe_is_due_only_after_backoff_passes(engine):
    assert engine.set_release_source_url(_ok_url())["ok"] is True
    engine.refresh_release()
    assert not engine._probe_due()
    engine.set_setting("release_probe_unix", "0")
    assert engine._probe_due()


def test_probe_release_if_due_runs_in_background_and_updates(engine):
    assert engine.set_release_source_url(_ok_url())["ok"] is True
    engine.set_setting("release_probe_unix", "0")
    engine.db().commit()
    engine.probe_release_if_due()
    engine.finish_release_probe()
    assert engine.get_setting("release_latest") == "V1-2611121012-PUB"


def test_not_modified_does_not_reset_latest_or_etag(engine):
    assert engine.set_release_source_url(_ok_url())["ok"] is True
    engine.refresh_release()
    etag_before = engine.get_setting("release_etag")
    tag_before = engine.get_setting("release_latest")
    _Handler.scenarios.update(status=304)
    engine.set_setting("release_manual_unix", "0")
    engine.refresh_release()
    assert engine.get_setting("release_latest") == tag_before
    assert engine.get_setting("release_etag") == etag_before


def test_schema_error_is_classified_as_schema_not_as_ok(engine):
    assert engine.set_release_source_url(_ok_url())["ok"] is True
    _Handler.scenarios.update(
        status=200, body=b'{"schema":"other/1","releases":[]}', etag='"v1"'
    )
    result = engine.refresh_release()
    assert result["view"]["error_kind"] == "schema"
    assert result["view"]["latest"] == ""


def test_failure_display_never_says_latest(engine):
    assert engine.set_release_source_url(_ok_url())["ok"] is True
    _Handler.scenarios.update(status=404)
    result = engine.refresh_release()
    view = result["view"]
    assert view["display"].startswith("请求失败，")
    assert "已是最新" not in view["display"]
    assert view["relation"] == ""


def test_env_url_overrides_db_setting(engine, monkeypatch):
    engine.set_release_source_url("https://example.com/releases.json")
    monkeypatch.setenv("ROGMEM_RELEASES_URL", _ok_url())
    result = engine.refresh_release()
    assert result["view"]["latest"] == "V1-2611121012-PUB"


def test_restart_recovers_from_settings_table(engine, tmp_path, monkeypatch):
    assert engine.set_release_source_url(_ok_url())["ok"] is True
    engine.refresh_release()
    latest = engine.get_setting("release_latest")
    engine.connect(tmp_path, tmp_path / "scan")
    assert engine.get_setting("release_latest") == latest
    view = engine.release_view()
    assert view["latest"] == latest


def test_get_release_view_is_a_pure_read_and_does_not_fetch(engine):
    assert engine.set_release_source_url(_ok_url())["ok"] is True
    engine.refresh_release()
    before = len(_Handler.calls)
    for _ in range(5):
        engine.release_view()
    assert len(_Handler.calls) == before


# ---- blocker 1: source identity binding ---------------------------------


def test_source_switch_invalidates_cached_tag(engine, monkeypatch):
    """Succeed on URL A, switch to URL B, view must not show A's tag."""
    assert engine.set_release_source_url(_ok_url())["ok"] is True
    engine.refresh_release()
    assert engine.release_view()["latest"] == "V1-2611121012-PUB"
    engine.set_release_source_url(f"http://127.0.0.1:{_port}/other.json")
    view = engine.release_view()
    assert view["latest"] == "", f"切源后不得暴露旧源的 tag，拿到：{view!r}"
    assert view["display"] != "V1-2611121012-PUB"
    assert view["display"] != "已是最新"
    assert "已是最新" not in view["display"]
    assert view["error_kind"] in {"never_checked", "config_pending"}


def test_env_override_to_a_different_source_invalidates_cached_tag(engine, monkeypatch):
    assert engine.set_release_source_url(_ok_url())["ok"] is True
    engine.refresh_release()
    monkeypatch.setenv("ROGMEM_RELEASES_URL", f"http://127.0.0.1:{_port}/other.json")
    view = engine.release_view()
    assert view["latest"] == "", f"env 覆盖到新源后不得暴露旧源 tag：{view!r}"
    assert "已是最新" not in view["display"]


def test_304_after_source_switch_does_not_resurrect_old_cache(engine):
    """Fingerprint-tagged ETag: a 304 can only confirm the SAME source."""
    assert engine.set_release_source_url(_ok_url())["ok"] is True
    engine.refresh_release()
    engine.set_release_source_url(f"http://127.0.0.1:{_port}/other.json")
    engine.set_setting("release_manual_unix", "0")
    engine.db().commit()
    _Handler.scenarios.update(status=304)
    engine.refresh_release()
    view = engine.release_view()
    assert view["latest"] == "", f"304 不应复活旧源的 cache：{view!r}"
    assert "已是最新" not in view["display"]


def test_in_flight_outcome_after_source_change_is_discarded(engine):
    """An outcome produced for URL A must not overwrite state after we switched to URL B."""
    from rogmem.release_source import FetchOutcome

    assert engine.set_release_source_url(_ok_url())["ok"] is True
    before_latest = engine.get_setting("release_latest")
    before_fingerprint = engine.get_setting("release_source_fingerprint")
    assert engine.set_release_source_url(f"http://127.0.0.1:{_port}/other.json")["ok"] is True
    outcome = FetchOutcome(
        kind="ok",
        body=_manifest("V1-2690101010-PUB"),
        etag='"stale"',
        status=200,
    )
    engine._apply_outcome_for_source(outcome, before_fingerprint)
    view = engine.release_view()
    assert view["latest"] != "V1-2690101010-PUB", (
        "过期来源的 outcome 不得写入当前来源的缓存"
    )
