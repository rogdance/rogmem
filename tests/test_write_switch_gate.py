"""Write-switch gate: apply-first 共享 helper + unresolved 引擎闭环。写开关已开。

QA 主线（WS-APPLY / WS-UR；关开关仍 fail-closed、不做 live re-sim）:

| id | cut | 锁什么 |
|---|---|---|
| chapter_apply_ops | done | #60 `_write_chapter` → apply_ops |
| field_apply_ops | done | #63 假 store 收到 keys.* / envelope path |
| field_thin_no_bypass | done | 瘦行不回退写 summary_json / chapters_json |
| unresolved_persist | done | #64 persist_unresolved 可落列 |
| legacy_thick_removed | done | #64 `_legacy_thick_*` 已删 |
| statements_edges_same_txn | done | #64 apply_ops 与 statements/edges 同一次 commit |
| apply_first_shared | this | WS-APPLY field apply-first + 共享 helper |
| persist_unresolved_engine | this | WS-UR engine.apply_integrate 闭环落库 |
| sidecar_commit_same_try | this | WS-APPLY sidecar+commit 同 try，失败整单回滚 |
| write_switch_on | this | DISTILLER_STORE_WRITE_ENABLED 已为 True |
| live_resim | next | live MiniMax re-sim |

本刀相关项必须实跑，禁止 pytest.skip / xfail 占位。
可用 `pytest -m ws_apply` / `pytest -m ws_ur` / `pytest -m ws_leg` /
`pytest -k 'WS_APPLY or WS_UR or WS_LEG'`。
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

import pytest

from rogmem.crystal_store import (
    DISTILLER_STORE_WRITE_ENABLED,
    CrystalStore,
    ThinCrystal,
    chapter_update_ops,
    field_update_ops,
    migrate_crystal_store,
)
from rogmem import engine as distill_engine

WRITE_SWITCH_GATE = (
    {"id": "chapter_apply_ops", "cut": "done"},
    {"id": "field_apply_ops", "cut": "done"},
    {"id": "field_thin_no_bypass", "cut": "done"},
    {"id": "unresolved_persist", "cut": "done"},
    {"id": "legacy_thick_removed", "cut": "done"},
    {"id": "statements_edges_same_txn", "cut": "done"},
    {"id": "apply_first_shared", "cut": "this"},
    {"id": "persist_unresolved_engine", "cut": "this"},
    {"id": "sidecar_commit_same_try", "cut": "this"},
    {"id": "write_switch_on", "cut": "this"},
    {"id": "live_resim", "cut": "next"},
)

CORPUS = "云岚测试员甲于 2019 年在北窗实验室任职。后来到南港书院做讲师。"
CANDIDATES = ["owner"]
BAD_NEW_EVIDENCE = "正文里没有这句冲突出处"


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
                "commit": commit,
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


def _source(engine, body: str, source_id: str = "src-field-wire"):
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready)
           VALUES(?,'user','本机','极空间',?,?,?,?,'已解析','p0',1)""",
        (source_id, now, f"file:{source_id}", f"hash-{source_id}", body),
    )
    engine.db().commit()
    return engine.db().execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()


def _snapshot(engine):
    return engine.db().execute(
        """SELECT summary_json, chapters_json, envelope_json, body_md, revision, unresolved_conflicts
           FROM crystallizations WHERE id='owner'"""
    ).fetchone()


def test_write_switch_gate_this_cut_items_run():
    """本刀项必须有实跑测试；下一刀项只登记，不得占位跳过。"""
    this = {row["id"] for row in WRITE_SWITCH_GATE if row["cut"] == "this"}
    nxt = {row["id"] for row in WRITE_SWITCH_GATE if row["cut"] == "next"}
    done = {row["id"] for row in WRITE_SWITCH_GATE if row["cut"] == "done"}
    assert this == {
        "apply_first_shared",
        "persist_unresolved_engine",
        "sidecar_commit_same_try",
        "write_switch_on",
    }
    assert nxt == {"live_resim"}
    assert done == {
        "chapter_apply_ops",
        "field_apply_ops",
        "field_thin_no_bypass",
        "unresolved_persist",
        "legacy_thick_removed",
        "statements_edges_same_txn",
    }
    assert DISTILLER_STORE_WRITE_ENABLED is True
    for item in WRITE_SWITCH_GATE:
        assert item["cut"] in {"done", "this", "next"}
        assert item["cut"] != "skip"


def test_field_apply_ops_fake_store_gets_keys_path(tmp_path, monkeypatch):
    """field_apply_ops: fake store receives keys.所在地; switch is on; injected store does not persist."""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    body = "现住南港。"
    source = _source(engine, body)
    before = _snapshot(engine)
    fake = FakeCrystalStore()

    engine._write_field(
        source,
        {"field": "所在地", "value": "南港", "quote": body, "crystal": "记忆主人", "trust": "written"},
        "user",
        body,
        store=fake,
    )

    assert DISTILLER_STORE_WRITE_ENABLED is True
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["crystal_id"] == "owner"
    assert call["ops"] == field_update_ops("所在地", "南港")
    assert call["ops"] == [{"op": "update", "path": "keys.所在地", "value": "南港"}]
    assert call["holder"]
    assert call["commit"] is False
    after = _snapshot(engine)
    assert after["summary_json"] == before["summary_json"]
    assert after["chapters_json"] == before["chapters_json"]
    assert after["envelope_json"] == before["envelope_json"]
    assert after["body_md"] == before["body_md"]
    assert after["revision"] == before["revision"]
    assert "南港" not in (after["summary_json"] or "")
    assert engine.db().execute(
        "SELECT 1 FROM statements WHERE crystal_id='owner' AND quote=?", (body,)
    ).fetchone() is None


def test_field_apply_ops_unverified_uses_chapter_shape(tmp_path, monkeypatch):
    """_write_field 待核实走章节 op，同样只经 apply_ops。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    body = "好像现住南港。"
    source = _source(engine, body, "src-field-unverified")
    before = _snapshot(engine)
    fake = FakeCrystalStore()
    note = engine._pending_note("user", "南港", body)

    engine._write_field(
        source,
        {"field": "所在地", "value": "南港", "quote": body, "crystal": "记忆主人", "trust": "unverified"},
        "user",
        body,
        store=fake,
    )

    assert DISTILLER_STORE_WRITE_ENABLED is True
    assert fake.calls[0]["ops"] == chapter_update_ops("个人生活", note)
    after = _snapshot(engine)
    assert after["summary_json"] == before["summary_json"]
    assert after["chapters_json"] == before["chapters_json"]
    assert after["revision"] == before["revision"]


def test_field_thin_row_does_not_fall_back_to_summary_json(tmp_path, monkeypatch):
    """field_thin_no_bypass: 行已是瘦信封时，真写落 keys，不得再直写 summary/chapters。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    body = "现住南港。"
    source = _source(engine, body, "src-field-thin")
    engine.db().execute(
        "UPDATE crystallizations SET envelope_json=?, body_md=?, summary_json=?, chapters_json=? WHERE id='owner'",
        (
            json.dumps(
                {
                    "summary": "瘦信封",
                    "aliases": [],
                    "owner_relation": "本人",
                    "facts": [],
                    "inferences": [],
                    "keys": {"所在地": "旧瘦所在地"},
                },
                ensure_ascii=False,
            ),
            "## 个人生活\n\n旧瘦正文。",
            json.dumps({"所在地": "这段厚摘要必须保持不动。"}, ensure_ascii=False),
            json.dumps({"个人生活": "这段厚章节必须保持不动。"}, ensure_ascii=False),
        ),
    )
    engine.db().commit()

    engine._write_field(
        source,
        {"field": "所在地", "value": "南港", "quote": body, "crystal": "记忆主人", "trust": "written"},
        "user",
        body,
    )

    row = _snapshot(engine)
    assert json.loads(row["summary_json"])["所在地"] == "这段厚摘要必须保持不动。"
    assert json.loads(row["chapters_json"])["个人生活"] == "这段厚章节必须保持不动。"
    assert row["body_md"] == "## 个人生活\n\n旧瘦正文。"
    assert "南港" not in (row["summary_json"] or "")
    assert "南港" not in (row["chapters_json"] or "")
    envelope = json.loads(row["envelope_json"])
    assert envelope["keys"]["所在地"] == "南港"
    stmt = engine.db().execute(
        "SELECT text FROM statements WHERE crystal_id='owner' AND quote=?", (body,)
    ).fetchone()
    assert stmt is not None and stmt["text"] == "南港"


def test_write_switch_is_on():
    """write_switch_on: 生产写开关已开。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True


@pytest.mark.ws_ur
def test_WS_UR_unresolved_persist_lands_on_crystal(tmp_path, monkeypatch):
    """WS-UR: engine.apply_integrate 闭环把 accept 上的 uncertain 写入列。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    before = _snapshot(engine)
    payload = {
        "identity": "same",
        "matched_id": "owner",
        "ops": [
            {
                "op": "update",
                "path": "org_title",
                "covering": True,
                "old": "北窗实验室 研究员",
                "claim": "当前是南港书院讲师",
                "fact": {
                    "claim": "当前是南港书院讲师",
                    "evidence": "后来到南港书院做讲师。",
                },
            }
        ],
        "conflicts": [
            {
                "path": "org_title",
                "verdict": "supersede",
                "evidence_new": BAD_NEW_EVIDENCE,
                "old_epistemics": "asserted",
                "new_epistemics": "asserted",
            }
        ],
    }
    result = engine.apply_integrate(
        payload,
        candidates=CANDIDATES,
        corpus=CORPUS,
        store=CrystalStore(engine.db()),
        crystal_id="owner",
    )
    assert result.decision == "accept"
    assert result.unresolved_conflicts
    assert all(item.get("verdict") == "uncertain" for item in result.unresolved_conflicts)

    row = _snapshot(engine)
    saved = json.loads(row["unresolved_conflicts"] or "[]")
    assert saved == result.unresolved_conflicts
    assert {item.get("path") for item in saved} == {"org_title"}
    assert all(item.get("verdict") == "uncertain" for item in saved)
    assert row["summary_json"] == before["summary_json"]
    assert row["chapters_json"] == before["chapters_json"]
    assert DISTILLER_STORE_WRITE_ENABLED is True


@pytest.mark.ws_ur
def test_WS_UR_engine_path_reject_does_not_write_empty_accept_clears(tmp_path, monkeypatch):
    """WS-UR: engine 路径 reject/deferred 不写；accept 空列表清列。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    store = CrystalStore(engine.db())
    accepted = engine.apply_integrate(
        {
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
            "conflicts": [
                {
                    "path": "org_title",
                    "verdict": "uncertain",
                    "reason": "两处说法暂时无法裁断",
                }
            ],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
        store=store,
        crystal_id="owner",
    )
    assert accepted.decision == "accept"
    row = json.loads(_snapshot(engine)["unresolved_conflicts"] or "[]")
    assert len(row) == 1
    assert row[0]["path"] == "org_title"
    assert row[0]["verdict"] == "uncertain"

    rejected = engine.apply_integrate(
        {"identity": "same", "matched_id": "ghost", "ops": [], "conflicts": []},
        candidates=CANDIDATES,
        corpus=CORPUS,
        store=store,
        crystal_id="owner",
    )
    assert rejected.decision == "rejected"
    still = json.loads(_snapshot(engine)["unresolved_conflicts"] or "[]")
    assert still == row

    deferred = engine.apply_integrate(
        "这不是 JSON",
        candidates=CANDIDATES,
        corpus=CORPUS,
        store=store,
        crystal_id="owner",
    )
    assert deferred.decision == "deferred"
    assert json.loads(_snapshot(engine)["unresolved_conflicts"] or "[]") == row

    cleared = engine.apply_integrate(
        {
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
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
        store=store,
        crystal_id="owner",
    )
    assert cleared.decision == "accept"
    assert cleared.unresolved_conflicts == []
    assert json.loads(_snapshot(engine)["unresolved_conflicts"] or "[]") == []
    assert DISTILLER_STORE_WRITE_ENABLED is True


@pytest.mark.ws_ur
def test_WS_UR_engine_apply_integrate_is_the_persist_call_site():
    """WS-UR: persist_unresolved 由 engine.apply_integrate 调用，不是只停在 API 缝。"""
    apply_src = inspect.getsource(distill_engine.apply_integrate)
    field_src = inspect.getsource(distill_engine._write_field)
    chapter_src = inspect.getsource(distill_engine._write_chapter)
    assert "persist_unresolved(" in apply_src
    assert "evaluate_integrate(" in apply_src
    assert "_apply_then_sidecar(" in apply_src
    assert "persist_unresolved(" not in field_src
    assert "persist_unresolved(" not in chapter_src
    assert DISTILLER_STORE_WRITE_ENABLED is True


@pytest.mark.ws_leg
def test_WS_LEG_legacy_thick_symbols_removed():
    """WS-LEG: `_legacy_thick_field_persist` / `_legacy_thick_chapter_persist` 已删除。"""
    from rogmem import engine

    assert not hasattr(engine, "_legacy_thick_field_persist")
    assert not hasattr(engine, "_legacy_thick_chapter_persist")
    source = inspect.getsource(engine)
    assert "_legacy_thick_field_persist" not in source
    assert "_legacy_thick_chapter_persist" not in source
    write_field = inspect.getsource(engine._write_field)
    write_chapter = inspect.getsource(engine._write_chapter)
    assert "summary_json=?" not in write_field
    assert "chapters_json=?" not in write_field
    assert "chapters_json=?" not in write_chapter
    assert DISTILLER_STORE_WRITE_ENABLED is True


@pytest.mark.ws_leg
def test_WS_LEG_thick_write_fail_closed_no_json_bypass(tmp_path, monkeypatch, write_switch_off):
    """WS-LEG: 关开关时厚行也不得回退写 summary/chapters，statements 一并回滚。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    body = "现住南港。"
    source = _source(engine, body, "src-leg-fail-closed")
    before = _snapshot(engine)
    assert not before["envelope_json"]

    engine._write_field(
        source,
        {"field": "所在地", "value": "南港", "quote": body, "crystal": "记忆主人", "trust": "written"},
        "user",
        body,
    )
    engine._write_chapter(
        source,
        {"chapter": "事业经历", "value": body, "quote": body, "crystal": "记忆主人"},
        body,
    )

    after = _snapshot(engine)
    assert after["summary_json"] == before["summary_json"]
    assert after["chapters_json"] == before["chapters_json"]
    assert after["envelope_json"] == before["envelope_json"]
    assert after["body_md"] == before["body_md"]
    assert after["revision"] == before["revision"]
    assert "南港" not in (after["summary_json"] or "")
    assert "南港" not in (after["chapters_json"] or "")
    assert engine.db().execute(
        "SELECT 1 FROM statements WHERE crystal_id='owner' AND quote=?", (body,)
    ).fetchone() is None
    assert DISTILLER_STORE_WRITE_ENABLED is True


def _sql_batches(trace: list[str]) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    for raw in trace:
        sql = " ".join(raw.split())
        current.append(sql)
        if sql.split(" ", 1)[0].upper() == "COMMIT":
            batches.append(current)
            current = []
    if current:
        batches.append(current)
    return batches


@pytest.mark.ws_leg
def test_WS_LEG_statements_edges_same_txn_as_apply_ops(tmp_path, monkeypatch):
    """WS-LEG: apply_ops 成功后 statements/edges 与 crystal apply 同一次 commit。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    monkeypatch.setattr("rogmem.crystal_store.DISTILLER_STORE_WRITE_ENABLED", True)
    engine = _engine(tmp_path, monkeypatch)
    body = "现住南港。"
    source = _source(engine, body, "src-same-txn")
    con = engine.db()
    store = CrystalStore(con)
    store.acquire_lease("owner", "distill:src-same-txn")

    trace: list[str] = []
    con.set_trace_callback(trace.append)
    engine._write_field(
        source,
        {"field": "所在地", "value": "南港", "quote": body, "crystal": "记忆主人", "trust": "written"},
        "user",
        body,
    )
    con.set_trace_callback(None)

    batches = _sql_batches(trace)
    shared = [
        batch
        for batch in batches
        if any("envelope_json" in sql and "crystallizations" in sql.lower() for sql in batch)
        and any("insert into statements" in sql.lower() for sql in batch)
        and any(sql.split(" ", 1)[0].upper() == "COMMIT" for sql in batch)
    ]
    assert shared, trace
    helper = inspect.getsource(engine._apply_then_sidecar)
    assert "commit=False" in helper
    assert "con.commit()" in helper
    assert "sidecar()" in helper
    envelope_at = next(i for i, sql in enumerate(shared[0]) if "envelope_json" in sql)
    insert_at = next(i for i, sql in enumerate(shared[0]) if "insert into statements" in sql.lower())
    assert envelope_at < insert_at, shared[0]

    row = _snapshot(engine)
    assert row["envelope_json"] and "南港" in row["envelope_json"]
    stmt = engine.db().execute(
        "SELECT text FROM statements WHERE crystal_id='owner' AND quote=?",
        (body,),
    ).fetchone()
    assert stmt is not None and stmt["text"] == "南港"
    assert DISTILLER_STORE_WRITE_ENABLED is True


@pytest.mark.ws_leg
def test_WS_LEG_apply_ops_commit_false_holds_sidecar_until_shared_commit(tmp_path, monkeypatch):
    """WS-LEG: apply_ops(commit=False) 后 statements 未提交；一次 commit 两边一起可见。"""
    monkeypatch.setattr("rogmem.crystal_store.DISTILLER_STORE_WRITE_ENABLED", True)
    root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "same-txn.sqlite"
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript((root / "rogmem" / "schema.sql").read_text(encoding="utf-8"))
    store = CrystalStore(con)
    store.put(
        ThinCrystal(
            id="k1",
            type="人物",
            name="甲",
            status="active",
            revision=0,
            summary="旧摘要",
        )
    )
    store.acquire_lease("k1", "integrate-1")
    store.apply_ops(
        "k1",
        0,
        [{"op": "update", "path": "keys.location", "value": "南港"}],
        holder="integrate-1",
        commit=False,
    )
    con.execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,created_at)
           VALUES('st1','k1','所在地','南港','现住南港。','src','written','user','2026-10-29T00:00:00+00:00')"""
    )

    peek = sqlite3.connect(db_path)
    peek.row_factory = sqlite3.Row
    migrate_crystal_store(peek)
    hidden = peek.execute("SELECT envelope_json FROM crystallizations WHERE id='k1'").fetchone()
    hidden_stmt = peek.execute("SELECT 1 FROM statements WHERE id='st1'").fetchone()
    assert not hidden["envelope_json"] or "南港" not in (hidden["envelope_json"] or "")
    assert hidden_stmt is None

    con.commit()
    visible = peek.execute("SELECT envelope_json FROM crystallizations WHERE id='k1'").fetchone()
    visible_stmt = peek.execute("SELECT text FROM statements WHERE id='st1'").fetchone()
    assert "南港" in visible["envelope_json"]
    assert visible_stmt["text"] == "南港"
    peek.close()
    con.close()
    assert DISTILLER_STORE_WRITE_ENABLED is True


@pytest.mark.ws_apply
def test_WS_APPLY_field_and_chapter_share_apply_sidecar_helper():
    """WS-APPLY: field/chapter 共用 apply+sidecar+commit helper，不各自复制一份。"""
    helper = inspect.getsource(distill_engine._apply_then_sidecar)
    field_src = inspect.getsource(distill_engine._write_field)
    chapter_src = inspect.getsource(distill_engine._write_chapter)
    assert "_apply_then_sidecar(" in field_src
    assert "_apply_then_sidecar(" in chapter_src
    assert "target_store.apply_ops" in helper
    assert "sidecar()" in helper
    assert "con.commit()" in helper
    assert "IntegrateWriteNotLive" in helper
    assert "target_store.apply_ops" not in field_src
    assert "target_store.apply_ops" not in chapter_src
    assert "con.commit()" not in field_src
    assert field_src.count("_apply_then_sidecar(") == 1
    assert chapter_src.count("_apply_then_sidecar(") == 1
    assert DISTILLER_STORE_WRITE_ENABLED is True


@pytest.mark.ws_apply
def test_WS_APPLY_switch_off_does_not_insert_then_rollback(tmp_path, monkeypatch, write_switch_off):
    """WS-APPLY: 关开关时 apply-first fail-closed，不先插 statement 再回滚。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    body = "现住南港。"
    source = _source(engine, body, "src-apply-first")
    added: list[tuple] = []
    original = engine._add_statement

    def spy(*args, **kwargs):
        added.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "_add_statement", spy)
    engine._write_field(
        source,
        {"field": "所在地", "value": "南港", "quote": body, "crystal": "记忆主人", "trust": "written"},
        "user",
        body,
    )
    engine._write_chapter(
        source,
        {"chapter": "事业经历", "value": body, "quote": body, "crystal": "记忆主人"},
        body,
    )
    assert added == []
    assert engine.db().execute(
        "SELECT 1 FROM statements WHERE crystal_id='owner' AND quote=?", (body,)
    ).fetchone() is None
    assert DISTILLER_STORE_WRITE_ENABLED is True


def _unlock_and_lease(engine, crystal_id: str, holder: str, monkeypatch):
    monkeypatch.setattr("rogmem.crystal_store.DISTILLER_STORE_WRITE_ENABLED", True)
    store = CrystalStore(engine.db())
    store.acquire_lease(crystal_id, holder)
    return store


@pytest.mark.ws_apply
def test_WS_APPLY_field_sidecar_failure_rolls_back_whole_txn(tmp_path, monkeypatch):
    """WS-APPLY: field sidecar 失败时 apply 与 statements 整单回滚。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    body = "现住南港。"
    source = _source(engine, body, "src-sidecar-field")
    before = _snapshot(engine)
    _unlock_and_lease(engine, "owner", "distill:src-sidecar-field", monkeypatch)

    def boom(*_args, **_kwargs):
        raise RuntimeError("sidecar boom")

    monkeypatch.setattr(engine, "_record_revision", boom)
    with pytest.raises(RuntimeError, match="sidecar boom"):
        engine._write_field(
            source,
            {"field": "所在地", "value": "南港", "quote": body, "crystal": "记忆主人", "trust": "written"},
            "user",
            body,
        )

    after = _snapshot(engine)
    assert after["envelope_json"] == before["envelope_json"]
    assert after["summary_json"] == before["summary_json"]
    assert after["revision"] == before["revision"]
    assert "南港" not in (after["envelope_json"] or "")
    assert engine.db().execute(
        "SELECT 1 FROM statements WHERE crystal_id='owner' AND quote=?", (body,)
    ).fetchone() is None
    assert DISTILLER_STORE_WRITE_ENABLED is True


@pytest.mark.ws_apply
def test_WS_APPLY_chapter_sidecar_failure_rolls_back_whole_txn(tmp_path, monkeypatch):
    """WS-APPLY: chapter sidecar 失败时 apply 与 statements 整单回滚。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "llm_target", lambda _role: None)
    body = "2019 年在南港书院做讲师。"
    source = _source(engine, body, "src-sidecar-chapter")
    before = _snapshot(engine)
    _unlock_and_lease(engine, "owner", "distill:src-sidecar-chapter", monkeypatch)

    def boom(*_args, **_kwargs):
        raise RuntimeError("sidecar boom")

    monkeypatch.setattr(engine, "_record_revision", boom)
    with pytest.raises(RuntimeError, match="sidecar boom"):
        engine._write_chapter(
            source,
            {"chapter": "事业经历", "value": body, "quote": body, "crystal": "记忆主人"},
            body,
        )

    after = _snapshot(engine)
    assert after["envelope_json"] == before["envelope_json"]
    assert after["chapters_json"] == before["chapters_json"]
    assert after["body_md"] == before["body_md"]
    assert after["revision"] == before["revision"]
    assert "南港书院" not in (after["envelope_json"] or "")
    assert "南港书院" not in (after["body_md"] or "")
    assert engine.db().execute(
        "SELECT 1 FROM statements WHERE crystal_id='owner' AND quote=?", (body,)
    ).fetchone() is None
    assert DISTILLER_STORE_WRITE_ENABLED is True
