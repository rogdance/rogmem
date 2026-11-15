"""v5.4 P1-3 槽位史链 + P1-4 source_id→session。

行为证据：crystal_detail.chains、resolve_source_origin、admin HTML。
禁止 inspect.getsource / 源码 sniff 冒充绿。
prompts 零 diff；≠可上线。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from rogmem.crystal_store import CrystalStore, ThinCrystal
from rogmem.slot_chain import style_delta
from rogmem import ui

pytestmark = pytest.mark.v54_slot_chain_fail_gate

MEAT_QUOTE = "我这段时间爱吃红烧肉。"
QUIT_QUOTE = "我现在戒肉吃素了。"
EVENT0 = "2026-03-01T00:00:00+00:00"
EVENT1 = "2026-04-01T00:00:00+00:00"

CORE_BODY = """## 身份速写

云岚核忆主人，现居南港。

## 沟通与决策风格

结论先行，少步骤。

## 约定与待办

以后回答都先给结论。

## 兴趣与偏好

爱吃红烧肉。

## 当前关注与计划

最近在搞云岚核忆。

## 教育背景

南港书院。
"""


def _engine(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _insert_source(
    engine,
    *,
    sid: str,
    kind: str,
    body: str,
    event_at: str = EVENT0,
    session_id: str = "",
    turn_id: str = "",
):
    engine.db().execute(
        """INSERT INTO sources(
             id,kind,session_id,turn_id,event_at,ingested_at,content_hash,body,status,lane,pair_ready
           ) VALUES(?,?,?,?,?,?,?,?,'已蒸馏','fast',1)""",
        (sid, kind, session_id or None, turn_id or None, event_at, event_at, f"hash-{sid}", body),
    )
    engine.db().commit()
    return engine.db().execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()


def _set_owner_body(engine, body_md: str) -> None:
    store = CrystalStore(engine.db())
    owner = store.get("owner")
    assert owner is not None
    store.put(
        ThinCrystal(
            id="owner",
            type=owner.type,
            name=owner.name,
            status=owner.status,
            revision=owner.revision,
            summary=owner.summary,
            aliases=list(owner.aliases),
            owner_relation=owner.owner_relation,
            body_md=body_md,
            facts=list(owner.facts),
            keys=dict(owner.keys),
        ),
        holder="test:v54-chain",
    )


def _apply_interest_op(engine, source, *, value: str, quote: str):
    return engine.apply_integrate(
        {
            "identity": "same",
            "matched_id": "owner",
            "ops": [
                {
                    "op": "update",
                    "path": "body_md##兴趣与偏好",
                    "value": value,
                    "fact": {"claim": value, "evidence": quote, "anchor": "兴趣与偏好"},
                }
            ],
            "conflicts": [],
            "facts": [{"claim": value, "evidence": quote, "anchor": "兴趣与偏好"}],
        },
        candidates=["owner"],
        corpus=quote,
        store=CrystalStore(engine.db()),
        crystal_id="owner",
        holder=f"distill:{source['id']}",
        draft_crystal={
            "type": "人物",
            "name": "记忆主人",
            "summary": "云岚核忆主人。",
            "body_md": CORE_BODY.replace("爱吃红烧肉。", value),
            "facts": [{"claim": value, "evidence": quote, "anchor": "兴趣与偏好"}],
        },
    )


def test_different_headings_do_not_retire_each_other(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    con = engine.db()
    engine._add_statement(con, "owner", None, "沟通风格", "先给结论", "先给结论。", "s-talk", "user", "written", EVENT0)
    engine._add_statement(con, "owner", None, "决策风格", "先看数据", "先看数据。", "s-decide", "user", "written", EVENT0)
    engine._add_statement(con, "owner", None, "决策风格", "先听反对意见", "先听反对意见。", "s-decide-2", "user", "written", EVENT0)
    con.commit()
    rows = list(
        con.execute(
            "SELECT chapter, text, invalid_at IS NULL AS live FROM statements WHERE crystal_id='owner' ORDER BY rowid"
        )
    )
    assert [(row["chapter"], row["text"], row["live"]) for row in rows] == [
        ("沟通风格", "先给结论", 1),
        ("决策风格", "先看数据", 0),
        ("决策风格", "先听反对意见", 1),
    ]


def test_style_delta_keeps_existing_wording():
    assert style_delta("", "先看数据") == "新增"
    assert style_delta("先看数据", "先看数据再听反对意见") == "新增了再听反对意见"
    assert style_delta("爱吃红烧肉", "戒肉吃素") == "由「爱吃红烧肉」改为「戒肉吃素」"


def test_V54_CHAIN_01_meat_to_veg_is_queryable(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _set_owner_body(engine, CORE_BODY)
    first = _insert_source(
        engine,
        sid="int-meat",
        kind="user",
        body=MEAT_QUOTE,
        event_at=EVENT0,
        session_id="sess-diet",
        turn_id="turn-meat",
    )
    assert _apply_interest_op(engine, first, value="爱吃红烧肉", quote=MEAT_QUOTE).decision == "accept"
    second = _insert_source(
        engine,
        sid="int-quit",
        kind="user",
        body=QUIT_QUOTE,
        event_at=EVENT1,
        session_id="sess-diet",
        turn_id="turn-quit",
    )
    assert _apply_interest_op(engine, second, value="戒肉吃素", quote=QUIT_QUOTE).decision == "accept"

    rings = engine.read_chain("记忆主人", "兴趣爱好")["rings"] or engine.read_chain("记忆主人", "兴趣与偏好")["rings"]
    assert [item["text"] for item in rings] == ["爱吃红烧肉", "戒肉吃素"]
    assert rings[0]["current"] is False
    assert rings[1]["current"] is True
    assert rings[1]["delta"] == "由「爱吃红烧肉」改为「戒肉吃素」"
    assert rings[0]["source_id"] == "int-meat"
    assert rings[1]["source_id"] == "int-quit"
    assert rings[0]["session_id"] == "sess-diet"
    assert rings[1]["turn_id"] == "turn-quit"
    old = engine.db().execute(
        "SELECT invalid_at FROM statements WHERE source_id='int-meat'"
    ).fetchone()
    assert old["invalid_at"]


def test_V54_CHAIN_02_style_chain_does_not_regress(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    first = _insert_source(
        engine,
        sid="style-a",
        kind="user",
        body="我做决定先看数据。",
        event_at="2026-10-24T04:30:00+00:00",
        session_id="style-sess",
        turn_id="d1",
    )
    engine._write_field(
        first,
        {"field": "决策风格", "value": "先看数据", "quote": "我做决定先看数据。", "crystal": "记忆主人", "trust": "written"},
        "user",
        first["body"],
    )
    second = _insert_source(
        engine,
        sid="style-c",
        kind="user",
        body="我做决定先看数据，再听反对意见。",
        event_at="2026-10-24T04:50:00+00:00",
        session_id="style-sess",
        turn_id="d3",
    )
    engine._write_field(
        second,
        {
            "field": "决策风格",
            "value": "先看数据再听反对意见",
            "quote": "我做决定先看数据，再听反对意见。",
            "crystal": "记忆主人",
            "trust": "written",
        },
        "user",
        second["body"],
    )
    rings = engine.read_chain("记忆主人", "决策风格")["rings"]
    assert [item["text"] for item in rings] == ["先看数据", "先看数据再听反对意见"]
    assert rings[0]["delta"] == "新增"
    assert rings[1]["delta"] == "新增了再听反对意见"
    assert rings[1]["session_id"] == "style-sess"


def test_V54_CHAIN_03_focus_supersede_keeps_history(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    first = _insert_source(
        engine,
        sid="focus-a",
        kind="user",
        body="最近在搞云岚核忆。",
        event_at=EVENT0,
        session_id="sess-focus",
        turn_id="turn-a",
    )
    engine._write_field(
        first,
        {"field": "当前关注", "value": "最近在搞云岚核忆", "quote": "最近在搞云岚核忆。", "crystal": "记忆主人", "trust": "written"},
        "user",
        "最近在搞云岚核忆。",
    )
    later = datetime(2026, 4, 5, tzinfo=timezone.utc).isoformat()
    second = _insert_source(
        engine,
        sid="focus-b",
        kind="user",
        body="改做南港书院讲义了。",
        event_at=later,
        session_id="sess-focus",
        turn_id="turn-b",
    )
    engine._write_field(
        second,
        {"field": "当前关注", "value": "改做南港书院讲义", "quote": "改做南港书院讲义了。", "crystal": "记忆主人", "trust": "written"},
        "user",
        "改做南港书院讲义了。",
    )
    rings = engine.read_chain("记忆主人", "当前关注")["rings"]
    assert [item["text"] for item in rings] == ["最近在搞云岚核忆", "改做南港书院讲义"]
    assert rings[0]["current"] is False
    assert rings[1]["current"] is True
    assert rings[1]["delta"] == "由「最近在搞云岚核忆」改为「改做南港书院讲义」"
    assert "chains" not in engine.crystal_detail("owner")


def test_V54_CHAIN_04_agreement_is_a_heading(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    source = _insert_source(engine, sid="agree-1", kind="user", body="以后回答都先给结论。")
    engine._write_field(
        source,
        {"field": "约定待办", "value": "以后回答都先给结论", "quote": "以后回答都先给结论。", "crystal": "记忆主人", "trust": "written"},
        "user",
        "以后回答都先给结论。",
    )
    assert "chains" not in engine.crystal_detail("owner")
    rings = engine.read_chain("记忆主人", "约定待办")["rings"]
    assert [item["text"] for item in rings] == ["以后回答都先给结论"]


def test_V54_TRACE_01_source_resolves_to_session(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _insert_source(
        engine,
        sid="src-sess",
        kind="user",
        body="有会话。",
        session_id="sess-open",
        turn_id="turn-1",
    )
    origin = engine.resolve_source_origin("src-sess")
    assert origin["ok"] is True
    assert origin["session_id"] == "sess-open"
    assert origin["turn_id"] == "turn-1"
    assert "session=sess-open" in origin["session_href"]
    assert "turn=turn-1" in origin["session_href"]
    snap = engine.source_snapshot("src-sess")
    assert snap["origin"]["session_id"] == "sess-open"
    listed = engine.list_sources("session", session="sess-open")
    assert [row["id"] for row in listed["rows"]] == ["src-sess"]


def test_V54_TRACE_02_missing_session_is_fail_soft(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    missing = engine.resolve_source_origin("no-such-source")
    assert missing["ok"] is False
    assert missing["session_href"] == ""
    assert engine.resolve_source_origin("")["ok"] is False
    assert engine.source_snapshot("no-such-source") is None
    _insert_source(engine, sid="src-bare", kind="user", body="没有会话编号。")
    bare = engine.resolve_source_origin("src-bare")
    assert bare["ok"] is True
    assert bare["session_id"] == ""
    assert bare["session_href"] == ""
    assert bare["source_href"]
    detail = engine.source_detail("src-bare")
    html = ui.source_detail(detail["source"], detail["statements"], detail["mate"])
    assert "500" not in html
    assert "src-bare" in html


def test_V54_TRACE_03_evidence_click_reaches_session(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _set_owner_body(engine, CORE_BODY)
    source = _insert_source(
        engine,
        sid="cite-src",
        kind="user",
        body=MEAT_QUOTE,
        session_id="sess-cite",
        turn_id="turn-cite",
    )
    assert _apply_interest_op(engine, source, value="爱吃红烧肉", quote=MEAT_QUOTE).decision == "accept"
    detail = engine.crystal_detail("owner")
    rings = engine.read_chain("记忆主人", "兴趣爱好")["rings"] or engine.read_chain("记忆主人", "兴趣与偏好")["rings"]
    assert rings
    assert "sess-cite" in (rings[0]["session_href"] or "")
    marks = detail["field_marks"].get("兴趣爱好") or detail["chapter_marks"].get("兴趣与偏好") or []
    if not marks:
        marks = rings
    cite = ui._cite_marks(
        [
            {
                "n": 1,
                "id": rings[0]["id"],
                "source_id": rings[0]["source_id"],
                "session_href": rings[0]["session_href"],
                "source_href": rings[0]["source_href"],
                "quote": MEAT_QUOTE,
            }
        ]
    )
    assert "session=sess-cite" in cite
    assert "turn=turn-cite" in cite
    page = ui.source_detail(
        {**dict(source), "_origin": engine.resolve_source_origin(source["id"])},
        [],
        None,
    )
    assert "sess-cite" in page
    assert "session=sess-cite" in page


def test_V54_INT_02_still_keeps_old_statement(tmp_path, monkeypatch):
    """Chain query must not change Integrate: old row stays with invalid_at."""
    engine = _engine(tmp_path, monkeypatch)
    _set_owner_body(engine, CORE_BODY)
    first = _insert_source(engine, sid="int-meat", kind="user", body=MEAT_QUOTE, event_at=EVENT0)
    assert _apply_interest_op(engine, first, value="爱吃红烧肉", quote=MEAT_QUOTE).decision == "accept"
    second = _insert_source(engine, sid="int-quit", kind="user", body=QUIT_QUOTE, event_at=EVENT1)
    assert _apply_interest_op(engine, second, value="戒肉吃素", quote=QUIT_QUOTE).decision == "accept"
    rows = list(
        engine.db().execute(
            """SELECT quote, invalid_at FROM statements
               WHERE crystal_id='owner'
                 AND (COALESCE(field,'') IN ('兴趣爱好','兴趣与偏好')
                      OR COALESCE(chapter,'') IN ('兴趣爱好','兴趣与偏好'))
               ORDER BY COALESCE(event_at, created_at), created_at"""
        )
    )
    assert [row["quote"] for row in rows] == [MEAT_QUOTE, QUIT_QUOTE]
    assert rows[0]["invalid_at"]
    assert rows[1]["invalid_at"] is None
