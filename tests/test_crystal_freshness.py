"""结晶详情页在类型那一行右侧记下正文和速写各自的写入时刻。"""

from pathlib import Path

from rogmem.ui import crystal_page


def _open(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _meta_row(html: str) -> str:
    head = html.split("<div class='entry-head'>", 1)[1].split("</div>", 1)[0]
    assert "<p class='meta'>" in head
    return head.split("<p class='meta'>", 1)[1].split("</p>", 1)[0]


def test_detail_row_shows_body_and_sketch_write_times(tmp_path, monkeypatch):
    """没记下的时刻留空。正文和速写的写入共用库里的那一只钟，详情页用现成的 time 显示。"""
    clock = {"now": "2026-01-01T00:00:00+00:00"}
    monkeypatch.setattr("rogmem.crystal_store.utcnow_iso", lambda: clock["now"])
    engine = _open(tmp_path, monkeypatch)
    blank = engine.crystal_detail("owner")
    assert blank.get("body_updated_at") == ""
    assert blank.get("sketch_updated_at") == ""
    blank_row = _meta_row(
        crystal_page(
            blank,
            [],
            [{"id": "owner", "name": "记忆主人"}],
            "",
            "active",
        )
    )
    assert "人物" in blank_row and "活跃" in blank_row
    assert "正文更新：<span>—</span>" in blank_row
    assert "速写更新：<span>—</span>" in blank_row
    assert "<time" not in blank_row

    clock["now"] = "2026-03-01T01:02:03+00:00"
    both = engine.save_crystal_markdown("owner", "# 记忆主人\n\n北窗还在改灯塔。", "北窗灯塔还在改。")
    assert both["ok"] is True
    after_both = engine.crystal_detail("owner")
    assert after_both["body_updated_at"] == "2026-03-01T01:02:03+00:00"
    assert after_both["sketch_updated_at"] == "2026-03-01T01:02:03+00:00"

    clock["now"] = "2026-03-02T04:05:06+00:00"
    body_only = engine.save_crystal_markdown("owner", "# 记忆主人\n\n南港还在改灯塔。", "北窗灯塔还在改。")
    assert body_only["ok"] is True
    after_body = engine.crystal_detail("owner")
    assert after_body["body_updated_at"] == "2026-03-02T04:05:06+00:00"
    assert after_body["sketch_updated_at"] == "2026-03-01T01:02:03+00:00"

    clock["now"] = "2026-03-03T07:08:09+00:00"
    sketch_only = engine.save_crystal_markdown("owner", "# 记忆主人\n\n南港还在改灯塔。", "南港灯塔还在改。")
    assert sketch_only["ok"] is True
    detail = engine.crystal_detail("owner")
    assert detail["body_updated_at"] == "2026-03-02T04:05:06+00:00"
    assert detail["sketch_updated_at"] == "2026-03-03T07:08:09+00:00"

    from rogmem.crystal_store import CrystalStore

    clock["now"] = "2026-04-01T00:00:00+00:00"
    crystal = CrystalStore(engine.db()).get("owner")
    crystal.keys["所在地"] = "南港"
    CrystalStore(engine.db()).put(crystal)
    held = engine.crystal_detail("owner")
    assert held["body_updated_at"] == "2026-03-02T04:05:06+00:00"
    assert held["sketch_updated_at"] == "2026-03-03T07:08:09+00:00"

    html = crystal_page(
        held,
        [],
        [{"id": "owner", "name": "记忆主人"}],
        "",
        "active",
    )
    row = _meta_row(html)
    assert row.index("活跃") < row.index("正文更新") < row.index("速写更新")
    assert "正文更新：<time datetime='2026-03-02T04:05:06+00:00'>2026-03-02T04:05:06+00:00</time>" in row
    assert "速写更新：<time datetime='2026-03-03T07:08:09+00:00'>2026-03-03T07:08:09+00:00</time>" in row
    style = " ".join(html.split("</style>", 1)[0].split())
    assert "h1 { font-size: 28px; line-height: 1.4; font-weight: 600;" in style
    assert ".entry-head > .meta .kind { color: var(--text-muted); font-size: 14px;" in style
    assert ".entry-head .freshness { font-size: 12px;" in style
    from starlette.testclient import TestClient
    from rogmem.app import build_app

    response = TestClient(build_app()).get("/admin/crystals/owner")
    assert response.status_code == 200
    assert "正文更新：<time datetime='2026-03-02T04:05:06+00:00'>2026-03-02T04:05:06+00:00</time>" in response.text
    assert "速写更新：<time datetime='2026-03-03T07:08:09+00:00'>2026-03-03T07:08:09+00:00</time>" in response.text
    assert response.text.index("人物") < response.text.index("正文更新") < response.text.index("速写更新")
