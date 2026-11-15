"""翻写开关：overview / 再裁决 / 别名 / 投影 / 开篇 / 章节清扫 瘦路径真落薄。

QA 过滤：``pytest -m ws_flip``。生产默认 ``DISTILLER_STORE_WRITE_ENABLED is True``。
关开关仍 fail-closed。禁止 pytest.skip / xfail 占位。
盘点仍可能有厚读 / 迁移残留，勿称全库零厚写。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from rogmem import ingress
from rogmem.crystal_store import (
    DISTILLER_STORE_WRITE_ENABLED,
    CrystalStore,
    LeaseConflict,
    field_update_ops,
)
from rogmem import engine as distill_engine

pytestmark = pytest.mark.ws_flip

CORPUS = "云岚测试员甲现住南港书院东侧。"


class FakeCrystalStore:
    def __init__(self):
        self.calls: list[dict] = []
        self.puts: list[dict] = []

    def apply_ops(self, crystal_id, expected_revision, ops, *, holder, commit=True):
        self.calls.append(
            {
                "crystal_id": crystal_id,
                "expected_revision": expected_revision,
                "ops": list(ops),
                "holder": holder,
                "commit": commit,
            }
        )
        return {"crystal_id": crystal_id, "revision": int(expected_revision) + 1, "ops": list(ops)}

    def put(self, crystal, *, holder=None, commit=True):
        self.puts.append({"crystal": crystal, "holder": holder, "commit": commit})
        return crystal


class EnvelopeSpy:
    """Watch material envelope → ingress → distill write boundary."""

    def __init__(self):
        self.accepted: list[dict] = []
        self.apply_ops: list[dict] = []
        self.thick_sql: list[str] = []

    def post(self, turns):
        raise AssertionError("ingress must not post into distill during spy dry-run")


def _engine(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _source(engine, body: str, source_id: str = "src-ws-flip"):
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready)
           VALUES(?,'user','本机','极空间',?,?,?,?,'已解析','p0',1)""",
        (source_id, now, f"file:{source_id}", f"hash-{source_id}", body),
    )
    engine.db().commit()
    return engine.db().execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()


def _snapshot(engine, crystal_id: str = "owner"):
    return engine.db().execute(
        """SELECT summary_json, chapters_json, envelope_json, body_md, revision, lease_holder, lease_until
           FROM crystallizations WHERE id=?""",
        (crystal_id,),
    ).fetchone()


def _material() -> dict:
    return ingress.envelope(
        kind="turn",
        body=CORPUS,
        event_at="2026-10-30T08:00:00+00:00",
        role="user",
        session_id="sess-ws-flip",
        turn_id="turn-ws-flip",
        seq=1,
        program="Hermes",
        device_name="云岚本机",
    )


def test_ws_flip_write_switch_is_on():
    assert DISTILLER_STORE_WRITE_ENABLED is True
    assert DISTILLER_STORE_WRITE_ENABLED is distill_engine.DISTILLER_STORE_WRITE_ENABLED


def test_ws_flip_overview_ops_are_thin_summary_path():
    """overview persist 必须走 field_update_ops('概述') → path summary，禁止厚 summary_json 回写。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    ops = field_update_ops("概述", "这是人物「记忆主人」。所在地是南港。")
    assert ops == [{"op": "update", "path": "summary", "value": "这是人物「记忆主人」。所在地是南港。"}]
    persist = inspect.getsource(distill_engine._persist_overview)
    rebuild = inspect.getsource(distill_engine.rebuild_overviews)
    sweep = inspect.getsource(distill_engine.sweep)
    put = inspect.getsource(distill_engine._put_overview)
    open_src = inspect.getsource(distill_engine._open_crystal)
    rejudge = inspect.getsource(distill_engine._apply_rejudge_update)
    fold = inspect.getsource(distill_engine._fold_spelling)
    projection = inspect.getsource(distill_engine._rebuild_projection)
    assert "field_update_ops(" in persist
    assert '"概述"' in persist
    assert "summary_json=?" not in persist
    assert "summary_json=?" not in rebuild
    assert "summary_json=?" not in sweep
    assert "_persist_overview(" in rebuild
    assert "_persist_overview(" in sweep
    assert "summary_json=?" not in put
    assert "_put_overview(" not in open_src
    assert "_put_overview(" not in rejudge
    assert "field_update_ops(" in rejudge
    assert "summary_json=?" not in rejudge
    assert "field_update_ops(" in fold
    assert '"别名"' in fold
    assert "summary_json=?" not in fold
    assert "_apply_then_sidecar(" in rejudge
    assert "_apply_then_sidecar(" in fold
    assert 'if key == "概述"' in projection
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_overview_switch_off_no_thick_or_thin_write(tmp_path, monkeypatch, write_switch_off):
    """关开关：sweep / rebuild / persist 不得写 envelope，也不得改厚 概述。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,event_at,created_at)
           VALUES('st-ov','owner','所在地','南港',?,?, 'written','user',?,?)""",
        (CORPUS, "src-ov", engine.utcnow(), engine.utcnow()),
    )
    engine.db().execute(
        "UPDATE crystallizations SET summary_json=? WHERE id='owner'",
        (json.dumps({"概述": "旧厚概述必须保持不动。", "所在地": "尚无来源"}, ensure_ascii=False),),
    )
    engine.db().commit()
    before = _snapshot(engine)
    engine.rebuild_overviews()
    swept = engine.sweep()
    fake = FakeCrystalStore()
    changed = engine._persist_overview(
        engine.db(),
        "owner",
        "人物",
        "记忆主人",
        store=fake,
        holder="sweep:overview",
    )
    after = _snapshot(engine)
    assert swept["overviews"] == 0
    assert json.loads(after["summary_json"])["概述"] == "旧厚概述必须保持不动。"
    assert after["envelope_json"] == before["envelope_json"]
    assert after["revision"] == before["revision"]
    assert changed is False
    assert fake.calls
    assert fake.calls[0]["ops"] == field_update_ops(
        "概述",
        engine._overview_text(engine.db(), "owner", "人物", "记忆主人"),
    )
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_overview_unlock_writes_thin_summary(tmp_path, monkeypatch):
    """开关已开：概述落入 envelope.summary，厚 概述 不动。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,event_at,created_at)
           VALUES('st-ov2','owner','所在地','南港',?,?, 'written','user',?,?)""",
        (CORPUS, "src-ov2", engine.utcnow(), engine.utcnow()),
    )
    engine.db().execute(
        "UPDATE crystallizations SET summary_json=? WHERE id='owner'",
        (json.dumps({"概述": "旧厚概述必须保持不动。", "所在地": "尚无来源"}, ensure_ascii=False),),
    )
    engine.db().commit()
    changed = engine._persist_overview(
        engine.db(),
        "owner",
        "人物",
        "记忆主人",
        holder="rebuild:overview",
    )
    assert changed is True
    row = _snapshot(engine)
    envelope = json.loads(row["envelope_json"])
    assert "南港" in envelope["summary"]
    assert json.loads(row["summary_json"])["概述"] == "旧厚概述必须保持不动。"
    detail = engine.crystal_detail("owner")
    assert "南港" in (detail["summary"]["概述"] or "")
    section = engine.read_section("记忆主人", "概述")
    assert section["missing"] is False
    assert "南港" in (section["text"] or "")
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_lease_helper_is_on_shared_apply_path():
    """_write_field / _write_chapter / apply_integrate 共用 helper 上的 crystal lease。"""
    helper = inspect.getsource(distill_engine._apply_then_sidecar)
    field_src = inspect.getsource(distill_engine._write_field)
    chapter_src = inspect.getsource(distill_engine._write_chapter)
    apply_src = inspect.getsource(distill_engine.apply_integrate)
    orphan = inspect.getsource(distill_engine.release_orphaned_leases)
    assert "_store_acquire_lease(" in helper
    assert "_store_release_lease(" in helper
    assert "purpose=\"distill\"" in inspect.getsource(distill_engine._store_acquire_lease)
    assert "release_orphaned(" in orphan
    assert "_apply_then_sidecar(" in field_src
    assert "_apply_then_sidecar(" in chapter_src
    assert "_apply_then_sidecar(" in apply_src
    assert "acquire_lease" not in field_src
    assert "acquire_lease" not in chapter_src
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_lease_auto_acquire_on_unlock(tmp_path, monkeypatch):
    """开关已开：_write_field 自己领 lease，不必测试里手工 acquire。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    source = _source(engine, CORPUS, "src-auto-lease")
    engine._write_field(
        source,
        {"field": "所在地", "value": "南港", "quote": CORPUS, "crystal": "记忆主人", "trust": "written"},
        "user",
        CORPUS,
    )
    row = _snapshot(engine)
    assert row["envelope_json"] and "南港" in row["envelope_json"]
    assert row["lease_holder"] is None
    held = engine.db().execute("SELECT 1 FROM crystal_leases WHERE crystal_id='owner'").fetchone()
    assert held is None
    stmt = engine.db().execute(
        "SELECT text FROM statements WHERE crystal_id='owner' AND quote=?",
        (CORPUS,),
    ).fetchone()
    assert stmt is not None and stmt["text"] == "南港"
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_lease_second_holder_cannot_write(tmp_path, monkeypatch):
    """另一 holder 占着 crystal lease 时，_write_field 不得落库。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    source = _source(engine, CORPUS, "src-lease-conflict")
    store = CrystalStore(engine.db())
    store.acquire_lease("owner", "distill:other-worker")
    before = _snapshot(engine)
    with pytest.raises(LeaseConflict):
        engine._write_field(
            source,
            {"field": "所在地", "value": "南港", "quote": CORPUS, "crystal": "记忆主人", "trust": "written"},
            "user",
            CORPUS,
        )
    after = _snapshot(engine)
    assert after["envelope_json"] == before["envelope_json"]
    assert after["revision"] == before["revision"]
    assert "南港" not in (after["envelope_json"] or "")
    assert engine.db().execute(
        "SELECT 1 FROM statements WHERE crystal_id='owner' AND quote=?",
        (CORPUS,),
    ).fetchone() is None
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_lease_orphan_release_on_connect(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    store = CrystalStore(engine.db())
    engine.db().execute(
        "INSERT INTO crystal_leases(crystal_id, holder, until, purpose) VALUES(?,?,?,?)",
        ("owner", "stale-worker", "2099-01-01T00:00:00+00:00", "distill"),
    )
    engine.db().execute(
        "UPDATE crystallizations SET lease_holder=?, lease_until=? WHERE id='owner'",
        ("stale-worker", "2099-01-01T00:00:00+00:00"),
    )
    engine.db().commit()
    assert store._has_lease("owner")
    n = engine.release_orphaned_leases()
    assert n == 0
    assert store._has_lease("owner") is False
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_envelope_spy_ingress_does_not_write_crystal(tmp_path, monkeypatch):
    """材料信封经 ingress.accept：dry-run 不蒸馏、不写 crystal，间谍能看到信封。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    spy = EnvelopeSpy()
    env = _material()
    accepted = ingress.accept(env, dry_run=True, post=spy.post)
    spy.accepted.extend(accepted["accepted"])
    before = _snapshot(engine)
    assert accepted["ok"] is True
    assert accepted["distilled"] is False
    assert accepted["crystal_written"] is False
    assert accepted["posted"] == 0
    assert spy.accepted[0]["schema"] == ingress.SCHEMA
    assert spy.accepted[0]["body"] == CORPUS
    assert spy.accepted[0]["content_hash"]
    after = _snapshot(engine)
    assert after["summary_json"] == before["summary_json"]
    assert after["chapters_json"] == before["chapters_json"]
    assert after["envelope_json"] == before["envelope_json"]
    assert after["revision"] == before["revision"]
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_envelope_spy_write_boundary_switch_off(tmp_path, monkeypatch, write_switch_off):
    """材料信封 → ingest → _write_field：关开关时 apply 被间谍看到，库不落厚/瘦写。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    env = _material()
    checked = ingress.accept(env, dry_run=True)
    assert checked["crystal_written"] is False
    turns = [ingress.turn_from_envelope(item) for item in checked["accepted"]]
    ingested = engine.ingest_history(turns, program="信封间谍")
    assert ingested["ok"] is True
    source_id = ingested["imported"][0]["id"]
    source = engine.db().execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    assert source["body"] == CORPUS
    before = _snapshot(engine)
    fake = FakeCrystalStore()
    engine._write_field(
        source,
        {"field": "所在地", "value": "南港", "quote": CORPUS, "crystal": "记忆主人", "trust": "written"},
        "user",
        CORPUS,
        store=fake,
    )
    after = _snapshot(engine)
    assert fake.calls
    assert fake.calls[0]["holder"] == f"distill:{source_id}"
    assert fake.calls[0]["ops"][0]["path"] == "keys.所在地"
    assert after["summary_json"] == before["summary_json"]
    assert after["chapters_json"] == before["chapters_json"]
    assert after["envelope_json"] == before["envelope_json"]
    assert after["revision"] == before["revision"]
    assert "南港" not in (after["summary_json"] or "")
    assert engine.db().execute(
        "SELECT 1 FROM statements WHERE crystal_id='owner' AND quote=?",
        (CORPUS,),
    ).fetchone() is None
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_envelope_spy_real_store_switch_off_no_persist(tmp_path, monkeypatch, write_switch_off):
    """真 store：关开关时材料正文进 _write_field / apply_integrate，不落 envelope / 厚 JSON。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    env = _material()
    source = _source(engine, env["body"], "src-spy-real")
    before = _snapshot(engine)
    engine._write_field(
        source,
        {"field": "所在地", "value": "南港", "quote": env["body"], "crystal": "记忆主人", "trust": "written"},
        "user",
        env["body"],
    )
    engine.apply_integrate(
        {
            "identity": "same",
            "matched_id": "owner",
            "ops": [{"op": "update", "path": "summary", "value": "不该写入的概述"}],
            "conflicts": [],
        },
        candidates=["owner"],
        corpus=env["body"],
        crystal_id="owner",
        holder=f"distill:{source['id']}",
    )
    after = _snapshot(engine)
    assert after["summary_json"] == before["summary_json"]
    assert after["chapters_json"] == before["chapters_json"]
    assert after["envelope_json"] == before["envelope_json"]
    assert after["body_md"] == before["body_md"]
    assert after["revision"] == before["revision"]
    assert "不该写入的概述" not in (after["envelope_json"] or "")
    assert "南港" not in (after["summary_json"] or "")
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_rejudge_ops_are_thin_field_path():
    """再裁决必须走 field_update_ops → apply_ops，禁止厚 summary_json 回写。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    rejudge = inspect.getsource(distill_engine._apply_rejudge_update)
    assert "field_update_ops(" in rejudge
    assert "_apply_then_sidecar(" in rejudge
    assert "summary_json=?" not in rejudge
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_rejudge_switch_off_no_thick_or_thin_write(tmp_path, monkeypatch, write_switch_off):
    """关开关：再裁决不得写 envelope，也不得改厚栏。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    now = engine.utcnow()
    engine.db().execute(
        "UPDATE crystallizations SET summary_json=? WHERE id='owner'",
        (json.dumps({"所在地": "旧港", "概述": "旧厚概述必须保持不动。"}, ensure_ascii=False),),
    )
    engine.db().execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,event_at,created_at)
           VALUES('st-old-loc','owner','所在地','旧港','旧港','src-rejudge', 'written','summary',?,?)""",
        (now, now),
    )
    source = _source(engine, "现住南港。", "src-rejudge")
    before = _snapshot(engine)
    fake = FakeCrystalStore()
    engine._apply_rejudge_update(
        engine.db(),
        "所在地",
        "旧港",
        "南港",
        "南港",
        source,
        store=fake,
        commit=False,
    )
    after = _snapshot(engine)
    assert fake.calls
    assert fake.calls[0]["ops"] == field_update_ops("所在地", "南港")
    assert fake.calls[0]["holder"] == "distill:src-rejudge"
    assert json.loads(after["summary_json"])["所在地"] == "旧港"
    assert after["envelope_json"] == before["envelope_json"]
    assert after["revision"] == before["revision"]
    assert "南港" not in (after["summary_json"] or "")
    assert engine.db().execute(
        "SELECT text FROM statements WHERE crystal_id='owner' AND field='所在地' AND invalid_at IS NULL"
    ).fetchone()["text"] == "旧港"
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_rejudge_unlock_writes_thin_field(tmp_path, monkeypatch):
    """开关已开：再裁决落入 envelope.keys，厚栏不动。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    now = engine.utcnow()
    engine.db().execute(
        "UPDATE crystallizations SET summary_json=? WHERE id='owner'",
        (json.dumps({"所在地": "旧港", "概述": "旧厚概述必须保持不动。"}, ensure_ascii=False),),
    )
    engine.db().execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,event_at,created_at)
           VALUES('st-old-loc2','owner','所在地','旧港','旧港','src-rejudge2', 'written','summary',?,?)""",
        (now, now),
    )
    engine.db().commit()
    source = _source(engine, "现住南港。", "src-rejudge2")
    engine._apply_rejudge_update(
        engine.db(),
        "所在地",
        "旧港",
        "南港",
        "南港",
        source,
    )
    row = _snapshot(engine)
    envelope = json.loads(row["envelope_json"])
    assert envelope["keys"]["所在地"] == "南港"
    assert json.loads(row["summary_json"])["所在地"] == "旧港"
    assert json.loads(row["summary_json"])["概述"] == "旧厚概述必须保持不动。"
    live = engine.db().execute(
        """SELECT text FROM statements
           WHERE crystal_id='owner' AND field='所在地' AND invalid_at IS NULL AND trust='written'
           ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()
    assert live["text"] == "南港"
    old = engine.db().execute("SELECT invalid_at FROM statements WHERE id='st-old-loc2'").fetchone()
    assert old["invalid_at"]
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_alias_ops_are_thin_aliases_path():
    """并入写法必须走 field_update_ops('别名') → path aliases，禁止厚 summary_json 回写。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    ops = field_update_ops("别名", "章三")
    assert ops == [{"op": "update", "path": "aliases", "value": "章三"}]
    fold = inspect.getsource(distill_engine._fold_spelling)
    assert "field_update_ops(" in fold
    assert '"别名"' in fold
    assert "summary_json=?" not in fold
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_alias_switch_off_no_thick_or_thin_write(tmp_path, monkeypatch, write_switch_off):
    """关开关：并入写法不得写 envelope，也不得改厚 别名。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,revision)
           VALUES('c-fold','人物','张三','active',?,?,0)""",
        (json.dumps({"别名": "尚无来源", "名称": "张三"}, ensure_ascii=False), "{}"),
    )
    engine.db().commit()
    source = _source(engine, "章三来了。", "src-fold")
    before = engine.db().execute(
        """SELECT summary_json, chapters_json, envelope_json, body_md, revision
           FROM crystallizations WHERE id='c-fold'"""
    ).fetchone()
    fake = FakeCrystalStore()
    folded = engine._fold_spelling(engine.db(), "章三", "人物", "", "章三来了。", source, store=fake)
    after = engine.db().execute(
        """SELECT summary_json, chapters_json, envelope_json, body_md, revision
           FROM crystallizations WHERE id='c-fold'"""
    ).fetchone()
    assert folded == "folded"
    assert fake.calls
    assert fake.calls[0]["ops"] == field_update_ops("别名", "章三")
    assert fake.calls[0]["holder"] == "distill:src-fold"
    assert json.loads(after["summary_json"])["别名"] == "尚无来源"
    assert after["envelope_json"] == before["envelope_json"]
    assert after["revision"] == before["revision"]
    assert engine.db().execute(
        "SELECT 1 FROM statements WHERE crystal_id='c-fold' AND field='别名'"
    ).fetchone() is None
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_alias_unlock_writes_thin_aliases(tmp_path, monkeypatch):
    """开关已开：别名落入 envelope.aliases，厚 别名 不动。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,revision)
           VALUES('c-fold2','人物','张三','active',?,?,0)""",
        (json.dumps({"别名": "尚无来源", "名称": "张三"}, ensure_ascii=False), "{}"),
    )
    engine.db().commit()
    source = _source(engine, "章三来了。", "src-fold2")
    folded = engine._fold_spelling(engine.db(), "章三", "人物", "", "章三来了。", source)
    engine.db().commit()
    assert folded == "folded"
    row = engine.db().execute(
        """SELECT summary_json, envelope_json, revision FROM crystallizations WHERE id='c-fold2'"""
    ).fetchone()
    envelope = json.loads(row["envelope_json"])
    assert envelope["aliases"] == ["章三"]
    assert json.loads(row["summary_json"])["别名"] == "尚无来源"
    detail = engine.crystal_detail("c-fold2")
    assert detail["summary"]["别名"] == "章三"
    labels = engine._crystal_labels(engine.db().execute("SELECT * FROM crystallizations WHERE id='c-fold2'").fetchone())
    assert "章三" in labels
    stmt = engine.db().execute(
        "SELECT text FROM statements WHERE crystal_id='c-fold2' AND field='别名' AND invalid_at IS NULL"
    ).fetchone()
    assert stmt is not None and stmt["text"] == "章三"
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_remaining_thick_inventory():
    """写路径已瘦。盘点仍可能有厚读 / 迁移残留，不宣称全库零厚写。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    rebuild = inspect.getsource(distill_engine._rebuild_projection)
    open_src = inspect.getsource(distill_engine._open_crystal)
    owner = inspect.getsource(distill_engine._ensure_owner)
    create = inspect.getsource(distill_engine._insert_crystal_page)
    strike = inspect.getsource(distill_engine._strike_features)
    move = inspect.getsource(distill_engine._move_started_features)
    merge = inspect.getsource(distill_engine._merge_into)
    assert "summary_json=?" not in rebuild
    assert "chapters_json=?" not in rebuild
    assert "field_update_ops(" in rebuild
    assert "chapter_update_ops(" in rebuild
    assert "_apply_then_sidecar(" in rebuild
    assert "summary_json=?" not in open_src
    assert "chapters_json=?" not in open_src
    assert "_insert_crystal_page(" in open_src
    assert "_insert_crystal_page(" in owner
    assert "ThinCrystal(" in create
    assert "put(" in create
    assert "尚无来源" not in open_src
    assert "尚无来源" not in owner
    assert "chapters_json=?" not in strike
    assert "chapters_json=?" not in move
    assert "chapters_json=?" not in merge
    assert "chapter_update_ops(" in strike
    assert "chapter_update_ops(" in move
    assert "chapter_update_ops(" in merge
    assert "_apply_then_sidecar(" in strike
    assert "_apply_then_sidecar(" in move
    assert "_apply_then_sidecar(" in merge
    assert "summary_json=?" not in inspect.getsource(distill_engine._apply_rejudge_update)
    assert "summary_json=?" not in inspect.getsource(distill_engine._fold_spelling)
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_projection_switch_off_no_thick_or_thin_write(tmp_path, monkeypatch, write_switch_off):
    """关开关：回滚投影不得写 envelope，也不得改厚栏。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    now = engine.utcnow()
    engine.db().execute(
        "UPDATE crystallizations SET summary_json=? WHERE id='owner'",
        (json.dumps({"所在地": "旧港", "概述": "旧厚概述必须保持不动。"}, ensure_ascii=False),),
    )
    engine.db().execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,event_at,created_at)
           VALUES('st-proj','owner','所在地','南港',?,?, 'written','user',?,?)""",
        (CORPUS, "src-proj", now, now),
    )
    engine.db().commit()
    before = _snapshot(engine)
    fake = FakeCrystalStore()
    engine._rebuild_projection(engine.db(), "owner")
    engine._rebuild_projection(engine.db(), "owner", store=fake)
    after = _snapshot(engine)
    assert json.loads(after["summary_json"])["所在地"] == "旧港"
    assert after["envelope_json"] == before["envelope_json"]
    assert after["revision"] == before["revision"]
    assert fake.calls
    assert any(op.get("path") == "keys.所在地" and op.get("value") == "南港" for op in fake.calls[0]["ops"])
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_projection_unlock_writes_thin_envelope(tmp_path, monkeypatch):
    """开关已开：投影刷薄信封，厚栏不动。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    now = engine.utcnow()
    engine.db().execute(
        "UPDATE crystallizations SET summary_json=?, chapters_json=? WHERE id='owner'",
        (
            json.dumps({"所在地": "旧港", "概述": "旧厚概述必须保持不动。"}, ensure_ascii=False),
            json.dumps({"个人生活": "旧厚章节必须保持不动。"}, ensure_ascii=False),
        ),
    )
    engine.db().execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,event_at,created_at)
           VALUES('st-proj2','owner','所在地','南港',?,?, 'written','user',?,?)""",
        (CORPUS, "src-proj2", now, now),
    )
    engine.db().execute(
        """INSERT INTO statements(id,crystal_id,chapter,text,quote,source_id,trust,rank,event_at,created_at)
           VALUES('st-proj-ch','owner','个人生活','住在南港书院东侧。',?,?, 'written','user',?,?)""",
        (CORPUS, "src-proj2", now, now),
    )
    engine.db().commit()
    engine._rebuild_projection(engine.db(), "owner", holder="rebuild:projection", commit=True)
    row = _snapshot(engine)
    envelope = json.loads(row["envelope_json"])
    assert envelope["keys"]["所在地"] == "南港"
    assert "住在南港书院东侧。" in (row["body_md"] or "")
    assert json.loads(row["summary_json"])["所在地"] == "旧港"
    assert json.loads(row["chapters_json"])["个人生活"] == "旧厚章节必须保持不动。"
    detail = engine.crystal_detail("owner")
    assert detail["summary"]["所在地"] == "南港"
    assert "住在南港书院东侧。" in (detail["chapters"]["个人生活"] or "")
    section = engine.read_section("记忆主人", "个人生活")
    assert section["missing"] is False
    assert "住在南港书院东侧。" in (section["text"] or "")
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_open_switch_off_empty_row_no_thick_skeleton(tmp_path, monkeypatch, write_switch_off):
    """关开关：开篇只建空行，不写厚栏骨架，也不 put 薄信封。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    owner = _snapshot(engine)
    assert json.loads(owner["summary_json"]) == {}
    assert json.loads(owner["chapters_json"]) == {}
    source = _source(engine, "云岚甲是南港书院的讲师。", "src-open")
    engine._open_crystal(
        source,
        {"type": "人物", "name": "云岚甲", "quote": "云岚甲是南港书院的讲师。", "landed": True},
        {},
        "云岚甲是南港书院的讲师。",
    )
    row = engine.db().execute(
        """SELECT summary_json, chapters_json, envelope_json, body_md, revision
           FROM crystallizations WHERE name='云岚甲' AND status='active'"""
    ).fetchone()
    assert row is not None
    assert json.loads(row["summary_json"]) == {}
    assert json.loads(row["chapters_json"]) == {}
    assert not row["envelope_json"]
    assert not row["body_md"]
    stmt = engine.db().execute(
        "SELECT text FROM statements WHERE field='姓名' AND text='云岚甲' AND invalid_at IS NULL"
    ).fetchone()
    assert stmt is not None
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_open_unlock_puts_thin_envelope(tmp_path, monkeypatch):
    """开关已开：开篇 CrystalStore.put 薄空信封，姓名走 field ops。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    source = _source(engine, "云岚乙是南港书院的讲师。", "src-open2")
    engine._open_crystal(
        source,
        {"type": "人物", "name": "云岚乙", "quote": "云岚乙是南港书院的讲师。", "landed": True},
        {},
        "云岚乙是南港书院的讲师。",
    )
    row = engine.db().execute(
        """SELECT id, summary_json, chapters_json, envelope_json, revision
           FROM crystallizations WHERE name='云岚乙' AND status='active'"""
    ).fetchone()
    assert row is not None
    assert json.loads(row["summary_json"]) == {}
    assert json.loads(row["chapters_json"]) == {}
    envelope = json.loads(row["envelope_json"])
    assert envelope["keys"]["姓名"] == "云岚乙"
    detail = engine.crystal_detail(row["id"])
    assert detail["summary"]["姓名"] == "云岚乙"
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_ensure_owner_unlock_puts_thin_envelope(tmp_path, monkeypatch):
    """开关已开：本机页走 put 薄空信封，不写厚栏骨架。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    engine.db().execute("DELETE FROM crystallizations WHERE id='owner'")
    engine.db().commit()
    engine._ensure_owner(engine.db())
    row = _snapshot(engine)
    assert json.loads(row["summary_json"]) == {}
    assert json.loads(row["chapters_json"]) == {}
    assert row["envelope_json"]
    envelope = json.loads(row["envelope_json"])
    assert envelope["keys"] == {}
    detail = engine.crystal_detail("owner")
    assert list(detail["summary"]) == engine.OWNER_FIELDS
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_chapter_sweep_switch_off_no_thick_or_thin_write(tmp_path, monkeypatch, write_switch_off):
    """关开关：特性清扫 / 开工 / 合并不得写 chapters_json 或 envelope。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    chapters = {"计划中特性": "云岚检索", "开发中特性": "暂无来源", "发版日志": "暂无来源"}
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,revision)
           VALUES('proj-s','工程','云岚仓','active',?,?,0)""",
        ("{}", json.dumps(chapters, ensure_ascii=False)),
    )
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,revision)
           VALUES('keep-m','人物','云岚甲','active',?,?,0)""",
        ("{}", json.dumps({"个人生活": "甲在南港。"}, ensure_ascii=False)),
    )
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,revision)
           VALUES('drop-m','人物','云岚丙','active',?,?,0)""",
        ("{}", json.dumps({"个人生活": "丙在东岸。"}, ensure_ascii=False)),
    )
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready)
           VALUES('src-strike','user','本机','极空间',?,?,?,'云岚检索已经交付。发了 v1.0。','已解析','p0',1)""",
        (now, "file:src-strike", "hash-src-strike"),
    )
    engine.db().commit()
    before_proj = engine.db().execute(
        "SELECT chapters_json, envelope_json, revision FROM crystallizations WHERE id='proj-s'"
    ).fetchone()
    before_keep = engine.db().execute(
        "SELECT chapters_json, envelope_json, revision FROM crystallizations WHERE id='keep-m'"
    ).fetchone()
    fake = FakeCrystalStore()
    struck = engine._strike_features(engine.db())
    moved = engine._move_started_features(engine.db())
    engine._strike_features(engine.db(), store=fake)
    engine._merge_into(engine.db(), "keep-m", "drop-m", "src-strike", store=fake)
    after_proj = engine.db().execute(
        "SELECT chapters_json, envelope_json, revision FROM crystallizations WHERE id='proj-s'"
    ).fetchone()
    after_keep = engine.db().execute(
        "SELECT chapters_json, envelope_json FROM crystallizations WHERE id='keep-m'"
    ).fetchone()
    assert struck == 0
    assert moved == 0
    assert after_proj["chapters_json"] == before_proj["chapters_json"]
    assert after_proj["envelope_json"] == before_proj["envelope_json"]
    assert after_proj["revision"] == before_proj["revision"]
    assert after_keep["chapters_json"] == before_keep["chapters_json"]
    assert after_keep["envelope_json"] == before_keep["envelope_json"]
    assert fake.calls
    assert any(
        op.get("path") == "body_md##计划中特性" and "~~云岚检索~~" in str(op.get("value") or "")
        for call in fake.calls
        for op in call["ops"]
    )
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_chapter_sweep_unlock_writes_thin_body(tmp_path, monkeypatch):
    """开关已开：清扫 / 开工落入 body_md，厚 chapters_json 不动。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    thick = {"计划中特性": "云岚检索", "开发中特性": "暂无来源", "发版日志": "暂无来源"}
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,revision)
           VALUES('proj-u','工程','云岚仓','active',?,?,0)""",
        ("{}", json.dumps(thick, ensure_ascii=False)),
    )
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready)
           VALUES('src-strike2','user','本机','极空间',?,?,?,'云岚检索已经交付。发了 v1.0。','已解析','p0',1)""",
        (now, "file:src-strike2", "hash-src-strike2"),
    )
    engine.db().commit()
    struck = engine._strike_features(engine.db())
    engine.db().commit()
    assert struck == 1
    row = engine.db().execute(
        "SELECT chapters_json, envelope_json, body_md FROM crystallizations WHERE id='proj-u'"
    ).fetchone()
    assert json.loads(row["chapters_json"])["计划中特性"] == "云岚检索"
    assert "~~云岚检索~~" in (row["body_md"] or "")
    detail = engine.crystal_detail("proj-u")
    assert "~~云岚检索~~" in (detail["chapters"]["计划中特性"] or "")
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_ws_flip_move_and_merge_unlock_writes_thin_body(tmp_path, monkeypatch):
    """开关已开：开工搬迁与合并走章节 ops，厚 blob 不动。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,revision)
           VALUES('proj-mv','工程','云岚仓','active',?,?,0)""",
        ("{}", json.dumps({"计划中特性": "云岚检索", "开发中特性": "暂无来源"}, ensure_ascii=False)),
    )
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,revision)
           VALUES('keep-u','人物','云岚甲','active',?,?,0)""",
        ("{}", json.dumps({"个人生活": "甲在南港。"}, ensure_ascii=False)),
    )
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,revision)
           VALUES('drop-u','人物','云岚丙','active',?,?,0)""",
        ("{}", json.dumps({"个人生活": "丙在东岸。"}, ensure_ascii=False)),
    )
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready)
           VALUES('src-move','user','本机','极空间',?,?,?,'云岚检索已经开工。','已解析','p0',1)""",
        (now, "file:src-move", "hash-src-move"),
    )
    engine.db().commit()
    moved = engine._move_started_features(engine.db())
    engine._merge_into(engine.db(), "keep-u", "drop-u", "src-move")
    engine.db().commit()
    assert moved == 1
    proj = engine.db().execute(
        "SELECT chapters_json, body_md FROM crystallizations WHERE id='proj-mv'"
    ).fetchone()
    keep = engine.db().execute(
        "SELECT chapters_json, body_md, envelope_json FROM crystallizations WHERE id='keep-u'"
    ).fetchone()
    drop = engine.db().execute("SELECT status FROM crystallizations WHERE id='drop-u'").fetchone()
    assert json.loads(proj["chapters_json"])["计划中特性"] == "云岚检索"
    assert "云岚检索" in (proj["body_md"] or "")
    assert "开发中特性" in (proj["body_md"] or "")
    assert json.loads(keep["chapters_json"])["个人生活"] == "甲在南港。"
    assert "丙在东岸。" in (keep["body_md"] or "")
    assert drop["status"] == "已合并"
    detail = engine.crystal_detail("keep-u")
    assert "丙在东岸。" in (detail["chapters"]["个人生活"] or "")
    assert DISTILLER_STORE_WRITE_ENABLED is True
