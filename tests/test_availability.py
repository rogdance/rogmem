"""探活色块：一次探活只代表它被下次探活或静默上限截断之前的那段时间。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rogmem.availability import choose_target, for_chart, measure, tenures, tone
from rogmem.ui import cost_page, queue_page


def test_a_probe_lasts_until_the_next_one_and_a_long_silence_stays_unknown():
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    hour = timedelta(hours=1)
    slot = (start.isoformat(), start, start + hour)
    held = measure([(start, "ok"), (start + timedelta(minutes=20), "warn")], [slot], timedelta(hours=2))
    assert held[0]["ok"] == 20 * 60
    assert held[0]["warn"] == 40 * 60
    assert held[0]["unknown"] == 0
    gap = measure([(start, "ok")], [slot], timedelta(minutes=10))
    assert gap[0]["ok"] == 10 * 60
    assert gap[0]["unknown"] == 50 * 60
    assert tone(gap[0]["ok"], gap[0]["warn"], gap[0]["down"], gap[0]["unknown"]) == ("ok", 10 / 60)


def test_tone_follows_the_longer_impairment_and_its_share_of_the_slot():
    assert tone(3600, 0, 0, 0) == ("ok", 1.0)
    assert tone(0, 0, 0, 3600) == ("unknown", 0.0)
    assert tone(2700, 900, 0, 0) == ("warn", 0.25)
    assert tone(900, 2700, 0, 0) == ("warn", 0.75)
    assert tone(0, 0, 3600, 0) == ("down", 1.0)
    assert tone(1800, 0, 1800, 0) == ("down", 0.5)
    # 降级更久时色块用黄，深度只计算降级占这一格的比例，不把较短的不可用算进去。
    assert tone(0, 3000, 600, 0) == ("warn", 3000 / 3600)
    assert tone(3500, 0, 0, 100) == ("ok", 3500 / 3600)
    assert tone(1800, 0, 0, 1800) == ("ok", 0.5)
    assert tone(0, 0, 3420, 180) == ("down", 3420 / 3600)


def test_model_name_stays_visible_when_it_sits_inside_the_provider_name():
    from rogmem.availability import _call_label

    assert _call_label([{"id": "p", "name": "MiniMax-Text"}], "p", "Text") == "MiniMax-Text · Text"


def test_minutes_before_any_call_stay_unobserved():
    from rogmem.availability import _mix_slots

    start = datetime(2024, 5, 4, tzinfo=timezone.utc)
    called = start + timedelta(minutes=20)
    end = start + timedelta(hours=1)
    bucket = _mix_slots(
        [("slot", start, end)],
        [(called, end, "other", "Qwen")],
        {"other": [(called, "ok"), (end, "ok")]},
        timedelta(hours=2),
    )[0]
    assert bucket["span"] == 3600
    assert bucket["unknown"] == 20 * 60
    assert bucket["ok"] == 40 * 60


def test_a_call_does_not_paint_the_time_before_it():
    start = datetime(2024, 5, 1, 1, tzinfo=timezone.utc)
    called = start + timedelta(minutes=30)
    end = start + timedelta(hours=1)
    spans = tenures(
        [(called, "other", "硅基 · Qwen")],
        start,
        end,
        ("mini", "MiniMax · M3"),
    )
    assert [(item[2], item[0], item[1]) for item in spans] == [
        ("mini", start, called),
        ("other", called, end),
    ]


def test_a_local_offset_still_names_the_model_that_was_called(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.set_setting("probe_interval", "7200")
    start = datetime(2024, 5, 3, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    local = (start + timedelta(minutes=30)).astimezone(timezone(timedelta(hours=8)))
    from rogmem import cost
    from rogmem.availability import note

    cost.record(
        at=local.isoformat(),
        worker="初始蒸馏",
        provider_id="other",
        model="Qwen",
        usage=(1, 1, 0),
        prompt="a",
        answer="b",
    )
    note(engine.db(), "other", "down", start.isoformat())
    note(engine.db(), "other", "ok", end.isoformat())
    note(engine.db(), engine.DEFAULT_MINIMAX_ID, "ok", start.isoformat())
    engine.db().commit()
    found = for_chart([_series(start, 1)[0]], _window(start, end))
    bucket = found["buckets"][0]
    assert any(item["name"] == "other · Qwen" and item["seconds"] == 30 * 60 for item in bucket["who"])
    assert bucket["down"] == 30 * 60
    assert bucket["ok"] == 30 * 60


def test_a_later_call_switches_the_model_the_square_follows():
    start = datetime(2024, 5, 1, 1, tzinfo=timezone.utc)
    switch = start + timedelta(hours=1, minutes=15)
    end = start + timedelta(hours=2)
    spans = tenures(
        [
            (start + timedelta(minutes=5), "mini", "MiniMax · M3"),
            (switch, "other", "硅基 · Qwen"),
        ],
        start,
        end,
        ("mini", "MiniMax · M3"),
    )
    assert [(item[2], item[3]) for item in spans] == [("mini", "MiniMax · M3"), ("other", "硅基 · Qwen")]
    assert spans[0][1] == switch
    assert tenures([], start, end, ("mini", "MiniMax · M3")) == [(start, end, "mini", "MiniMax · M3")]
    assert tenures([], start, end, None) == []


def test_each_hour_uses_the_model_called_in_that_hour(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.set_setting("probe_interval", "7200")
    start = datetime(2024, 5, 2, 1, tzinfo=timezone.utc)
    second = start + timedelta(hours=1)
    end = start + timedelta(hours=2)
    from rogmem import cost
    from rogmem.availability import note

    cost.record(
        at=start.isoformat(),
        worker="初始蒸馏",
        provider_id=engine.DEFAULT_MINIMAX_ID,
        model="MiniMax-M3.1-Flash",
        usage=(1, 1, 0),
        prompt="a",
        answer="b",
    )
    cost.record(
        at=second.isoformat(),
        worker="向量嵌入",
        provider_id="embedder",
        model="embed",
        usage=(1, 0, 0),
        prompt="a",
        answer="b",
    )
    cost.record(
        at=second.isoformat(),
        worker="初始蒸馏",
        provider_id="other",
        model="Qwen",
        usage=(1, 1, 0),
        prompt="a",
        answer="b",
    )
    note(engine.db(), engine.DEFAULT_MINIMAX_ID, "ok", start.isoformat())
    note(engine.db(), "other", "down", second.isoformat())
    note(engine.db(), "other", "ok", end.isoformat())
    engine.db().commit()
    found = for_chart(_series(start, 2), _window(start, end))
    first, second_bucket = found["buckets"]
    assert first["ok"] == 3600
    assert first["down"] == 0
    assert first["who"][0]["name"].startswith("MiniMax")
    assert second_bucket["down"] == 3600
    assert second_bucket["ok"] == 0
    assert second_bucket["who"][0]["name"] == "other · Qwen"


def test_the_strip_follows_the_assigned_model_not_a_dormant_name():
    minimax = {"id": "minimax-cn", "name": "MiniMax", "base_url": "https://api.minimax.cn/v1", "models": ["MiniMax-M3.1-Flash"], "model_kinds": {"MiniMax-M3.1-Flash": "chat"}}
    other = {"id": "sf", "name": "硅基", "base_url": "https://api.siliconflow.cn/v1", "models": ["Qwen"], "model_kinds": {"Qwen": "chat"}}
    chosen, model = choose_target([other, minimax], "sf|Qwen")
    assert chosen["id"] == "sf"
    assert model == "Qwen"
    assigned, assigned_model = choose_target(
        [minimax, {"id": "mini-2", "name": "MiniMax 备用", "base_url": "https://api.minimax.cn/v1", "models": ["其他"], "model_kinds": {"其他": "chat"}}],
        "mini-2|其他",
    )
    assert assigned["id"] == "mini-2"
    assert assigned_model == "其他"
    fallback, fallback_model = choose_target([other, minimax], "")
    assert fallback["id"] == "minimax-cn"
    assert fallback_model == "MiniMax-M3.1-Flash"
    assert choose_target([], "") is None


def _open(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _seed(engine) -> tuple[datetime, datetime]:
    """四格：没人探过、整格可用、45 分钟降级、整格不可用。"""

    gap = datetime(2024, 4, 2, 1, tzinfo=timezone.utc)
    ok = gap + timedelta(hours=1)
    warn = ok + timedelta(hours=1)
    down = warn + timedelta(hours=1)
    end = down + timedelta(hours=1)
    engine.set_setting("probe_interval", "7200")
    from rogmem.availability import note

    for moment, health in (
        (ok, "ok"),
        (warn, "warn"),
        (warn + timedelta(minutes=45), "ok"),
        (down, "down"),
        (end, "ok"),
    ):
        note(engine.db(), engine.DEFAULT_MINIMAX_ID, health, moment.isoformat())
    engine.db().commit()
    return gap, end


def _series(start: datetime, count: int) -> list[dict]:
    return [
        {
            "at": (start + timedelta(hours=i)).isoformat(),
            "label": "slot",
            "grain": "hour",
            "money": 1.0,
            "money_known": 1.0,
            "tokens": 10,
            "input": 6,
            "output": 4,
            "cache": 0,
            "unpriced": 0,
            "bare": 0,
            "tasks": 1,
            "chars": 10,
        }
        for i in range(count)
    ]


def _window(start: datetime, end: datetime) -> dict:
    return {"start": start.isoformat(), "end": end.isoformat(), "grain": "hour", "tz": "UTC+8"}


def test_stamped_probe_is_kept_and_the_chart_splits_the_hour(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine._stamp_health(engine.DEFAULT_MINIMAX_ID, {"health": "warn", "health_detail": "HTTP 429", "remark": "HTTP 429"})
    row = engine.db().execute("SELECT provider_id, health FROM provider_health").fetchone()
    assert row["provider_id"] == engine.DEFAULT_MINIMAX_ID
    assert row["health"] == "warn"

    start, end = _seed(engine)
    found = for_chart(_series(start, 4), _window(start, end))
    assert found["name"] == "MiniMax"
    assert found["model"] == "MiniMax-M3.1-Flash"
    by_share = {(item["ok"], item["warn"], item["down"], item["unknown"]) for item in found["buckets"]}
    assert (0, 0, 0, 3600) in by_share
    assert (3600, 0, 0, 0) in by_share
    assert (900, 2700, 0, 0) in by_share
    assert (0, 0, 3600, 0) in by_share


def _payload(html: str, name: str) -> dict:
    match = re.search(rf"var {name} = (\{{.*?\}});", html, re.DOTALL)
    assert match, html[html.find(f"var {name}") : html.find(f"var {name}") + 200]
    return json.loads(match.group(1))


def _cost_board(series, window) -> dict:
    return {
        "span": "24h",
        "worker": "",
        "model": "",
        "detail": "24h",
        "dworker": "",
        "dmodel": "",
        "page": 1,
        "pages": 1,
        "count": 0,
        "summary": {"money": 1.0, "tokens": 10, "input": 6, "output": 4, "cache": 0, "unpriced": 0, "bare": 0},
        "series": series,
        "window": window,
        "rows": [],
        "workers": [],
        "models": [],
        "rate": None,
        "observed": True,
    }


def _queue_board(series, window) -> dict:
    return {
        "span": "24h",
        "tab": "all",
        "page": 1,
        "pages": 1,
        "page_size": 50,
        "total": 0,
        "rows": [],
        "counts": {"all": 0, "done": 0, "queued": 0, "leased": 0},
        "summary": {
            "done_tasks": 0,
            "done_chars": 0,
            "queued_tasks": 0,
            "queued_chars": 0,
            "per_hour_tasks": 0,
            "per_hour_chars": 0,
            "running": 0,
            "stopped": 0,
            "untimed_tasks": 0,
        },
        "series": series,
        "window": window,
    }


def test_both_pages_carry_the_same_probe_mix(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    start, end = _seed(engine)
    series = _series(start, 4)
    window = _window(start, end)
    cost = _payload(cost_page(_cost_board(series, window), [], {}), "COST")
    queue = _payload(queue_page(_queue_board(series, window)), "QUEUE")
    assert cost["availability"]["buckets"] == queue["availability"]["buckets"]
    assert "占比越久" in cost_page(_cost_board(series, window), [], {})
    assert "空心" in queue_page(_queue_board(series, window))
    assert "__AVAIL_JS__" not in cost_page(_cost_board([], window), [], {})


def test_a_closed_database_still_renders_the_strip_slot():
    from rogmem import engine

    previous = engine._DATA
    engine._DATA = None
    try:
        start = datetime(2024, 4, 2, tzinfo=timezone.utc)
        page = cost_page(_cost_board(_series(start, 2), _window(start, start + timedelta(hours=2))), [], {})
    finally:
        engine._DATA = previous
    data = _payload(page, "COST")
    assert [item["unknown"] for item in data["availability"]["buckets"]] == [3600, 3600]
    assert "availTone" in page


def test_an_unobserved_edge_keeps_only_the_minutes_inside_the_window():
    from rogmem import engine

    previous = engine._DATA
    engine._DATA = None
    try:
        hour = datetime(2024, 4, 2, 1, tzinfo=timezone.utc)
        start = hour + timedelta(minutes=20)
        end = hour + timedelta(hours=1)
        found = for_chart(
            [{"at": hour.isoformat(), "grain": "hour"}],
            {"start": start.isoformat(), "end": end.isoformat(), "grain": "hour"},
        )
    finally:
        engine._DATA = previous
    assert found["buckets"][0]["span"] == 40 * 60
    assert found["buckets"][0]["unknown"] == 40 * 60
    assert found["buckets"][0]["ok"] == 0
