"""W-58-01: _write_chapter routes through apply_ops. Production write switch is on."""

from __future__ import annotations

import json
from pathlib import Path

from rogmem.crystal_store import DISTILLER_STORE_WRITE_ENABLED, chapter_update_ops


class FakeCrystalStore:
    def __init__(self):
        self.calls: list[dict] = []

    def apply_ops(self, crystal_id, expected_revision, ops, *, holder, commit=True):
        self.calls.append(
            {
                "crystal_id": crystal_id,
                "expected_revision": expected_revision,
                "ops": list(ops),
                "holder": holder,
            }
        )
        return {"crystal_id": crystal_id, "revision": int(expected_revision) + 1, "ops": list(ops)}


def _engine(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _source(engine, body: str, source_id: str = "src-wire"):
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready)
           VALUES(?,'user','本机','极空间',?,?,?,?,'已解析','p0',1)""",
        (source_id, now, f"file:{source_id}", f"hash-{source_id}", body),
    )
    engine.db().commit()
    return engine.db().execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()


def test_w_58_01_write_chapter_calls_apply_ops_without_flipping_switch(tmp_path, monkeypatch):
    """W-58-01: fake store receives body_md## chapter ops; injected store does not persist."""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "llm_target", lambda _role: None)
    body = "2019 年在南港书院做讲师。"
    source = _source(engine, body)
    before = engine.db().execute(
        "SELECT chapters_json, envelope_json, body_md, revision FROM crystallizations WHERE id='owner'"
    ).fetchone()
    fake = FakeCrystalStore()

    engine._write_chapter(
        source,
        {"chapter": "事业经历", "value": body, "quote": body, "crystal": "记忆主人"},
        body,
        store=fake,
    )

    assert DISTILLER_STORE_WRITE_ENABLED is True
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["crystal_id"] == "owner"
    assert call["ops"] == chapter_update_ops("事业经历", body)
    assert call["ops"] == [{"op": "update", "path": "body_md##事业经历", "value": body}]
    assert call["holder"]
    after = engine.db().execute(
        "SELECT chapters_json, envelope_json, body_md, revision FROM crystallizations WHERE id='owner'"
    ).fetchone()
    assert after["chapters_json"] == before["chapters_json"]
    assert after["envelope_json"] == before["envelope_json"]
    assert after["body_md"] == before["body_md"]
    assert after["revision"] == before["revision"]
    assert "南港书院" not in (after["chapters_json"] or "")


def test_write_chapter_apply_ops_shape_uses_computed_prose(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "2019 年在南港书院做讲师。"
    source = _source(engine, body, "src-prose-wire")
    fake = FakeCrystalStore()
    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m") if role == "prose" else None)
    monkeypatch.setattr(engine, "_complete_chat", lambda *_args: ("他于 2019 年在南港书院做讲师。", "stop"))

    engine._write_chapter(
        source,
        {"chapter": "事业经历", "value": body, "quote": body, "crystal": "记忆主人"},
        body,
        store=fake,
    )

    assert fake.calls[0]["ops"] == chapter_update_ops("事业经历", "他于 2019 年在南港书院做讲师。")


def test_write_chapter_refuses_prose_that_drops_quote(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "2019 年在南港书院做讲师。"
    source = _source(engine, body, "src-prose-drop")
    fake = FakeCrystalStore()
    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m") if role == "prose" else None)
    monkeypatch.setattr(engine, "_complete_chat", lambda *_args: ("改写后丢掉了原文。", "stop"))

    engine._write_chapter(
        source,
        {"chapter": "事业经历", "value": body, "quote": body, "crystal": "记忆主人"},
        body,
        store=fake,
    )

    assert fake.calls == []
    chapters = engine.db().execute("SELECT chapters_json FROM crystallizations WHERE id='owner'").fetchone()
    assert "南港书院" not in chapters["chapters_json"]


def test_write_chapter_thin_row_does_not_fall_back_to_chapters_json(tmp_path, monkeypatch):
    """thin 禁厚写：行已是瘦信封时，真写落 body_md，不得再直写 chapters_json。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "llm_target", lambda _role: None)
    body = "后来到南港书院做讲师。"
    source = _source(engine, body, "src-thin-no-bypass")
    engine.db().execute(
        "UPDATE crystallizations SET envelope_json=?, body_md=?, chapters_json=? WHERE id='owner'",
        (
            json.dumps({"summary": "瘦信封", "aliases": [], "owner_relation": "本人", "facts": [], "inferences": [], "keys": {}}, ensure_ascii=False),
            "## 事业经历\n\n旧瘦正文。",
            json.dumps({"事业经历": "这段厚正文必须保持不动。"}, ensure_ascii=False),
        ),
    )
    engine.db().commit()

    engine._write_chapter(
        source,
        {"chapter": "事业经历", "value": body, "quote": body, "crystal": "记忆主人"},
        body,
    )

    row = engine.db().execute(
        "SELECT chapters_json, envelope_json, body_md FROM crystallizations WHERE id='owner'"
    ).fetchone()
    assert json.loads(row["chapters_json"])["事业经历"] == "这段厚正文必须保持不动。"
    assert "南港书院" not in (row["chapters_json"] or "")
    assert "南港书院" in (row["body_md"] or "")
    assert engine.crystal_detail("owner")["chapters"]["事业经历"]
    assert "南港书院" in (engine.crystal_detail("owner")["chapters"]["事业经历"] or "")
