"""CrystalStore thin-only reads, lease, and no dual-write (v3 §7 / §11.1)."""

from __future__ import annotations

import inspect
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rogmem.crystal_store import (
    DISTILLER_STORE_WRITE_ENABLED,
    CrystalStore,
    Fact,
    IntegrateWriteNotLive,
    InvalidTransition,
    LeaseConflict,
    RevisionConflict,
    ThickReadDeprecated,
    ThinCrystal,
    _apply_one,
    apply_op_supported,
    assert_distiller_write_not_flipped,
    chapter_update_ops,
    migrate_crystal_store,
    project_thick,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "rogmem" / "schema.sql").read_text(encoding="utf-8")


def _con(tmp_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(tmp_path / "crystal.sqlite")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    migrate_crystal_store(con)
    return con


@pytest.fixture
def unlock_writes(monkeypatch):
    monkeypatch.setattr("rogmem.crystal_store.DISTILLER_STORE_WRITE_ENABLED", True)


def _insert_thick(con: sqlite3.Connection, cid: str = "p-厚页") -> None:
    summary = json.dumps(
        {
            "名称": "云岚测试员甲",
            "别名": "测试员甲",
            "概述": "北窗实验室的人。",
            "与记忆主人的关系": "同事",
            "所属组织及职务": "北窗实验室 研究员",
        },
        ensure_ascii=False,
    )
    chapters = json.dumps(
        {
            "事业经历": "2019 年在北窗实验室任职。",
            "教育背景": "暂无来源",
            "个人生活": "尚无来源",
        },
        ensure_ascii=False,
    )
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,revision) VALUES(?,?,?,'active',?,?,0)",
        (cid, "人物", "云岚测试员甲", summary, chapters),
    )
    con.commit()


def test_thick_row_get_stays_thin_and_does_not_project(tmp_path):
    con = _con(tmp_path)
    _insert_thick(con)
    store = CrystalStore(con)
    got = store.get("p-厚页")
    assert got is not None
    assert got.storage == "thin"
    assert "2019 年在北窗实验室任职。" not in (got.body_md or "")
    assert got.summary == ""
    assert got.aliases == []
    assert got.facts == []
    row = con.execute(
        "SELECT summary_json, chapters_json FROM crystallizations WHERE id='p-厚页'"
    ).fetchone()
    assert "北窗实验室的人。" in row["summary_json"]
    assert "2019 年在北窗实验室任职。" in row["chapters_json"]


def test_kill_switch_blocks_put_and_apply_ops(tmp_path, write_switch_off):
    assert DISTILLER_STORE_WRITE_ENABLED is True
    con = _con(tmp_path)
    _insert_thick(con)
    store = CrystalStore(con)
    before = store.get("p-厚页")
    assert before.storage == "thin"
    assert before.summary == ""
    with pytest.raises(IntegrateWriteNotLive):
        store.put(
            ThinCrystal(
                id="p-厚页",
                type="人物",
                name="云岚测试员甲",
                status="active",
                revision=0,
                summary="不该写入",
                body_md="瘦写必须被杀开关拦住",
            )
        )
    with pytest.raises(IntegrateWriteNotLive):
        store.apply_ops(
            "p-厚页",
            0,
            [{"op": "update", "path": "body_md", "value": "补丁"}],
            holder="integrate-1",
        )
    with pytest.raises(IntegrateWriteNotLive):
        store.transition("p-厚页", "merged", extra={"merged_into": "x"})
    got = store.get("p-厚页")
    assert got.storage == "thin"
    assert got.summary == ""
    row = con.execute("SELECT envelope_json, body_md, summary_json FROM crystallizations WHERE id='p-厚页'").fetchone()
    assert row["envelope_json"] is None
    assert row["body_md"] is None
    assert "北窗实验室的人。" in row["summary_json"]


def test_thin_put_does_not_mutate_chapters_json(tmp_path, unlock_writes):
    con = _con(tmp_path)
    _insert_thick(con)
    before = con.execute(
        "SELECT chapters_json, summary_json FROM crystallizations WHERE id='p-厚页'"
    ).fetchone()
    store = CrystalStore(con)
    projected = store.get("p-厚页")
    assert projected is not None
    store.put(
        ThinCrystal(
            id="p-厚页",
            type="人物",
            name="云岚测试员甲",
            status="active",
            revision=projected.revision,
            summary="瘦信封摘要",
            aliases=["测试员甲"],
            owner_relation="同事",
            body_md="## 事业经历\n\n2019 年在北窗实验室任职。\n后到南港书院。",
            facts=[],
        )
    )
    after = con.execute(
        "SELECT chapters_json, summary_json, envelope_json, body_md FROM crystallizations WHERE id='p-厚页'"
    ).fetchone()
    assert after["chapters_json"] == before["chapters_json"]
    assert after["summary_json"] == before["summary_json"]
    assert after["envelope_json"]
    assert "南港书院" in after["body_md"]
    got = store.get("p-厚页")
    assert got.storage == "thin"
    assert got.summary == "瘦信封摘要"
    assert "南港书院" in got.body_md
    assert "北窗实验室的人" not in got.summary


def test_thin_wins_over_stale_thick_and_does_not_reproject(tmp_path, unlock_writes):
    con = _con(tmp_path)
    _insert_thick(con)
    store = CrystalStore(con)
    store.put(
        ThinCrystal(
            id="p-厚页",
            type="人物",
            name="云岚测试员甲",
            status="active",
            revision=0,
            summary="只认瘦",
            body_md="## 事业经历\n\n瘦正文。",
        )
    )
    con.execute(
        "UPDATE crystallizations SET chapters_json=? WHERE id='p-厚页'",
        (json.dumps({"事业经历": "这段厚正文必须被忽略"}, ensure_ascii=False),),
    )
    con.commit()
    got = store.get("p-厚页")
    assert got.storage == "thin"
    assert got.body_md == "## 事业经历\n\n瘦正文。"
    assert "必须被忽略" not in got.body_md


def test_new_thin_row_uses_empty_thick_placeholders(tmp_path, unlock_writes):
    store = CrystalStore(_con(tmp_path))
    got = store.put(
        ThinCrystal(
            id="d-new",
            type="人物",
            name="南港讲师乙",
            status="draft",
            revision=0,
            summary="新草稿",
            body_md="## 事业经历\n\n在南港书院做讲师。",
        )
    )
    assert got.storage == "thin"
    assert got.status == "draft"
    row = store.con.execute("SELECT chapters_json, summary_json FROM crystallizations WHERE id='d-new'").fetchone()
    assert json.loads(row["chapters_json"]) == {}
    assert json.loads(row["summary_json"]) == {}
    events = store.events("d-new")
    assert events[0]["kind"] == "draft_created"


def test_revision_conflict_does_not_write(tmp_path, unlock_writes):
    store = CrystalStore(_con(tmp_path))
    first = store.put(ThinCrystal(id="r1", type="人物", name="甲", status="active", revision=0, body_md="一"))
    store.put(ThinCrystal(id="r1", type="人物", name="甲", status="active", revision=first.revision, body_md="二"))
    with pytest.raises(RevisionConflict):
        store.put(ThinCrystal(id="r1", type="人物", name="甲", status="active", revision=first.revision, body_md="三"))
    assert store.get("r1").body_md == "二"


def test_unknown_op_hard_fails_and_does_not_write(tmp_path, unlock_writes):
    """Regression: unknown ops used to return the crystal unchanged."""
    crystal = ThinCrystal(
        id="x",
        type="人物",
        name="甲",
        status="active",
        revision=0,
        body_md="旧正文",
        facts=[Fact(claim="旧事实", evidence="引")],
    )
    with pytest.raises(ValueError, match="illegal op 'patch'"):
        _apply_one(crystal, {"op": "patch", "path": "facts", "value": {"claim": "不该写入"}})
    with pytest.raises(ValueError, match="illegal op 'update' path='chapters.wiki_score'"):
        _apply_one(crystal, {"op": "update", "path": "chapters.wiki_score", "value": 1})
    assert apply_op_supported({"op": "update", "path": "chapters.wiki_score"}) is False
    assert apply_op_supported({"op": "update", "path": "summary"}) is True
    assert apply_op_supported({"op": "update", "path": "org_title"}) is True
    assert crystal.body_md == "旧正文"
    assert [fact.claim for fact in crystal.facts] == ["旧事实"]

    store = CrystalStore(_con(tmp_path))
    store.put(crystal)
    store.acquire_lease("x", "integrate-1")
    with pytest.raises(ValueError, match="illegal op"):
        store.apply_ops(
            "x",
            0,
            [{"op": "merge", "path": "facts", "fact": {"claim": "静默会丢"}}],
            holder="integrate-1",
        )
    got = store.get("x")
    assert got.body_md == "旧正文"
    assert [fact.claim for fact in got.facts] == ["旧事实"]
    assert got.revision == 0
    assert [item for item in store.events("x") if item["kind"] == "apply_op"] == []


def test_apply_ops_mid_batch_failure_writes_no_events(tmp_path, unlock_writes):
    store = CrystalStore(_con(tmp_path))
    store.put(
        ThinCrystal(id="x", type="人物", name="甲", status="active", revision=0, body_md="旧正文")
    )
    store.acquire_lease("x", "integrate-1")
    assert [item for item in store.events("x") if item["kind"] == "apply_op"] == []
    with pytest.raises(ValueError, match="illegal op"):
        store.apply_ops(
            "x",
            0,
            [
                {"op": "update", "path": "body_md", "value": "先改正文"},
                {"op": "merge", "path": "facts", "fact": {"claim": "第二刀失败"}},
            ],
            holder="integrate-1",
        )
    got = store.get("x")
    assert got.body_md == "旧正文"
    assert got.revision == 0
    assert [item for item in store.events("x") if item["kind"] == "apply_op"] == []


def test_apply_updates_envelope_keys_not_silent_noop(tmp_path, unlock_writes):
    crystal = ThinCrystal(
        id="k1",
        type="人物",
        name="甲",
        status="active",
        revision=0,
        summary="旧摘要",
        body_md="旧正文",
    )
    after_title = _apply_one(crystal, {"op": "update", "path": "org_title", "value": "南港书院 讲师"})
    assert after_title.keys["org_title"] == "南港书院 讲师"
    after_summary = _apply_one(crystal, {"op": "update", "path": "summary", "value": "新摘要"})
    assert after_summary.summary == "新摘要"

    store = CrystalStore(_con(tmp_path))
    store.put(crystal)
    store.acquire_lease("k1", "integrate-1")
    updated = store.apply_ops(
        "k1",
        0,
        [{"op": "update", "path": "keys.location", "value": "南港"}],
        holder="integrate-1",
    )
    assert updated.keys["org_title"] == "南港书院 讲师"
    assert updated.keys["location"] == "南港"
    assert updated.summary == "新摘要"
    assert updated.revision == 1


def test_open_key_paths_apply_without_field_allowlist(tmp_path, unlock_writes):
    import rogmem.crystal_store as cs

    assert not hasattr(cs, "KEY_FIELDS")
    src = inspect.getsource(cs._envelope_key) + inspect.getsource(cs.apply_op_supported)
    assert "gender" not in src
    assert "launched_at" not in src

    crystal = ThinCrystal(
        id="open-k",
        type="人物",
        name="甲",
        status="active",
        revision=0,
        summary="旧摘要",
    )
    after_foo = _apply_one(crystal, {"op": "update", "path": "keys.foo", "value": "新槽"})
    assert after_foo.keys["foo"] == "新槽"
    after_nick = _apply_one(crystal, {"op": "update", "path": "keys.nickname", "value": "甲甲"})
    assert after_nick.keys["nickname"] == "甲甲"
    after_bare = _apply_one(crystal, {"op": "add", "path": "custom_slot", "value": "开路径"})
    assert after_bare.keys["custom_slot"] == "开路径"
    assert apply_op_supported({"op": "update", "path": "keys.foo"}) is True
    assert apply_op_supported({"op": "delete", "path": "keys.nickname"}) is True
    assert apply_op_supported({"op": "update", "path": "summary"}) is True
    assert apply_op_supported({"op": "update", "path": "chapters.wiki_score"}) is False
    assert apply_op_supported(chapter_update_ops("事业经历", "补一节")[0]) is True

    store = CrystalStore(_con(tmp_path))
    store.put(crystal)
    store.acquire_lease("open-k", "integrate-1")
    updated = store.apply_ops(
        "open-k",
        0,
        [{"op": "update", "path": "keys.foo", "value": "落库"}],
        holder="integrate-1",
    )
    assert updated.keys["foo"] == "落库"
    assert updated.keys["nickname"] == "甲甲"
    assert updated.revision == 1


def test_put_and_transition_require_lease_when_held(tmp_path, unlock_writes):
    store = CrystalStore(_con(tmp_path))
    first = store.put(ThinCrystal(id="c1", type="人物", name="甲", status="draft", revision=0))
    store.acquire_lease("c1", "worker-a")
    with pytest.raises(LeaseConflict):
        store.put(ThinCrystal(id="c1", type="人物", name="甲", status="draft", revision=first.revision))
    with pytest.raises(LeaseConflict):
        store.transition("c1", "deferred", extra={"maybe_with": "old-1"})
    with pytest.raises(LeaseConflict):
        store.put(
            ThinCrystal(id="c1", type="人物", name="甲", status="draft", revision=first.revision),
            holder="worker-b",
        )
    held = store.put(
        ThinCrystal(id="c1", type="人物", name="甲", status="draft", revision=first.revision, body_md="持租约写"),
        holder="worker-a",
    )
    assert held.body_md == "持租约写"
    deferred = store.transition("c1", "deferred", extra={"maybe_with": "old-1"}, holder="worker-a")
    assert deferred.status == "deferred"
    assert deferred.maybe_with == "old-1"


@pytest.mark.ws_ur
def test_WS_UR_persist_unresolved_does_not_need_write_switch(tmp_path):
    """WS-UR: unresolved 列不依赖写开关，不碰厚/瘦信封。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    con = _con(tmp_path)
    _insert_thick(con)
    store = CrystalStore(con)
    before = con.execute(
        "SELECT chapters_json, summary_json, envelope_json, body_md, revision FROM crystallizations WHERE id='p-厚页'"
    ).fetchone()
    unresolved = [{"path": "org_title", "verdict": "uncertain"}]
    loaded = store.persist_unresolved("p-厚页", unresolved)
    assert loaded.unresolved_conflicts == unresolved
    after = con.execute(
        "SELECT chapters_json, summary_json, envelope_json, body_md, revision, unresolved_conflicts FROM crystallizations WHERE id='p-厚页'"
    ).fetchone()
    assert after["chapters_json"] == before["chapters_json"]
    assert after["summary_json"] == before["summary_json"]
    assert after["envelope_json"] == before["envelope_json"]
    assert after["body_md"] == before["body_md"]
    assert after["revision"] == before["revision"]
    assert json.loads(after["unresolved_conflicts"]) == unresolved
    assert DISTILLER_STORE_WRITE_ENABLED is True


@pytest.mark.ws_leg
def test_WS_LEG_apply_ops_commit_false_defers_visibility(tmp_path, unlock_writes):
    """WS-LEG: apply_ops(commit=False) 在共享 commit 前对另一连接不可见。"""
    db_path = tmp_path / "crystal.sqlite"
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    store = CrystalStore(con)
    store.put(ThinCrystal(id="x", type="人物", name="甲", status="active", revision=0, body_md="旧"))
    store.acquire_lease("x", "integrate-1")
    store.apply_ops(
        "x",
        0,
        [{"op": "update", "path": "body_md", "value": "新正文"}],
        holder="integrate-1",
        commit=False,
    )
    peek = sqlite3.connect(db_path)
    peek.row_factory = sqlite3.Row
    migrate_crystal_store(peek)
    hidden = peek.execute("SELECT body_md FROM crystallizations WHERE id='x'").fetchone()
    assert hidden["body_md"] != "新正文"
    con.commit()
    visible = peek.execute("SELECT body_md FROM crystallizations WHERE id='x'").fetchone()
    assert visible["body_md"] == "新正文"
    peek.close()


def test_apply_ops_requires_lease_and_bumps_revision(tmp_path, unlock_writes):
    store = CrystalStore(_con(tmp_path))
    store.put(ThinCrystal(id="old", type="人物", name="甲", status="active", revision=0, body_md="旧"))
    store.put(ThinCrystal(id="draft", type="人物", name="甲", status="draft", revision=0, body_md="新"))
    with pytest.raises(LeaseConflict):
        store.apply_ops(
            "old",
            0,
            [{"op": "update", "path": "body_md", "value": "补丁"}],
            holder="integrate-1",
        )
    claimed = store.begin_distiller_write(draft_id="draft", matched_id="old", holder="integrate-1")
    assert claimed == ["draft", "old"]
    updated = store.apply_ops(
        "old",
        0,
        [{"op": "update", "path": "body_md", "value": "补丁正文"}],
        holder="integrate-1",
    )
    assert updated.revision == 1
    assert updated.body_md == "补丁正文"
    assert updated.storage == "thin"
    assert any(item["kind"] == "apply_op" for item in store.events("old"))
    store.release_lease("old", "integrate-1")
    store.release_lease("draft", "integrate-1")


def test_second_holder_cannot_steal_lease(tmp_path, unlock_writes):
    store = CrystalStore(_con(tmp_path))
    store.put(ThinCrystal(id="c1", type="人物", name="甲", status="active", revision=0))
    now = datetime.now(timezone.utc)
    store.acquire_lease("c1", "worker-a", now=now)
    with pytest.raises(LeaseConflict):
        store.acquire_lease("c1", "worker-b", now=now)
    kinds = [item["kind"] for item in store.events("c1")]
    assert "lease_conflict" in kinds
    store.acquire_lease("c1", "worker-b", now=now + timedelta(minutes=6))
    store.release_lease("c1", "worker-b")


@pytest.mark.ws_flip
def test_ws_flip_release_orphaned_clears_crystal_leases(tmp_path, unlock_writes):
    store = CrystalStore(_con(tmp_path))
    store.put(ThinCrystal(id="c1", type="人物", name="甲", status="active", revision=0))
    store.acquire_lease("c1", "worker-stale")
    assert store._has_lease("c1")
    assert store.release_orphaned() == 1
    assert store._has_lease("c1") is False
    row = store.con.execute(
        "SELECT lease_holder, lease_until FROM crystallizations WHERE id='c1'"
    ).fetchone()
    assert row["lease_holder"] is None
    assert row["lease_until"] is None


def test_transition_machine_and_illegal_status(tmp_path, unlock_writes):
    store = CrystalStore(_con(tmp_path))
    store.put(ThinCrystal(id="d1", type="人物", name="甲", status="draft", revision=0))
    deferred = store.transition("d1", "deferred", extra={"maybe_with": "old-1"})
    assert deferred.status == "deferred"
    assert deferred.maybe_with == "old-1"
    assert any(item["kind"] == "defer" for item in store.events("d1"))
    active = store.transition("d1", "active", expected_revision=deferred.revision)
    assert active.status == "active"
    with pytest.raises(InvalidTransition):
        store.transition("d1", "draft")
    with pytest.raises(InvalidTransition):
        store.transition("d1", "pending_confirm")
    rejected = store.put(
        ThinCrystal(id="d2", type="人物", name="乙", status="rejected", revision=0)
    )
    with pytest.raises(InvalidTransition):
        store.transition("d2", "active", expected_revision=rejected.revision)


def test_same_merge_keeps_old_active(tmp_path, unlock_writes):
    store = CrystalStore(_con(tmp_path))
    store.put(ThinCrystal(id="old", type="人物", name="甲", status="active", revision=0))
    store.put(ThinCrystal(id="draft", type="人物", name="甲", status="draft", revision=0))
    store.transition("draft", "merged", extra={"merged_into": "old"})
    assert store.get("old").status == "active"
    assert store.get("draft").status == "merged"
    assert store.get("draft").merged_into == "old"


def test_migrate_adds_thin_columns_to_pre_2_0_table(tmp_path):
    con = sqlite3.connect(tmp_path / "old.sqlite")
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE crystallizations (
             id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
             status TEXT NOT NULL, summary_json TEXT NOT NULL,
             chapters_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0
           )"""
    )
    con.execute(
        "INSERT INTO crystallizations VALUES('p-old','人物','甲','active','{}',?,0)",
        (json.dumps({"事业经历": "旧库厚章节。"}, ensure_ascii=False),),
    )
    con.commit()
    migrate_crystal_store(con)
    cols = {row[1] for row in con.execute("PRAGMA table_info(crystallizations)")}
    assert {"envelope_json", "body_md", "facts_json", "lease_until"} <= cols
    row = con.execute("SELECT chapters_json, envelope_json FROM crystallizations WHERE id='p-old'").fetchone()
    assert "旧库厚章节" in row["chapters_json"]
    assert row["envelope_json"] is None
    got = CrystalStore(con).get("p-old")
    assert got.storage == "thin"
    assert "旧库厚章节" not in (got.body_md or "")
    assert "旧库厚章节" in row["chapters_json"]


def test_engine_connect_migrates_crystal_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.delenv("ROGMEM_LLM_BASE", raising=False)
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    cols = {row[1] for row in engine.db().execute("PRAGMA table_info(crystallizations)")}
    assert "envelope_json" in cols
    assert "body_md" in cols
    event = engine.db().execute(
        "SELECT kind, payload_json FROM crystal_events WHERE crystal_id='owner'"
    ).fetchone()
    assert event is not None
    assert event["kind"] == "promote_new"
    owner = CrystalStore(engine.db()).get("owner")
    assert owner is not None
    assert owner.storage == "thin"
    assert owner.keys == {}
    assert json.loads(
        engine.db().execute("SELECT summary_json FROM crystallizations WHERE id='owner'").fetchone()["summary_json"]
    ) == {}


def test_production_distiller_write_is_on():
    assert DISTILLER_STORE_WRITE_ENABLED is True
    assert_distiller_write_not_flipped()


def test_kill_switch_still_raises_when_off(write_switch_off):
    with pytest.raises(IntegrateWriteNotLive):
        assert_distiller_write_not_flipped()


def test_project_thick_helper_is_deprecated_fail_closed():
    row = {
        "id": "x",
        "type": "人物",
        "name": "甲",
        "status": "active",
        "revision": 2,
        "summary_json": json.dumps({"概述": "尚无来源", "别名": ""}, ensure_ascii=False),
        "chapters_json": json.dumps({"事业经历": "暂无来源", "代表作品": "写过一篇。"}, ensure_ascii=False),
    }
    with pytest.raises(ThickReadDeprecated):
        project_thick(row)
