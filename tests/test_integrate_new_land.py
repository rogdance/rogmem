"""identity=new accept 薄建硬卡。

本刀只治「已进 Integrate 后的 new」：accept + identity=new 用 Draft 中稿
薄写新 active 词条。maybe 进 deferred、不改 active；same 仍只改 matched_id。
Match=0 冷启改道见 test_match0_cold_start_fail_gate.py。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from rogmem import engine as distill_engine
from rogmem.crystal_store import DISTILLER_STORE_WRITE_ENABLED, CrystalStore, hydrate
from rogmem.distill_gate import PAYLOAD_KIND_INTEGRATE, stamp_integrate_payload

CORPUS = "云岚测试员甲于 2019 年在北窗实验室任职。后来到南港书院做讲师。"
NEW_DRAFT = {
    "name": "北窗讲师丙",
    "type": "人物",
    "aliases": ["讲师丙"],
    "summary": "在北窗实验室做讲师。",
    "owner_relation": "同事",
    "body_md": "## 事业经历\n\n云岚测试员甲于 2019 年在北窗实验室任职。",
    "facts": [
        {
            "claim": "2019 年在北窗实验室任职",
            "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
        }
    ],
    "org_title": "讲师",
}
NEW_ACCEPT = {"identity": "new", "ops": [], "conflicts": []}
MAYBE_ACCEPT = {"identity": "maybe", "ops": [], "conflicts": []}
SAME_ACCEPT = {
    "identity": "same",
    "matched_id": "owner",
    "ops": [
        {
            "op": "update",
            "path": "body_md",
            "value": CORPUS,
            "fact": {
                "claim": "2019 入职",
                "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
            },
        }
    ],
    "conflicts": [],
}


def _engine(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _active_rows(engine):
    return list(
        engine.db().execute(
            "SELECT id, type, name, status, summary_json, chapters_json, envelope_json, "
            "body_md, facts_json, revision FROM crystallizations WHERE status='active' "
            "ORDER BY id"
        )
    )


def _ids(rows):
    return {row["id"] for row in rows}


def test_identity_new_accept_puts_active_thin_row(tmp_path, monkeypatch):
    """合法 identity=new + accept → 库内出现新 active 薄词条。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    result = engine.apply_integrate(
        NEW_ACCEPT,
        candidates=["owner"],
        corpus=CORPUS,
        store=CrystalStore(engine.db()),
        draft_crystal=NEW_DRAFT,
        holder="integrate-new-qa",
    )
    assert result.decision == "accept"
    assert result.identity == "new"
    after = _active_rows(engine)
    created = [row for row in after if row["id"] not in _ids(before)]
    assert len(created) == 1
    row = created[0]
    assert row["status"] == "active"
    assert row["name"] == "北窗讲师丙"
    assert row["type"] == "人物"
    assert json.loads(row["summary_json"] or "{}") == {}
    assert json.loads(row["chapters_json"] or "{}") == {}
    thin = hydrate(row)
    assert thin.storage == "thin"
    assert thin.summary == "在北窗实验室做讲师。"
    assert "讲师丙" in thin.aliases
    assert thin.owner_relation == "同事"
    assert "事业经历" in (thin.body_md or "")
    assert any(fact.claim == "2019 年在北窗实验室任职" for fact in thin.facts)
    assert thin.keys.get("org_title") == "讲师"
    events = CrystalStore(engine.db()).events(row["id"])
    assert any(item["kind"] == "promote_new" for item in events)


def test_identity_maybe_accept_writes_nothing(tmp_path, monkeypatch):
    """identity=maybe → deferred，不改任何 active，即便带了 crystal_id。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    owner_before = engine.db().execute(
        "SELECT revision, unresolved_conflicts, envelope_json, body_md "
        "FROM crystallizations WHERE id='owner'"
    ).fetchone()
    result = engine.apply_integrate(
        MAYBE_ACCEPT,
        candidates=["owner"],
        corpus=CORPUS,
        store=CrystalStore(engine.db()),
        crystal_id="owner",
        draft_crystal=NEW_DRAFT,
        holder="integrate-maybe-qa",
    )
    assert result.decision == "deferred"
    assert result.identity == "maybe"
    after = _active_rows(engine)
    assert _ids(after) == _ids(before)
    owner_after = engine.db().execute(
        "SELECT revision, unresolved_conflicts, envelope_json, body_md "
        "FROM crystallizations WHERE id='owner'"
    ).fetchone()
    assert owner_after["revision"] == owner_before["revision"]
    assert owner_after["unresolved_conflicts"] == owner_before["unresolved_conflicts"]
    assert owner_after["envelope_json"] == owner_before["envelope_json"]
    assert owner_after["body_md"] == owner_before["body_md"]
    deferred = list(
        engine.db().execute(
            "SELECT id, status FROM crystallizations WHERE status='deferred' AND name=?",
            (NEW_DRAFT["name"],),
        )
    )
    assert len(deferred) == 1
    assert deferred[0]["status"] == "deferred"


def test_identity_same_still_only_touches_matched(tmp_path, monkeypatch):
    """identity=same → 仍只走 matched_id 改旧条，不新建。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    owner_before = engine.db().execute(
        "SELECT revision, body_md FROM crystallizations WHERE id='owner'"
    ).fetchone()
    result = engine.apply_integrate(
        SAME_ACCEPT,
        candidates=["owner"],
        corpus=CORPUS,
        store=CrystalStore(engine.db()),
        crystal_id="owner",
        draft_crystal=NEW_DRAFT,
        holder="integrate-same-qa",
    )
    assert result.decision == "accept"
    assert result.identity == "same"
    after = _active_rows(engine)
    assert _ids(after) == _ids(before)
    owner_after = engine.db().execute(
        "SELECT revision, body_md FROM crystallizations WHERE id='owner'"
    ).fetchone()
    assert int(owner_after["revision"] or 0) > int(owner_before["revision"] or 0)
    assert CORPUS in (owner_after["body_md"] or "")
    assert all(row["name"] != "北窗讲师丙" for row in after)


def test_illegal_new_with_ops_still_rejected_no_write(tmp_path, monkeypatch):
    """带 ops 的 identity=new 仍是非法闸门，不得当落地绿。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    result = engine.apply_integrate(
        {"identity": "new", "ops": [{"op": "add", "path": "facts"}], "conflicts": []},
        candidates=["owner"],
        corpus=CORPUS,
        store=CrystalStore(engine.db()),
        draft_crystal=NEW_DRAFT,
    )
    assert result.decision == "rejected"
    assert any(item.code == "ops_conflicts_must_be_empty" for item in result.issues)
    assert _ids(_active_rows(engine)) == _ids(before)


def test_produce_route_carries_draft_sidecar_to_new_land(tmp_path, monkeypatch):
    """produce→route 把 Draft 中稿带到落库缝，new 路径能读到并薄建。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    produce_src = inspect.getsource(engine._produce_distill_payload)
    enter_src = inspect.getsource(engine._enter_integrate)
    apply_src = inspect.getsource(engine.apply_integrate)
    assert "draft_crystal" in produce_src
    assert "draft_crystal" in enter_src
    assert "result.identity" in apply_src
    assert "_open_crystal(" not in apply_src
    assert apply_src.count("_apply_then_sidecar(") == 1

    def fake_integrate(_text, _kind="", **_kw):
        return dict(NEW_ACCEPT)

    monkeypatch.setattr(engine, "_llm_integrate", fake_integrate)
    data = engine._produce_distill_payload(
        CORPUS,
        "user",
        draft_crystal=NEW_DRAFT,
        library_candidates=[{"id": "owner", "name": "记忆主人", "type": "人物"}],
    )
    assert data["payload_kind"] == PAYLOAD_KIND_INTEGRATE
    assert data.get("_draft_crystal") == NEW_DRAFT
    assert data["identity"] == "new"

    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?, 'user', 'qa', 'qa', ?, ?, ?, ?, 'fast', 1)""",
        ("src-new-land", engine.utcnow(), "src-new-land-h", CORPUS, engine.ST_VEC_READY),
    )
    con.commit()
    source = con.execute("SELECT * FROM sources WHERE id='src-new-land'").fetchone()
    before = _active_rows(engine)
    engine._route_distill_payload(source, data, CORPUS)
    created = [row for row in _active_rows(engine) if row["id"] not in _ids(before)]
    assert len(created) == 1
    assert created[0]["name"] == "北窗讲师丙"
    assert created[0]["status"] == "active"
    assert json.loads(created[0]["summary_json"] or "{}") == {}
    assert json.loads(created[0]["chapters_json"] or "{}") == {}
    thin = hydrate(created[0])
    assert thin.summary == "在北窗实验室做讲师。"


def test_enter_integrate_forwards_draft_sidecar(tmp_path, monkeypatch):
    """_enter_integrate 把 sidecar 中稿交给 apply_integrate，不嗅 identity 分流。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    enter_src = inspect.getsource(engine._enter_integrate)
    route_src = inspect.getsource(engine._route_distill_payload)
    assert "identity" not in inspect.getsource(engine._declared_payload_kind)
    assert "result.identity" not in route_src
    assert 'identity ==' not in route_src
    assert '["identity"]' not in route_src
    captured: dict = {}
    orig = engine.apply_integrate

    def spy(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return orig(*args, **kwargs)

    monkeypatch.setattr(engine, "apply_integrate", spy)
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?, 'user', 'qa', 'qa', ?, ?, ?, ?, 'fast', 1)""",
        ("src-enter-draft", engine.utcnow(), "src-enter-draft-h", CORPUS, engine.ST_VEC_READY),
    )
    con.commit()
    source = con.execute("SELECT * FROM sources WHERE id='src-enter-draft'").fetchone()
    payload = stamp_integrate_payload(dict(NEW_ACCEPT))
    payload["_draft_crystal"] = NEW_DRAFT
    engine._enter_integrate(source, payload, CORPUS)
    assert captured["kwargs"].get("draft_crystal") == NEW_DRAFT
    assert "identity" not in enter_src


def test_docs_prompts_zero_diff_for_this_knife():
    """本刀不得改 Draft / Integrate prompt 文案。"""
    from tests.docs_prompts_zero_diff import assert_docs_prompts_zero_diff

    assert DISTILLER_STORE_WRITE_ENABLED is True
    assert_docs_prompts_zero_diff()
    engine_src = Path(distill_engine.__file__).read_text(encoding="utf-8")
    assert "_open_crystal(" in engine_src
    apply_src = inspect.getsource(distill_engine.apply_integrate)
    assert "_open_crystal(" not in apply_src


def _queue_user(engine, source_id: str) -> None:
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?, 'user', 'qa', 'qa', ?, ?, ?, ?, 'fast', 1)""",
        (source_id, engine.utcnow(), source_id + "-h", CORPUS, engine.ST_VEC_READY),
    )
    con.execute("INSERT INTO queue(source_id,lane,state) VALUES(?, 'fast', 'queued')", (source_id,))
    con.commit()


def _queue_row(engine, source_id: str):
    return engine.db().execute(
        "SELECT state, last_error FROM queue WHERE source_id=?", (source_id,)
    ).fetchone()


def _assert_new_land_unwritten(result, engine, before):
    assert result.decision != "accept"
    assert result.decision in {"rejected", "deferred"}
    assert _ids(_active_rows(engine)) == _ids(before)
    assert all(row["name"] != "北窗讲师丙" for row in _active_rows(engine))


def test_new_land_empty_draft_not_accept_no_write(tmp_path, monkeypatch):
    """空稿不得伪装 accept，也不落库。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    result = engine.apply_integrate(
        NEW_ACCEPT,
        candidates=["owner"],
        corpus=CORPUS,
        store=CrystalStore(engine.db()),
        draft_crystal={},
        holder="integrate-new-empty",
    )
    _assert_new_land_unwritten(result, engine, before)
    assert any(item.code == "new_land_missing_draft" for item in result.issues)


def test_new_land_missing_sidecar_not_accept_no_write(tmp_path, monkeypatch):
    """缺 sidecar / 无名中稿不得伪装 accept。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    missing = engine.apply_integrate(
        NEW_ACCEPT,
        candidates=["owner"],
        corpus=CORPUS,
        store=CrystalStore(engine.db()),
        holder="integrate-new-noside",
    )
    _assert_new_land_unwritten(missing, engine, before)
    nameless = engine.apply_integrate(
        NEW_ACCEPT,
        candidates=["owner"],
        corpus=CORPUS,
        store=CrystalStore(engine.db()),
        draft_crystal={"type": "人物", "facts": NEW_DRAFT["facts"]},
        holder="integrate-new-noname",
    )
    _assert_new_land_unwritten(nameless, engine, before)
    assert any(item.code == "new_land_missing_draft" for item in missing.issues)
    assert any(item.code == "new_land_missing_draft" for item in nameless.issues)


def test_new_land_write_switch_off_not_accept_no_write(tmp_path, monkeypatch, write_switch_off):
    """写开关关：new 不得假 accept，与 fail-closed 对齐。"""
    engine = _engine(tmp_path, monkeypatch)
    before = _active_rows(engine)
    result = engine.apply_integrate(
        NEW_ACCEPT,
        candidates=["owner"],
        corpus=CORPUS,
        store=CrystalStore(engine.db()),
        draft_crystal=NEW_DRAFT,
        holder="integrate-new-switch-off",
    )
    _assert_new_land_unwritten(result, engine, before)
    assert result.decision == "deferred"
    assert any(item.code == "new_land_write_switch_off" for item in result.issues)


def test_new_land_missing_sidecar_pump_not_fake_done(tmp_path, monkeypatch):
    """缺中稿进泵：非 accept、不落库、队列不假 done。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    monkeypatch.setattr(
        engine,
        "_with_approved_draft",
        lambda *_a, **_k: {"library_candidates": [{"id": "owner", "name": "记忆主人"}]},
    )
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: dict(NEW_ACCEPT))
    before = _active_rows(engine)
    _queue_user(engine, "src-new-noside")
    assert engine.pump_one() == "src-new-noside"
    queued = _queue_row(engine, "src-new-noside")
    assert queued["state"] != "done"
    assert queued["last_error"]
    assert _ids(_active_rows(engine)) == _ids(before)


def test_new_land_write_switch_off_pump_not_fake_done(tmp_path, monkeypatch, write_switch_off):
    """写开关关进泵：new 不落库，队列不假 done。"""
    engine = _engine(tmp_path, monkeypatch)
    monkeypatch.setattr(
        engine,
        "_with_approved_draft",
        lambda *_a, **_k: {
            "draft_crystal": NEW_DRAFT,
            "library_candidates": [{"id": "owner", "name": "记忆主人"}],
        },
    )
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: dict(NEW_ACCEPT))
    before = _active_rows(engine)
    _queue_user(engine, "src-new-off")
    assert engine.pump_one() == "src-new-off"
    queued = _queue_row(engine, "src-new-off")
    assert queued["state"] != "done"
    assert queued["last_error"]
    assert _ids(_active_rows(engine)) == _ids(before)
    assert all(row["name"] != "北窗讲师丙" for row in _active_rows(engine))
