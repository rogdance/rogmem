"""N Draft→Integrate loop: one Draft, each mid-稿 gets Integrate + land.

Single mid-稿 degenerates to #79. Per-draft land failure is fail-closed.
docs/prompts 零 diff. 路由只看 payload_kind。integrate-new 禁 _open_crystal。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from rogmem import engine as distill_engine
from rogmem.crystal_store import DISTILLER_STORE_WRITE_ENABLED, CrystalStore, hydrate
from rogmem.distill_gate import PAYLOAD_KIND_INTEGRATE

CORPUS = "云岚测试员甲于 2019 年在北窗实验室任职。后来到南港书院做讲师。"
YUNLAN_DRAFT = {
    "name": "云岚甲",
    "type": "人物",
    "aliases": ["测试员甲"],
    "summary": "在北窗实验室任职。",
    "owner_relation": "同事",
    "body_md": "## 事业经历\n\n云岚测试员甲于 2019 年在北窗实验室任职。",
    "facts": [
        {
            "claim": "2019 年在北窗实验室任职",
            "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
        }
    ],
}
SCHOOL_DRAFT = {
    "name": "南港书院",
    "type": "组织",
    "aliases": [],
    "summary": "云岚甲后来任教的书院。",
    "owner_relation": "任职机构",
    "body_md": "## 发展沿革\n\n后来到南港书院做讲师。",
    "facts": [
        {
            "claim": "后来到南港书院做讲师",
            "evidence": "后来到南港书院做讲师。",
        }
    ],
}
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
}
NAMELESS_DRAFT = {
    "type": "人物",
    "aliases": [],
    "facts": [
        {
            "claim": "2019 年在北窗实验室任职",
            "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
        }
    ],
}
EMPTY_FACTS = {
    "name": "空事实丁",
    "type": "人物",
    "aliases": [],
    "facts": [],
}
SAME_YUNLAN = {
    "identity": "same",
    "matched_id": "c-yunlan",
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
SAME_SCHOOL = {
    "identity": "same",
    "matched_id": "c-school",
    "ops": [
        {
            "op": "update",
            "path": "body_md",
            "value": CORPUS,
            "fact": {
                "claim": "南港任教",
                "evidence": "后来到南港书院做讲师。",
            },
        }
    ],
    "conflicts": [],
}
NEW_ACCEPT = {"identity": "new", "ops": [], "conflicts": []}
ILLEGAL_NEW = {"identity": "new", "ops": [{"op": "add", "path": "facts"}], "conflicts": []}


def _engine(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _queue_user(engine, source_id: str, body: str = CORPUS) -> None:
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?, 'user', 'qa', 'qa', ?, ?, ?, ?, 'fast', 1)""",
        (source_id, engine.utcnow(), source_id + "-h", body, engine.ST_VEC_READY),
    )
    con.execute("INSERT INTO queue(source_id,lane,state) VALUES(?, 'fast', 'queued')", (source_id,))
    con.commit()


def _queue_row(engine, source_id: str):
    return engine.db().execute(
        "SELECT state, last_error FROM queue WHERE source_id=?", (source_id,)
    ).fetchone()


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


def _seed_named(engine, cid: str, name: str, ctype: str = "人物") -> None:
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json)
           VALUES(?,?,?,'active',?,?)""",
        (cid, ctype, name, json.dumps({"名称": name}, ensure_ascii=False), "{}"),
    )
    engine.db().commit()


def _draft_bundle(*crystals):
    return {"crystals": list(crystals)}


def test_two_mid_drafts_each_get_integrate_and_land(tmp_path, monkeypatch):
    """同一条材料 ≥2 条非空 facts 中稿：各走一次 Integrate，并各有落库结果。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    _seed_named(engine, "c-school", "南港书院", "组织")
    before = _active_rows(engine)
    integrate_calls: list[dict] = []
    apply_calls: list[dict] = []
    draft_calls: list[int] = []
    orig_apply = engine.apply_integrate

    def spy_draft(*_a, **_k):
        draft_calls.append(1)
        return _draft_bundle(YUNLAN_DRAFT, SCHOOL_DRAFT)

    def spy_integrate(_text, _kind="", **kw):
        integrate_calls.append(kw)
        name = (kw.get("draft_crystal") or {}).get("name")
        if name == "云岚甲":
            return dict(SAME_YUNLAN)
        if name == "南港书院":
            return dict(SAME_SCHOOL)
        raise AssertionError(f"unexpected draft {name}")

    def spy_apply(*args, **kwargs):
        apply_calls.append({"args": args, "kwargs": kwargs})
        return orig_apply(*args, **kwargs)

    monkeypatch.setattr(engine, "_llm_draft", spy_draft)
    monkeypatch.setattr(engine, "_llm_integrate", spy_integrate)
    monkeypatch.setattr(engine, "apply_integrate", spy_apply)
    monkeypatch.setattr(engine, "_llm_extract", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("extract")))
    _queue_user(engine, "src-n-loop")
    assert engine.pump_one() == "src-n-loop"
    assert _queue_row(engine, "src-n-loop")["state"] == "done"
    assert draft_calls == [1]
    integrate_names = [call["draft_crystal"]["name"] for call in integrate_calls]
    assert integrate_names.count("云岚甲") >= 1
    assert integrate_names.count("南港书院") >= 1
    first_by_name = {}
    for call in integrate_calls:
        name = call["draft_crystal"]["name"]
        first_by_name.setdefault(name, [item["id"] for item in call["library_candidates"]])
    assert set(first_by_name["云岚甲"]) >= {"c-yunlan", "c-school"}
    assert set(first_by_name["南港书院"]) >= {"c-yunlan", "c-school"}
    assert len(apply_calls) == 2
    assert apply_calls[0]["kwargs"].get("draft_crystal")["name"] == "云岚甲"
    assert apply_calls[1]["kwargs"].get("draft_crystal")["name"] == "南港书院"
    qing = engine.db().execute("SELECT body_md, revision FROM crystallizations WHERE id='c-yunlan'").fetchone()
    school = engine.db().execute("SELECT body_md, revision FROM crystallizations WHERE id='c-school'").fetchone()
    assert CORPUS in (qing["body_md"] or "")
    assert CORPUS in (school["body_md"] or "")
    assert int(qing["revision"] or 0) > 0
    assert int(school["revision"] or 0) > 0
    assert _ids(_active_rows(engine)) == _ids(before)


def test_two_mid_drafts_same_plus_new_land(tmp_path, monkeypatch):
    """两条中稿可以分别 same 改旧条、new 薄建，互不吞掉。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    before = _active_rows(engine)

    def spy_integrate(_text, _kind="", **kw):
        name = (kw.get("draft_crystal") or {}).get("name")
        if name == "云岚甲":
            return dict(SAME_YUNLAN)
        return dict(NEW_ACCEPT)

    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: _draft_bundle(YUNLAN_DRAFT, NEW_DRAFT))
    monkeypatch.setattr(engine, "_llm_integrate", spy_integrate)
    monkeypatch.setattr(engine, "_llm_extract", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("extract")))
    _queue_user(engine, "src-same-new")
    assert engine.pump_one() == "src-same-new"
    assert _queue_row(engine, "src-same-new")["state"] == "done"
    created = [row for row in _active_rows(engine) if row["id"] not in _ids(before)]
    assert len(created) == 1
    assert created[0]["name"] == "北窗讲师丙"
    assert created[0]["status"] == "active"
    assert json.loads(created[0]["summary_json"] or "{}") == {}
    assert json.loads(created[0]["chapters_json"] or "{}") == {}
    thin = hydrate(created[0])
    assert thin.storage == "thin"
    assert thin.summary == "在北窗实验室做讲师。"
    qing = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='c-yunlan'").fetchone()
    assert CORPUS in (qing["body_md"] or "")


def test_empty_facts_stub_does_not_enter_integrate(tmp_path, monkeypatch):
    """空 facts 不是中稿；夹在两条真中稿里也不得单独进 Integrate。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    _seed_named(engine, "c-school", "南港书院", "组织")
    names: list[str] = []

    def spy_integrate(_text, _kind="", **kw):
        name = (kw.get("draft_crystal") or {}).get("name")
        names.append(name)
        if name == "云岚甲":
            return dict(SAME_YUNLAN)
        return dict(SAME_SCHOOL)

    monkeypatch.setattr(
        engine,
        "_llm_draft",
        lambda *_a, **_k: _draft_bundle(YUNLAN_DRAFT, EMPTY_FACTS, SCHOOL_DRAFT),
    )
    monkeypatch.setattr(engine, "_llm_integrate", spy_integrate)
    _queue_user(engine, "src-skip-empty")
    assert engine.pump_one() == "src-skip-empty"
    assert "云岚甲" in names
    assert "南港书院" in names
    assert "空事实丁" not in names


def test_single_mid_draft_degenerates_to_hash79(tmp_path, monkeypatch):
    """单条中稿：produce 仍是单 payload，无 batch 包装，路由一次 apply_integrate。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    apply_calls: list = []
    orig_apply = engine.apply_integrate

    def spy_apply(*args, **kwargs):
        apply_calls.append((args, kwargs))
        return orig_apply(*args, **kwargs)

    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: _draft_bundle(YUNLAN_DRAFT))
    monkeypatch.setattr(engine, "_llm_integrate", lambda *_a, **_k: dict(SAME_YUNLAN))
    monkeypatch.setattr(engine, "apply_integrate", spy_apply)
    data = engine._produce_distill_payload(CORPUS, "user", **engine._match_integrate_parts(CORPUS))
    assert data["payload_kind"] == PAYLOAD_KIND_INTEGRATE
    assert "_integrate_batch" not in data
    assert data.get("_draft_crystal") == YUNLAN_DRAFT
    assert data["identity"] == "same"
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?, 'user', 'qa', 'qa', ?, ?, ?, ?, 'fast', 1)""",
        ("src-single-eq79", engine.utcnow(), "src-single-eq79-h", CORPUS, engine.ST_VEC_READY),
    )
    con.commit()
    source = con.execute("SELECT * FROM sources WHERE id='src-single-eq79'").fetchone()
    engine._raise_if_new_land_unwritten(engine._route_distill_payload(source, data, CORPUS))
    assert len(apply_calls) == 1
    assert apply_calls[0][1].get("draft_crystal") == YUNLAN_DRAFT
    qing = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='c-yunlan'").fetchone()
    assert CORPUS in (qing["body_md"] or "")


def test_one_integrate_none_fail_closes_does_not_skip(tmp_path, monkeypatch):
    """NL-FAIL-03: ≥2 中一条 Integrate 返回 None，不得静默丢 mid，也不得假 done。

    先写下的中稿一并撤回；整源仍非 done。不掉 extract。
    """
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    _seed_named(engine, "c-school", "南港书院", "组织")
    integrate_names: list[str] = []
    extract_calls: list[int] = []
    apply_names: list[str] = []
    orig_apply = engine.apply_integrate

    def spy_integrate(_text, _kind="", **kw):
        name = (kw.get("draft_crystal") or {}).get("name")
        integrate_names.append(name)
        if name == "云岚甲":
            return dict(SAME_YUNLAN)
        return None

    def spy_apply(*args, **kwargs):
        draft = kwargs.get("draft_crystal") or {}
        apply_names.append(str(draft.get("name") or ""))
        return orig_apply(*args, **kwargs)

    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: _draft_bundle(YUNLAN_DRAFT, SCHOOL_DRAFT))
    monkeypatch.setattr(engine, "_llm_integrate", spy_integrate)
    monkeypatch.setattr(engine, "apply_integrate", spy_apply)
    monkeypatch.setattr(engine, "_llm_extract", lambda *_a, **_k: extract_calls.append(1) or {"payload_kind": "extract", "items": []})
    _queue_user(engine, "src-one-none")
    assert engine.pump_one() == "src-one-none"
    queued = _queue_row(engine, "src-one-none")
    assert queued["state"] != "done"
    assert queued["last_error"]
    assert "云岚甲" in integrate_names
    assert "南港书院" in integrate_names
    assert apply_names == ["云岚甲"]
    assert extract_calls == []
    qing = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='c-yunlan'").fetchone()
    school = engine.db().execute("SELECT body_md, revision FROM crystallizations WHERE id='c-school'").fetchone()
    assert CORPUS not in (qing["body_md"] or "")
    assert CORPUS not in (school["body_md"] or "")
    assert int(school["revision"] or 0) == 0


def test_all_integrate_none_does_not_extract(tmp_path, monkeypatch):
    """NL-FAIL-05: 全部中稿 Integrate 返回 None，不得掉 extract 假消化，也不得假 done。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    _seed_named(engine, "c-school", "南港书院", "组织")
    before = _active_rows(engine)
    integrate_names: list[str] = []
    extract_calls: list[int] = []
    apply_calls: list = []

    def spy_integrate(_text, _kind="", **kw):
        integrate_names.append((kw.get("draft_crystal") or {}).get("name"))
        return None

    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: _draft_bundle(YUNLAN_DRAFT, SCHOOL_DRAFT))
    monkeypatch.setattr(engine, "_llm_integrate", spy_integrate)
    monkeypatch.setattr(engine, "apply_integrate", lambda *_a, **_k: apply_calls.append(1))
    monkeypatch.setattr(engine, "_llm_extract", lambda *_a, **_k: extract_calls.append(1) or {"payload_kind": "extract", "items": []})
    _queue_user(engine, "src-all-none")
    assert engine.pump_one() == "src-all-none"
    queued = _queue_row(engine, "src-all-none")
    assert queued["state"] != "done"
    assert queued["last_error"]
    assert integrate_names == ["云岚甲", "南港书院"]
    assert apply_calls == []
    assert extract_calls == []
    assert _ids(_active_rows(engine)) == _ids(before)
    qing = engine.db().execute("SELECT body_md, revision FROM crystallizations WHERE id='c-yunlan'").fetchone()
    school = engine.db().execute("SELECT body_md, revision FROM crystallizations WHERE id='c-school'").fetchone()
    assert CORPUS not in (qing["body_md"] or "")
    assert CORPUS not in (school["body_md"] or "")
    assert int(qing["revision"] or 0) == 0
    assert int(school["revision"] or 0) == 0


def test_batch_gate_reject_blocks_done_not_only_new_land(tmp_path, monkeypatch):
    """NL-FAIL-04: batch 内普通 rejected（非 new_land_*）也必须挡住泵假 done。

    先写下的中稿一并撤回；整源仍非 done。
    """
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    before = _active_rows(engine)
    integrate_names: list[str] = []
    apply_names: list[str] = []
    orig_apply = engine.apply_integrate

    def spy_integrate(_text, _kind="", **kw):
        name = (kw.get("draft_crystal") or {}).get("name")
        integrate_names.append(name)
        if name == "云岚甲":
            return dict(SAME_YUNLAN)
        return dict(ILLEGAL_NEW)

    def spy_apply(*args, **kwargs):
        draft = kwargs.get("draft_crystal") or {}
        apply_names.append(str(draft.get("name") or ""))
        return orig_apply(*args, **kwargs)

    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: _draft_bundle(YUNLAN_DRAFT, NEW_DRAFT))
    monkeypatch.setattr(engine, "_llm_integrate", spy_integrate)
    monkeypatch.setattr(engine, "apply_integrate", spy_apply)
    monkeypatch.setattr(engine, "_llm_extract", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("extract")))
    _queue_user(engine, "src-gate-reject")
    assert engine.pump_one() == "src-gate-reject"
    queued = _queue_row(engine, "src-gate-reject")
    assert queued["state"] != "done"
    assert queued["last_error"]
    assert integrate_names == ["云岚甲", "北窗讲师丙"]
    assert apply_names == ["云岚甲", "北窗讲师丙"]
    qing = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='c-yunlan'").fetchone()
    assert CORPUS not in (qing["body_md"] or "")
    created = [row for row in _active_rows(engine) if row["id"] not in _ids(before)]
    assert created == []
    assert all(row["name"] != "北窗讲师丙" for row in _active_rows(engine))


def test_any_land_failure_fail_closes_source(tmp_path, monkeypatch):
    """任一条中稿落地失败：不假 accept/done；已写下的中稿一并撤回。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    engine = _engine(tmp_path, monkeypatch)
    _seed_named(engine, "c-yunlan", "云岚甲")
    before = _active_rows(engine)
    integrate_names: list[str] = []
    apply_names: list[str] = []
    orig_apply = engine.apply_integrate

    def spy_integrate(_text, _kind="", **kw):
        name = (kw.get("draft_crystal") or {}).get("name") or ""
        integrate_names.append(name or "<nameless>")
        if name == "云岚甲":
            return dict(SAME_YUNLAN)
        return dict(NEW_ACCEPT)

    def spy_apply(*args, **kwargs):
        draft = kwargs.get("draft_crystal") or {}
        apply_names.append(str(draft.get("name") or "<nameless>"))
        return orig_apply(*args, **kwargs)

    monkeypatch.setattr(engine, "_llm_draft", lambda *_a, **_k: _draft_bundle(YUNLAN_DRAFT, NAMELESS_DRAFT))
    monkeypatch.setattr(engine, "_llm_integrate", spy_integrate)
    monkeypatch.setattr(engine, "apply_integrate", spy_apply)
    _queue_user(engine, "src-fail-closed")
    assert engine.pump_one() == "src-fail-closed"
    queued = _queue_row(engine, "src-fail-closed")
    assert queued["state"] != "done"
    assert queued["last_error"]
    assert integrate_names == ["云岚甲", "<nameless>"]
    assert apply_names == ["云岚甲", "<nameless>"]
    qing = engine.db().execute("SELECT body_md FROM crystallizations WHERE id='c-yunlan'").fetchone()
    assert CORPUS not in (qing["body_md"] or "")
    created = [row for row in _active_rows(engine) if row["id"] not in _ids(before)]
    assert created == []
    assert all(row["name"] != "北窗讲师丙" for row in _active_rows(engine))


def test_n_loop_hard_gates_and_prompts_zero_diff():
    """路由只看 payload_kind；new 不走 _open_crystal；本刀不改 prompt 文案。"""
    from rogmem.distill_resim import DRAFT_PROMPT_PATH, INTEGRATE_PROMPT_PATH

    assert DISTILLER_STORE_WRITE_ENABLED is True
    assert DRAFT_PROMPT_PATH.is_file()
    assert INTEGRATE_PROMPT_PATH.is_file()
    with_src = inspect.getsource(distill_engine._with_approved_draft)
    produce_src = inspect.getsource(distill_engine._produce_distill_payload)
    route_src = inspect.getsource(distill_engine._route_distill_payload)
    enter_src = inspect.getsource(distill_engine._enter_integrate)
    apply_src = inspect.getsource(distill_engine.apply_integrate)
    raise_src = inspect.getsource(distill_engine._raise_if_new_land_unwritten)
    kind_src = inspect.getsource(distill_engine._declared_payload_kind)
    assert "list_draft_crystals" in with_src
    assert "pick_matched_draft_crystal" not in with_src
    assert "draft_crystals" in with_src
    assert "_llm_integrate(" in produce_src
    assert "_llm_draft(" not in produce_src
    assert "payload_kind" in route_src
    assert "result.identity" not in route_src
    assert "identity ==" not in route_src
    assert '["identity"]' not in route_src
    assert "identity" not in kind_src
    assert "identity" not in enter_src
    assert "_open_crystal(" not in apply_src
    assert "_open_crystal(" not in inspect.getsource(distill_engine._land_integrate_new)
    assert apply_src.count("_apply_then_sidecar(") == 1
    assert "summary_json" not in apply_src
    assert "chapters_json" not in apply_src
    assert "list" in raise_src or "tuple" in raise_src
    assert '!= "accept"' in raise_src
    assert "integrate_call_failed" in raise_src
    assert "had_mids" in produce_src
    assert "_unwritten_integrate_payload(" in produce_src
    assert "_fail_integrate_call(" in enter_src
    draft_text = DRAFT_PROMPT_PATH.read_text(encoding="utf-8")
    integrate_text = INTEGRATE_PROMPT_PATH.read_text(encoding="utf-8")
    assert "你是记忆库的萃取写作者" in draft_text
    assert "你是记忆库的**整合裁决者**" in integrate_text
