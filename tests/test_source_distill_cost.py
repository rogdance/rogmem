"""Per-source distill cost: every tagged call counts, money priced now."""

from datetime import datetime, timezone

import pytest

from rogmem import cost
from rogmem.ui import source_detail, source_row_html, sources_page


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    return engine


def test_bind_source_tags_every_call_including_retries(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    cost.save_book("7", [{"provider": "p", "model": "m", "currency": "CNY", "input": 1, "output": 2, "cache": 0.5}])
    with cost.bind_source("src-a"):
        cost.record(at="2026-11-20T01:00:00+00:00", worker="草稿", provider_id="p", model="m", usage=(100, 10, 0))
        cost.record(at="2026-11-20T01:01:00+00:00", worker="草稿", provider_id="p", model="m", usage=(50, 5, 0))
    cost.record(
        at="2026-11-20T01:02:00+00:00",
        worker="草稿",
        provider_id="p",
        model="m",
        usage=(999, 9, 0),
        source_id="src-b",
    )
    # Untagged call must not land on either source.
    cost.record(at="2026-11-20T01:03:00+00:00", worker="草稿", provider_id="p", model="m", usage=(1, 1, 0))

    a = cost.total_for_group(["src-a"])
    b = cost.totals_for(["src-b"])["src-b"]
    assert a["tokens"] == 165  # 110 + 55
    assert b["tokens"] == 1008
    assert a["money"] == pytest.approx((100 + 50) * 1 / 1_000_000 + (10 + 5) * 2 / 1_000_000)
    overview = cost.overview(now=datetime(2026, 11, 20, 2, 0, tzinfo=timezone.utc), span="24h")
    assert overview["summary"]["tokens"] == 165 + 1008 + 2


def test_failed_retry_and_success_all_accumulate(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    cost.save_book("7", [{"provider": "p", "model": "m", "currency": "CNY", "input": 1, "output": 1, "cache": 1}])
    with cost.bind_source("file-1"):
        cost.record(at="2026-11-20T02:00:00+00:00", worker="并页", provider_id="p", model="m", usage=(10, 0, 0))
        cost.record(at="2026-11-20T02:01:00+00:00", worker="并页", provider_id="p", model="m", usage=None)
        cost.record(at="2026-11-20T02:02:00+00:00", worker="并页", provider_id="p", model="m", usage=(20, 5, 0))
    total = engine.distill_cost_for("file-1")
    assert total["tokens"] == 35
    assert total["bare"] == 1
    assert total["money"] == pytest.approx(35 / 1_000_000)


def test_session_turn_mates_share_one_distill_total(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    cost.save_book("7", [{"provider": "p", "model": "m", "currency": "CNY", "input": 1, "output": 1, "cache": 1}])
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,turn_id,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES('u1','user','turn-9','2026-11-20T00:00:00+00:00','h1','问','萃取已结晶','fast',1)"""
    )
    con.execute(
        """INSERT INTO sources(id,kind,turn_id,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES('a1','agent','turn-9','2026-11-20T00:00:01+00:00','h2','答','萃取已结晶','fast',1)"""
    )
    con.commit()
    with cost.bind_source("u1"):
        cost.record(at="2026-11-20T03:00:00+00:00", worker="草稿", provider_id="p", model="m", usage=(100, 20, 0))
    user = engine.distill_cost_for("u1")
    agent = engine.distill_cost_for("a1")
    assert user["tokens"] == agent["tokens"] == 120
    assert user["money"] == agent["money"]


def test_list_and_detail_show_token_and_money(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    cost.save_book("7", [{"provider": "p", "model": "m", "currency": "CNY", "input": 2, "output": 0, "cache": 0}])
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,filename,ingested_at,content_hash,body,status,lane,pipeline)
           VALUES('f1','file','简历.md','2026-11-20T00:00:00+00:00','h','正文','萃取已结晶','fast','完成:ok')"""
    )
    con.commit()
    with cost.bind_source("f1"):
        cost.record(at="2026-11-20T04:00:00+00:00", worker="草稿", provider_id="p", model="m", usage=(1_000_000, 0, 0))

    listed = engine.list_sources(tab="file")
    row = listed["rows"][0]
    assert row["id"] == "f1"
    assert row["cost_tokens"] == 1_000_000
    assert row["cost_money"] == pytest.approx(2.0)

    html = sources_page(listed["rows"], "file", {}, {}, listed)
    assert "<th class='fit num'>Token</th>" in html
    assert "<th class='fit num'>金额</th>" in html
    assert "1,000,000" in html
    assert "¥2.00" in html

    detail = engine.source_detail("f1")
    page = source_detail(detail["source"], detail["statements"], detail["mate"], "file")
    assert "aria-label='萃取成本'" in page
    assert "1,000,000" in page
    assert "¥2.00" in page

    cell = source_row_html(row, "file")
    assert "1,000,000" in cell
    assert "¥2.00" in cell


def test_unpriced_call_shows_as_unknown_money(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    with cost.bind_source("x"):
        cost.record(at="2026-11-20T05:00:00+00:00", worker="速写", provider_id="p", model="m", usage=(10, 1, 0))
    total = cost.total_for_group(["x"])
    assert total["tokens"] == 11
    assert total["money"] is None
    assert total["unpriced"] == 1
    html = source_row_html({"id": "x", "kind": "file", "filename": "a.md", "cost": total, "cost_tokens": 11, "cost_money": None, "cost_unpriced": 1, "pipeline": "", "status": "", "note": ""}, "file")
    assert "未计入" in html
