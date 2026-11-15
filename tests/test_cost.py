"""Call cost is computed when read. The ledger stores tokens, not money."""

import json
import re
import urllib.error
from datetime import datetime, timezone

import pytest

from rogmem import cost
from rogmem.ui import NAV, cost_page, models_page


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    return engine


def _board(**changes):
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
        "summary": {"money": None, "tokens": 0, "input": 0, "output": 0, "cache": 0, "unpriced": 0, "bare": 0},
        "series": [],
        "rows": [],
        "workers": list(cost.WORKERS),
        "models": [],
        "rate": None,
    }
    board.update(changes)
    return board


def test_usage_shapes_share_one_meaning():
    openai = {"usage": {"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 40}}}
    anthropic = {"usage": {"input_tokens": 60, "cache_read_input_tokens": 40, "output_tokens": 10}}
    assert cost.read_usage(openai) == cost.read_usage(anthropic) == (100, 10, 40)
    assert cost.read_usage({"usage": {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 50}}}) == (10, 0, 10)
    assert cost.read_usage({"usage": {"total_tokens": 12}}) == (12, 0, 0)
    created = {
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 6,
            "output_tokens": 1,
        }
    }
    assert cost.read_usage(created) == (20, 1, 4)
    assert cost.read_usage({}) is None
    assert cost.read_usage({"usage": {}}) is None
    assert cost.total_tokens(100, 10) == 110
    assert cost.total_tokens(None, None) is None


def test_money_prices_fresh_and_cache_separately():
    price = cost.Price("CNY", 2, 3, 0.5)
    assert cost.money_cny(100, 10, 40, price, None) == pytest.approx(170 / 1_000_000)
    assert cost.money_cny(1_000_000, 0, 0, cost.Price("USD", 1, 1, 1), None) is None
    assert cost.money_cny(1_000_000, 0, 0, cost.Price("USD", 1, 1, 1), 7) == pytest.approx(7)
    assert cost.money_cny(1_000_000, 0, 0, None, 7) is None
    assert cost.money_cny(1_000_000, 50, 20, cost.Price("CNY", 0, 0, 0), None) == 0


def test_history_is_each_day_not_a_running_total(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    now = datetime(2026, 11, 11, 4, 0, tzinfo=timezone.utc)
    cost.record(
        at="2026-11-01T04:00:00+00:00",
        worker="初始蒸馏",
        provider_id="p",
        model="m",
        usage=(1000, 0, 0),
        prompt="第一天",
        answer="",
    )
    cost.record(
        at="2026-11-02T04:00:00+00:00",
        worker="初始蒸馏",
        provider_id="p",
        model="m",
        usage=(100, 0, 0),
        prompt="第二天",
        answer="",
    )
    view = cost.overview( now=now, span="all")
    assert [item["tokens"] for item in view["series"] if item["tokens"]] == [1000, 100]


def test_cache_is_inside_input_and_a_missing_price_is_not_zero(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    now = datetime(2026, 11, 11, 4, 0, tzinfo=timezone.utc)
    cost.record(
        at="2026-11-11T03:00:00+00:00",
        worker="并页",
        provider_id="p",
        model="m",
        usage=(100, 10, 40),
        prompt="材料",
        answer="一页",
    )
    view = cost.overview( now=now, span="24h")
    assert view["summary"]["tokens"] == 110
    assert view["summary"]["cache"] == 40
    assert view["summary"]["money"] is None
    assert view["summary"]["unpriced"] == 1
    cost.save_book(
        "7.2",
        [{"provider": "p", "model": "m", "currency": "USD", "input": "2", "output": "3", "cache": "0.5"}],
    )
    priced = cost.overview( now=now, span="24h")
    assert priced["summary"]["money"] == pytest.approx((60 * 2 + 40 * 0.5 + 10 * 3) / 1_000_000 * 7.2)
    cost.save_book( "", [{"provider": "p", "model": "m", "currency": "USD", "input": "2", "output": "3", "cache": "0.5"}])
    assert cost.overview( now=now, span="24h")["summary"]["money"] is None


def test_detail_filters_do_not_move_the_overview(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    now = datetime(2026, 11, 11, 4, 0, tzinfo=timezone.utc)
    for at, worker, usage in (
        ("2026-10-01T04:00:00+00:00", "初始蒸馏", (1000, 0, 0)),
        ("2026-11-11T03:00:00+00:00", "并页", (10, 0, 0)),
    ):
        cost.record(
            at=at,
            worker=worker,
            provider_id="p",
            model="m",
            usage=usage,
            prompt=worker,
            answer="",
        )
    view = cost.overview( now=now, span="all", worker="初始蒸馏", detail="24h")
    assert view["summary"]["tokens"] == 1000
    assert view["count"] == 1
    assert view["rows"][0]["worker"] == "并页"
    assert any(item["tokens"] == 1000 for item in view["series"])


def test_calls_page_fifty_at_a_time(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    now = datetime(2026, 11, 11, 4, 0, tzinfo=timezone.utc)
    for minute in range(51):
        cost.record(
            at=f"2026-11-11T03:{minute:02d}:00+00:00",
            worker="速写",
            provider_id="p",
            model="m",
            usage=(1, 1, 0),
            prompt="p",
            answer="a",
        )
    first = cost.overview( now=now, span="24h", detail="all", page=1)
    second = cost.overview( now=now, span="24h", detail="all", page=2)
    third = cost.overview( now=now, span="24h", detail="all", page=3)
    assert first["count"] == 51
    assert first["pages"] == 3
    assert first["page_size"] == 20
    assert len(first["rows"]) == 20
    assert first["rows"][0]["at"] == "11-11 11:50"
    assert len(second["rows"]) == 20
    assert len(third["rows"]) == 11
    assert cost.overview( now=now, span="24h", detail="all", page=9)["page"] == 3


def test_a_price_row_is_complete_or_absent(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    cost.save_book(
        "7.2",
        [{"provider": "p", "model": "m", "currency": "CNY", "input": "1", "output": "2", "cache": "0"}],
    )
    with pytest.raises(cost.BadBook):
        cost.save_book(
            "7.2",
            [{"provider": "p", "model": "m", "currency": "CNY", "input": "1", "output": "", "cache": ""}],
        )
    assert cost.load_prices()[("p", "m")].output == 2
    with pytest.raises(cost.BadBook):
        cost.save_book(
            "0",
            [{"provider": "p", "model": "m", "currency": "CNY", "input": "1", "output": "2", "cache": "0"}],
        )
    with pytest.raises(cost.BadBook):
        cost.save_book(
            "-1",
            [{"provider": "p", "model": "m", "currency": "CNY", "input": "1", "output": "2", "cache": "0"}],
        )
    with pytest.raises(cost.BadBook):
        cost.save_book(
            "1",
            [
                {"provider": "p", "model": "m", "currency": "CNY", "input": "1", "output": "1", "cache": "1"},
                {"provider": "p", "model": "m", "currency": "USD", "input": "2", "output": "2", "cache": "2"},
            ],
        )
    cost.save_book(
        "",
        [{"provider": "p", "model": "m", "currency": "CNY", "input": "", "output": "", "cache": ""}],
    )
    assert cost.load_prices() == {}
    assert cost.load_rate() is None


def test_an_unpriced_call_keeps_the_total_unknown(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    now = datetime(2026, 11, 11, 4, 0, tzinfo=timezone.utc)
    cost.record(
        at="2026-11-11T02:00:00+00:00",
        worker="初始蒸馏",
        provider_id="p",
        model="priced",
        usage=(1_000_000, 0, 0),
        prompt="已标价",
        answer="",
    )
    cost.record(
        at="2026-11-11T03:00:00+00:00",
        worker="并页",
        provider_id="p",
        model="open",
        usage=(10, 0, 0),
        prompt="未标价",
        answer="",
    )
    cost.save_book(
        "1",
        [{"provider": "p", "model": "priced", "currency": "CNY", "input": "2", "output": "0", "cache": "0"}],
    )
    view = cost.overview(now=now, span="24h")
    assert view["summary"]["money"] is None
    assert view["summary"]["tokens"] == 1_000_010
    assert view["summary"]["unpriced"] == 1
    cost.save_book(
        "1",
        [
            {"provider": "p", "model": "priced", "currency": "CNY", "input": "2", "output": "0", "cache": "0"},
            {"provider": "p", "model": "open", "currency": "CNY", "input": "0", "output": "0", "cache": "0"},
        ],
    )
    done = cost.overview(now=now, span="24h")
    assert done["summary"]["money"] == pytest.approx(2)
    assert done["summary"]["unpriced"] == 0


def test_recording_does_not_wait_on_the_memory_database(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    held = engine.db()
    held.execute("INSERT INTO settings(k,v) VALUES('lockprobe','1')")
    engine._meter_chat(
        "https://example.test/v1",
        "m",
        [{"role": "user", "content": "材料"}],
        "答",
        {"usage": {"prompt_tokens": 10, "completion_tokens": 2}},
        "distill",
    )
    view = cost.overview(now=datetime.now(timezone.utc), span="all")
    assert view["count"] == 1
    assert view["rows"][0]["worker"] == "模型调用"
    assert view["rows"][0]["total"] == 12
    names = {row[0] for row in held.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "model_calls" not in names
    held.rollback()


def test_overview_read_does_not_wait_behind_a_call_writer(tmp_path, monkeypatch):
    import sqlite3
    import time

    _open(tmp_path, monkeypatch)
    cost.record(
        at="2026-11-11T03:00:00+00:00",
        worker="初始蒸馏",
        provider_id="p",
        model="m",
        usage=(10, 0, 0),
        prompt="先记下",
        answer="",
    )
    # Warm the process schema flag so the next read skips DDL.
    assert cost.overview(now=datetime(2026, 11, 11, 4, 0, tzinfo=timezone.utc), span="24h")["count"] == 1
    writer = sqlite3.connect(tmp_path / "cost.sqlite", timeout=1)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            """INSERT INTO model_calls(at, worker, provider_id, model, input_tokens, output_tokens, cache_tokens, prompt, answer)
               VALUES('2026-11-11T03:01:00+00:00','初始蒸馏','p','m',1,0,0,'占着写锁','')"""
        )
        started = time.perf_counter()
        view = cost.overview(now=datetime(2026, 11, 11, 4, 0, tzinfo=timezone.utc), span="24h")
        assert time.perf_counter() - started < 1.0
        assert view["count"] >= 1
    finally:
        writer.rollback()
        writer.close()


def test_overview_window_skips_older_ledger_rows(tmp_path, monkeypatch):
    _open(tmp_path, monkeypatch)
    now = datetime(2026, 11, 11, 4, 0, tzinfo=timezone.utc)
    for at, tokens in (
        ("2025-01-01T04:00:00+00:00", 9_000_000),
        ("2026-11-11T03:00:00+00:00", 10),
    ):
        cost.record(
            at=at,
            worker="初始蒸馏",
            provider_id="p",
            model="m",
            usage=(tokens, 0, 0),
            prompt=at,
            answer="",
        )
    view = cost.overview(now=now, span="30d", detail="30d")
    assert view["summary"]["tokens"] == 10
    assert view["count"] == 1


def test_the_worker_is_whoever_is_asking(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)

    @cost.charging("并页")
    def ask():
        assert cost.current_worker() == "并页"
        engine._meter_chat(
            "https://example.test/v1",
            "m",
            [{"role": "user", "content": "材料" * 20}],
            "修订",
            {"usage": {"prompt_tokens": 5, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 1}}},
            "distill",
        )

    ask()
    assert cost.current_worker() == ""
    view = cost.overview( now=datetime.now(timezone.utc), span="24h")
    assert view["rows"][0]["worker"] == "并页"
    assert view["rows"][0]["input"] == 5
    assert view["rows"][0]["total"] == 7
    assert view["rows"][0]["prompt"].endswith("…")


def test_a_failed_call_is_not_recorded_and_embed_still_returns(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)

    def refuse(*_args, **_kwargs):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(engine.urllib.request, "urlopen", refuse)
    with pytest.raises(engine.ModelCallFailed):
        engine._complete_chat(
            "https://example.test/v1",
            "key",
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert cost.overview( now=datetime.now(timezone.utc), span="all")["count"] == 0

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {"data": [{"index": 0, "embedding": [0.25]}], "usage": {"prompt_tokens": 8, "total_tokens": 8}}
            ).encode()

    monkeypatch.setattr(engine.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert engine._remote_embed_call({"base_url": "https://example.test/v1", "api_key": "k"}, "embed", ["hello"]) == [[0.25]]
    row = cost.overview( now=datetime.now(timezone.utc), span="all")["rows"][0]
    assert row["worker"] == "向量嵌入"
    assert row["input"] == 8
    assert row["total"] == 8


def test_a_page_key_places_itself_in_its_section():
    from rogmem.ui import page as render

    queue = render("队列", "queue", "<p></p>")
    header = queue.split("<header>", 1)[1].split("</header>", 1)[0]
    assert "class='on' href='/admin/costs'>监控中心" in header
    side = queue.split("<nav class='side'>", 1)[1].split("</nav>", 1)[0]
    assert "class='on' href='/admin/settings/queue'>队列状态" in side
    assert "基础设置" not in side
    basic = render("基础", "basic", "<p></p>")
    header = basic.split("<header>", 1)[1].split("</header>", 1)[0]
    assert "class='on' href='/admin/settings/basic'>系统设置" in header
    side = basic.split("<nav class='side'>", 1)[1].split("</nav>", 1)[0]
    assert "class='on' href='/admin/settings/basic'>基础设置" in side
    assert side.index("基础设置") < side.index("宿主管理") < side.index("模型管理")
    assert "运行日志" not in side and "队列状态" not in side
    assert "采集管理" not in side
    assert "模型管理" in side and "管线设置" in side and "关于ROGMEM" in side
    hosts = render("宿主", "hosts", "<p></p>")
    header = hosts.split("<header>", 1)[1].split("</header>", 1)[0]
    assert "class='on' href='/admin/settings/basic'>系统设置" in header
    desktop = header.split("<nav>", 1)[1].split("</nav>", 1)[0]
    primary = re.sub(r"<span class='nav-sub'[\s\S]*?</span></span>", "", desktop)
    assert "宿主管理" not in primary
    assert "宿主管理" in desktop
    side = hosts.split("<nav class='side'>", 1)[1].split("</nav>", 1)[0]
    assert "class='on' href='/admin/hosts'>宿主管理" in side


def test_cost_is_its_own_page_and_models_stay_a_supplier_list():
    labels = [item[1] for item in NAV]
    assert labels.index("监控中心") + 1 == labels.index("系统设置")
    assert "成本明细" not in labels
    assert "宿主管理" not in labels
    page = cost_page(
        _board(),
        [{"id": "p", "name": "甲", "models": ["one", "two"]}],
        {},
    )
    assert "成本明细" in page
    assert "历史至今" in page
    assert "单价配置" in page
    assert "每百万token" in page
    assert "总 token" in page
    assert page.count("class='rate-row'") == 2
    assert "甲" in page
    content = page.split('<div class="content">', 1)[1]
    assert "基础设置" not in content
    assert "<th>资料</th>" not in page
    side = page.split("<nav class='side'>", 1)[1].split("</nav>", 1)[0]
    assert side.index("成本明细") < side.index("队列状态") < side.index("运行日志")
    assert "class='on' href='/admin/costs'>成本明细" in side
    header = page.split("<header>", 1)[1].split("</header>", 1)[0]
    assert "class='on' href='/admin/costs'>监控中心" in header
    models = models_page(
        [{"id": "p1", "name": "MiniMax", "base_url": "https://example.test", "models": ["m"], "key_hint": "已填写"}],
        {},
        {"source": "none"},
    )
    assert "单价配置" not in models
    assert models.index("监控中心") < models.index("系统设置")
    settings_side = models.split("<nav class='side'>", 1)[1].split("</nav>", 1)[0]
    assert "队列状态" not in settings_side
    assert "运行日志" not in settings_side
    assert "成本明细" not in settings_side


def test_the_page_saves_a_book_and_rejects_a_partial_one(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from starlette.testclient import TestClient

    from rogmem.app import build_app

    client = TestClient(build_app())
    page = client.get("/admin/costs").text
    assert "成本明细" in page
    assert "单价配置" in page
    partial = client.post(
        "/admin/api/costs",
        json={"rate": "7", "items": [{"provider": "p", "model": "m", "currency": "CNY", "input": "1", "output": "", "cache": ""}]},
    )
    assert partial.status_code == 400
    saved = client.post(
        "/admin/api/costs",
        json={"rate": "7.1", "items": [{"provider": "p", "model": "m", "currency": "USD", "input": "1", "output": "2", "cache": "0"}]},
    )
    assert saved.status_code == 200
    assert cost.load_rate() == pytest.approx(7.1)
    assert cost.load_prices()[("p", "m")].currency == "USD"
