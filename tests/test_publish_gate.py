"""Coverage invariant: no synthetic 无, chunk-level cited, distilled vs complete."""

from __future__ import annotations

import json
import threading

import pytest

from rogmem.publish_gate import (
    DISTILLED,
    assert_coverage,
    coverage_state_for_source,
    evidence_chapters,
    forbid_synthetic_none,
    mark_chunks_cited,
    residual_summary_required,
    set_source_distill_state,
    unit_chunks,
)

def test_month_ranges_still_count_as_career():
    from rogmem.publish_gate import career_lines

    lines = career_lines("2020/05-2024/11：云岚科技 产品负责人\n2025/12~至今：北窗科技 负责人")
    assert len(lines) == 2
    assert "云岚科技" in lines[0]
    assert "至今" in lines[1]


BODY = """# 简历
韩跑跑
## 经历
2018-2020 甲公司 工程师 支付核心
2020-2023 乙集团 高级工程师
2023-今 腾讯 工程师
## 教育
2014-2018 某大学 计算机
"""

CAREER_CHUNK = "2018-2020 甲公司 工程师 支付核心"
EDU_CHUNK = "2014-2018 某大学 计算机"
LEFTOVER = "丙工厂夜班质检尚未写入"


def _chunks() -> list[dict]:
    return [
        {"seq": 0, "text": CAREER_CHUNK, "cited": 0},
        {"seq": 1, "text": EDU_CHUNK, "cited": 0},
    ]


def test_evidence_detects_career_and_education():
    got = evidence_chapters(BODY, "人物", owner=True)
    assert "事业经历" in got and "教育背景" in got


def test_named_chapter_headings_are_evidence():
    body = "## 代表作品\n2020 年出版《海边》。\n## 健康档案\n2019 年做过手术。"
    got = evidence_chapters(body, "人物", owner=True)
    assert "代表作品" in got
    assert "健康档案" in got
    with pytest.raises(RuntimeError, match="代表作品"):
        assert_coverage(body, [{"seq": 1, "text": "2020 年出版《海边》。"}], [], None)


def test_plain_job_field_is_not_resume_evidence():
    assert evidence_chapters("现职是工程师", "人物", True) == set()
    assert evidence_chapters("我在腾讯任工程师", "人物", True) == set()


def test_unit_chunks_do_not_expand_when_nothing_overlaps():
    chunks = [{"text": "甲"}, {"text": "乙段完全不相关"}]
    assert unit_chunks(chunks, "甲") == [{"text": "甲"}]
    assert unit_chunks(chunks, "丙") == []
    assert unit_chunks(chunks, "") == []


def test_absent_verdict_is_not_reported_as_synthetic_none():
    items = [{"op": "field", "field": "真名", "value": "韩跑跑", "quote": "韩跑跑", "crystal": "记忆主人"}]
    sheet = [{"crystal": "记忆主人", "type": "人物", "entries": [
        {"kind": "chapter", "key": "代表作品", "verdict": "写入", "value": "x", "quote": "x"},
    ]}]
    forbid_synthetic_none(sheet, BODY, items)
    with pytest.raises(RuntimeError, match="覆盖"):
        assert_coverage(BODY, _chunks(), items, sheet=sheet)


def test_forbid_synthetic_none_when_sheet_missing_entries_filled_as_none():
    items = [{"op": "field", "field": "真名", "value": "韩跑跑", "quote": "韩跑跑", "crystal": "记忆主人"}]
    sheet = [{"crystal": "记忆主人", "type": "人物", "entries": [
        {"kind": "chapter", "key": "事业经历", "verdict": "无", "value": "", "quote": ""},
        {"kind": "chapter", "key": "教育背景", "verdict": "无", "value": "", "quote": ""},
    ]}]
    with pytest.raises(RuntimeError, match="合成|无|覆盖|事业经历"):
        forbid_synthetic_none(sheet, BODY, items)


def test_field_only_publish_rejected_under_coverage():
    items = [
        {"op": "field", "field": "真名", "value": "韩跑跑", "quote": "韩跑跑", "crystal": "记忆主人"},
        {"op": "field", "field": "现职", "value": "工程师", "quote": "腾讯 工程师", "crystal": "记忆主人"},
    ]
    with pytest.raises(RuntimeError, match="覆盖|残段|事业经历|教育"):
        assert_coverage(BODY, _chunks(), items, sheet=None)


def test_chapters_covering_both_slices_pass():
    items = [
        {"op": "chapter", "chapter": "事业经历", "value": CAREER_CHUNK, "quote": CAREER_CHUNK, "crystal": "记忆主人"},
        {"op": "chapter", "chapter": "教育背景", "value": EDU_CHUNK, "quote": EDU_CHUNK, "crystal": "记忆主人"},
    ]
    report = assert_coverage(BODY, _chunks(), items, sheet=None)
    assert report.complete
    assert report.uncovered == []


def test_one_chapter_without_residual_is_rejected():
    items = [
        {"op": "chapter", "chapter": "事业经历", "value": CAREER_CHUNK, "quote": CAREER_CHUNK, "crystal": "记忆主人"},
    ]
    with pytest.raises(RuntimeError, match="覆盖|残段|教育"):
        assert_coverage(BODY, _chunks(), items, sheet=None)


def test_residual_allows_uncovered_slice_and_writes_summary():
    items = [
        {"op": "chapter", "chapter": "事业经历", "value": CAREER_CHUNK, "quote": CAREER_CHUNK, "crystal": "记忆主人"},
        {"op": "residual", "value": "教育段仅保留原文", "quote": EDU_CHUNK},
    ]
    report = assert_coverage(BODY, _chunks(), items, sheet=None)
    assert report.uncovered
    summary = residual_summary_required(report.uncovered)
    assert "未覆盖残段" in summary
    assert "某大学" in summary or "教育" in summary or EDU_CHUNK[:6] in summary


def _open_engine(tmp_path, monkeypatch, llm_port: int | None = None):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    if llm_port:
        monkeypatch.setenv("ROGMEM_LLM_BASE", f"http://127.0.0.1:{llm_port}/v1")
        monkeypatch.setenv("ROGMEM_LLM_KEY", "stub")
        monkeypatch.setenv("ROGMEM_LLM_MODEL", "stub")
    else:
        monkeypatch.delenv("ROGMEM_LLM_BASE", raising=False)
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    return engine


def _insert_source(engine, source_id: str, body: str, chunks: list[tuple[int, str]]):
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES(?, 'user', 'qa', 'qa', ?, ?, ?, '队列中', 'fast', 1)""",
        (source_id, engine.utcnow(), f"h-{source_id}", body),
    )
    for seq, text in chunks:
        cur = con.execute(
            "INSERT INTO chunks(source_id,seq,loc,text) VALUES(?,?,?,?)",
            (source_id, seq, str(seq + 1), text),
        )
        con.execute("INSERT INTO fts_chunk(chunk_id,text) VALUES(?,?)", (str(cur.lastrowid), engine._tokens(text)))
    con.commit()
    return con


def test_cite_fallback_uses_loc_not_the_previous_slice(tmp_path, monkeypatch):
    engine = _open_engine(tmp_path, monkeypatch)
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
           VALUES('loc-src','user','qa','qa',?,'loc-h','甲乙','队列中','fast',1)""",
        (engine.utcnow(),),
    )
    con.execute("INSERT INTO chunks(source_id,seq,loc,text) VALUES('loc-src',1,'nope','甲公司')")
    con.execute("INSERT INTO chunks(source_id,seq,loc,text) VALUES('loc-src',0,'2','某大学')")
    con.commit()
    token = engine._CHUNK_SEQ.set(2)
    try:
        engine._add_statement(
            con, "owner", None, "事业经历", "不在任何切片", "不在任何切片", "loc-src", "user", "written", None
        )
    finally:
        engine._CHUNK_SEQ.reset(token)
    con.commit()
    cited = {row["seq"]: row["cited"] for row in con.execute("SELECT seq, cited FROM chunks WHERE source_id='loc-src'")}
    assert cited == {0: 1, 1: 0}


def test_one_unit_does_not_wait_for_a_later_slice(tmp_path, monkeypatch):
    engine = _open_engine(tmp_path, monkeypatch)
    later = "2001-2004 丙公司 质检"
    full = CAREER_CHUNK + "\n" + later
    con = _insert_source(engine, "unit-src", full, [(0, CAREER_CHUNK), (1, later)])
    source = con.execute("SELECT * FROM sources WHERE id='unit-src'").fetchone()
    entries = []
    for kind, key in engine.checklist("人物", True):
        if kind == "chapter" and key == "事业经历":
            entries.append({"kind": kind, "key": key, "verdict": "写入", "value": CAREER_CHUNK, "quote": CAREER_CHUNK})
        else:
            entries.append({"kind": kind, "key": key, "verdict": "无", "value": "", "quote": ""})
    items = [{"op": "chapter", "chapter": "事业经历", "value": CAREER_CHUNK, "quote": CAREER_CHUNK, "crystal": "记忆主人"}]
    sheet = [{"crystal": "记忆主人", "type": "人物", "entries": entries}]
    report = engine._require_publish_sheet(source, items, {}, sheet, full, unit_body=CAREER_CHUNK)
    assert report is not None and report.complete


def test_one_statement_does_not_cite_all_chunks(tmp_path, monkeypatch):
    engine = _open_engine(tmp_path, monkeypatch)
    con = _insert_source(engine, "cite-src", BODY, [(0, CAREER_CHUNK), (1, EDU_CHUNK)])
    engine._add_statement(
        con, "owner", None, "事业经历", CAREER_CHUNK, CAREER_CHUNK, "cite-src", "user", "written", None
    )
    con.commit()
    cited = {row["seq"]: row["cited"] for row in con.execute("SELECT seq, cited FROM chunks WHERE source_id='cite-src'")}
    assert cited[0] == 1
    assert cited[1] == 0
    found = engine.search("某大学")
    assert any("某大学" in item.get("text", "") for item in found.get("raw", [])), found


def test_mark_chunks_cited_never_updates_without_seq(tmp_path, monkeypatch):
    engine = _open_engine(tmp_path, monkeypatch)
    con = _insert_source(engine, "mark-src", BODY, [(0, CAREER_CHUNK), (1, EDU_CHUNK)])
    mark_chunks_cited(con, "mark-src", [])
    con.commit()
    rows = list(con.execute("SELECT cited FROM chunks WHERE source_id='mark-src'"))
    assert all(row["cited"] == 0 for row in rows)
    mark_chunks_cited(con, "mark-src", [0])
    cited = {row["seq"]: row["cited"] for row in con.execute("SELECT seq, cited FROM chunks WHERE source_id='mark-src'")}
    assert cited == {0: 1, 1: 0}


def test_field_only_forced_write_is_not_coverage_complete(tmp_path, monkeypatch):
    engine = _open_engine(tmp_path, monkeypatch)
    con = _insert_source(engine, "partial-src", BODY, [(0, CAREER_CHUNK), (1, EDU_CHUNK)])
    engine._add_statement(con, "owner", "真名", None, "韩跑跑", "韩跑跑", "partial-src", "user", "written", None)
    con.commit()
    state = coverage_state_for_source(con, "partial-src", BODY)
    assert state == "partial"
    set_source_distill_state(con, "partial-src", state)
    con.commit()
    row = con.execute("SELECT status, coverage FROM sources WHERE id='partial-src'").fetchone()
    assert row["status"] == DISTILLED
    assert row["coverage"] == "partial"


def test_coverage_complete_only_when_slices_land(tmp_path, monkeypatch):
    engine = _open_engine(tmp_path, monkeypatch)
    con = _insert_source(engine, "full-src", BODY, [(0, CAREER_CHUNK), (1, EDU_CHUNK)])
    engine._add_statement(con, "owner", None, "事业经历", CAREER_CHUNK, CAREER_CHUNK, "full-src", "user", "written", None)
    engine._add_statement(con, "owner", None, "教育背景", EDU_CHUNK, EDU_CHUNK, "full-src", "user", "written", None)
    con.commit()
    state = coverage_state_for_source(con, "full-src", BODY)
    assert state == "complete"
    set_source_distill_state(con, "full-src", state)
    con.commit()
    row = con.execute("SELECT status, coverage FROM sources WHERE id='full-src'").fetchone()
    assert row["status"] == DISTILLED
    assert row["coverage"] == "complete"


def test_llm_stub_proposes_career_and_education_for_resume():
    from tests.llm_stub import propose

    data = propose(BODY)
    chapters = [item for item in data["items"] if item.get("op") == "chapter"]
    blob = json.dumps(data, ensure_ascii=False)
    assert any(item.get("chapter") == "事业经历" for item in chapters)
    assert any(item.get("chapter") == "教育背景" for item in chapters)
    assert "甲公司" in blob and "乙集团" in blob and "某大学" in blob
    assert not all(item.get("op") == "field" for item in data["items"])


def test_resume_pump_writes_career_education_and_keeps_uncited_raw(tmp_path, monkeypatch):
    from tests.llm_stub import serve

    server = serve(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        engine = _open_engine(tmp_path, monkeypatch, llm_port=port)
        con = engine.db()
        sid = "resume-src"
        con.execute(
            """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready)
               VALUES(?, 'user', 'qa', 'qa', ?, 'resume-h', ?, '队列中', 'fast', 1)""",
            (sid, engine.utcnow(), BODY),
        )
        engine._index_text(con, sid, BODY)
        con.execute("INSERT INTO queue(source_id,lane,state) VALUES(?, 'fast', 'queued')", (sid,))
        extra = con.execute(
            "INSERT INTO chunks(source_id,seq,loc,text) VALUES(?,?,?,?)",
            (sid, 99, "99", LEFTOVER),
        )
        con.execute("INSERT INTO fts_chunk(chunk_id,text) VALUES(?,?)", (str(extra.lastrowid), engine._tokens(LEFTOVER)))
        con.commit()
        processed = engine.pump_one()
        assert processed == sid
        row = con.execute(
            "SELECT chapters_json, envelope_json, body_md FROM crystallizations WHERE id='owner'"
        ).fetchone()
        thick = json.loads(row["chapters_json"] or "{}")
        assert "甲公司" not in json.dumps(thick, ensure_ascii=False)
        detail = engine.crystal_detail("owner")
        blob = json.dumps(detail["chapters"], ensure_ascii=False)
        assert "甲公司" in blob and "乙集团" in blob and "某大学" in blob
        assert "甲公司" in (row["body_md"] or "")
        row = con.execute("SELECT status, coverage FROM sources WHERE id=?", (sid,)).fetchone()
        assert row["status"] == DISTILLED
        assert row["coverage"] == "complete"
        leftover = con.execute("SELECT cited FROM chunks WHERE source_id=? AND seq=99", (sid,)).fetchone()[0]
        assert leftover == 0
        found = engine.search("丙工厂")
        assert any("丙工厂" in item.get("text", "") for item in found.get("raw", [])), found
    finally:
        server.shutdown()


def test_ui_lists_distinguish_partial_and_complete_coverage():
    from rogmem import ui

    html = ui.sources_page(
        [
            {
                "id": "a",
                "kind": "user",
                "preview": "字段已写",
                "device_name": "qa",
                "agent_name": "qa",
                "status": DISTILLED,
                "status_label": DISTILLED,
                "coverage": "partial",
                "ingested_at": "",
                "project_ref": "",
                "note": "覆盖未齐",
            },
            {
                "id": "b",
                "kind": "user",
                "preview": "简历已齐",
                "device_name": "qa",
                "agent_name": "qa",
                "status": DISTILLED,
                "status_label": DISTILLED,
                "coverage": "complete",
                "ingested_at": "",
                "project_ref": "",
                "note": "",
            },
        ],
        "session",
        {},
        {"statuses": [DISTILLED, "蒸馏无结晶"]},
    )
    assert "字段已写" in html
    assert "简历已齐" in html
    assert "覆盖未齐" in html
    detail = ui.source_detail(
        {
            "id": "a",
            "kind": "user",
            "status": DISTILLED,
            "coverage": "partial",
            "_uncovered_chunks": [{"seq": 1, "text": EDU_CHUNK}],
            "residual": "",
        },
        [],
        None,
    )
    assert "尚未覆盖的切片" in detail
    assert EDU_CHUNK in detail
    assert "已有结晶，还有切片没被引文盖住" in detail
    complete = ui.source_detail(
        {"id": "b", "kind": "user", "status": DISTILLED, "coverage": "complete", "_uncovered_chunks": [], "residual": ""},
        [],
        None,
    )
    assert "应盖住的切片都已有落点" in complete
