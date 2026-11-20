"""管线暂停开关：暂停时不派单，抬起开关从原处继续。"""

from __future__ import annotations

from pathlib import Path

BASE = {"scan_interval": "60", "probe_interval": "60", "reconcile_hours": "3", "sweep_at": "03:00"}


def _engine(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _queue_one(engine, tmp_path: Path) -> None:
    note = tmp_path / "scan" / "note.txt"
    note.parent.mkdir(exist_ok=True)
    note.write_text("2026 年 5 月，我把这件事记在这里，留给以后的自己。", encoding="utf-8")
    engine.ingest_file(note)


def test_pause_switch_round_trips(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    assert engine.pipeline_config()["distill_paused"] == ""
    assert engine.distill_paused() is False

    saved = engine.save_pipeline(dict(BASE, distill_paused=True))
    assert saved["ok"] is True
    assert saved["config"]["distill_paused"] == "1"
    assert engine.distill_paused() is True
    # /healthz 读的是缓存，写入后必须同步刷新。
    assert engine.health_snapshot()["distill_paused"] is True

    saved = engine.save_pipeline(dict(BASE, distill_paused=False))
    assert saved["ok"] is True
    assert saved["config"]["distill_paused"] == ""
    assert engine.distill_paused() is False
    assert engine.health_snapshot()["distill_paused"] is False


def test_checkbox_strings_still_parse(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    assert engine.save_pipeline(dict(BASE, distill_paused="on"))["config"]["distill_paused"] == "1"
    assert engine.save_pipeline(dict(BASE, distill_paused="false"))["config"]["distill_paused"] == ""


def test_paused_takes_no_unit_and_resume_continues(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _queue_one(engine, tmp_path)

    engine.save_pipeline(dict(BASE, distill_paused=True))
    assert engine.lease_next() is None  # 暂停：一个单位都不锁

    engine.save_pipeline(dict(BASE, distill_paused=False))
    leased = engine.lease_next()  # 恢复：从原处继续拿到同一条
    assert leased is not None


def test_pipeline_page_renders_the_switch(tmp_path, monkeypatch):
    _engine(tmp_path, monkeypatch)
    from rogmem.ui import pipeline_page

    on = pipeline_page([], {}, {}, {"distill_paused": "1"})
    off = pipeline_page([], {}, {}, {})
    assert "name='distill_paused' checked" in on
    assert "name='distill_paused'" in off
    assert "name='distill_paused' checked" not in off
