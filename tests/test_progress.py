"""The source filter reads the same lamps the list paints."""

import re

from rogmem.progress import (
    DONE,
    ST_NO_CRYSTAL,
    WAIT_DRAFT,
    flow_states,
    matches,
    processing_filters,
    vector_state,
)
from rogmem.ui import sources_page


def _options(html: str) -> list[tuple[str, str]]:
    select = html.split("处理状态", 1)[1].split("</select>", 1)[0]
    return re.findall(r"<option value='([^']*)'[^>]*>([^<]*)</option>", select)


def test_session_and_file_share_the_lamp_filter_and_drop_the_old_words():
    session = sources_page([], "session", {}, {"statuses": ["蒸馏中"]})
    files = sources_page([], "file", {}, {"statuses": ["已解析待向量化", "蒸馏中"]})
    assert _options(session) == _options(files)
    labels = [label for _value, label in _options(session)]
    assert labels[0] == "全部"
    assert "解析失败" in labels
    assert "向量化进行中" in labels
    assert "待蒸馏" in labels
    assert "草稿进行中" in labels
    assert "并页重试中" in labels
    assert "润色失败" not in labels
    assert "速写进行中" in labels
    assert "蒸馏无结晶" in labels
    assert "已完成" in labels
    assert "蒸馏中" not in labels
    assert "已向量化待蒸馏" not in labels
    assert "蒸馏已结晶" not in labels
    assert "蒸馏失败" not in labels


def test_an_old_status_link_stays_visible_on_the_control():
    html = sources_page([], "file", {"status": "蒸馏中"}, {})
    values = [value for value, _label in _options(html)]
    assert values[1] == "蒸馏中"


def test_each_filter_finds_the_lamp_it_names():
    parked = {
        "解析:fail": ("解析:fail", "解析失败"),
        "无正文": ("解析:fail", "无正文"),
        "向量化:run": ("解析:ok~向量:run", "已解析待向量化"),
        "向量化:retry": ("解析:ok~向量:retry", "已解析待向量化"),
        "向量化:fail": ("解析:ok~向量:fail", "向量化失败"),
        WAIT_DRAFT: ("解析:ok~向量:ok", "已向量化待蒸馏"),
        "草稿:run": ("解析:ok,草稿:run~向量:ok", "蒸馏中"),
        "草稿:retry": ("并页|解析:ok,草稿:retry", "蒸馏中"),
        "草稿:fail": ("解析:ok,草稿:fail", "蒸馏失败"),
        "并页:run": ("解析:ok,草稿:ok,并页:run", "蒸馏中"),
        "并页:retry": ("解析:ok,草稿:ok,并页:retry", "蒸馏中"),
        "并页:fail": ("并页|解析:ok,草稿:ok,并页:fail", "蒸馏失败"),
        "速写:run": ("解析:ok,草稿:ok,并页:ok,速写:run", "蒸馏已结晶"),
        "速写:retry": ("解析:ok,草稿:ok,并页:ok,速写:retry", "蒸馏已结晶"),
        "速写:fail": ("解析:ok,草稿:ok,并页:ok,速写:fail", "蒸馏失败"),
        ST_NO_CRYSTAL: ("草稿:ok", "蒸馏无结晶"),
        DONE: ("完成:ok~向量:ok", "蒸馏已结晶"),
    }
    offered = [value for value, _label in processing_filters()]
    assert list(parked) == offered
    for chosen, (pipeline, status) in parked.items():
        assert matches(pipeline, status, chosen)
        others = [item for item in offered if item != chosen]
        # A side lamp can stay red while a later stage runs. Terminals do not overlap.
        if chosen not in {"向量化:run", "向量化:retry", "向量化:fail"}:
            assert not any(matches(pipeline, status, item) for item in others if not item.startswith("向量化:"))


def test_a_running_draft_with_a_failed_vector_matches_both_lamps():
    pipeline, status = "解析:ok,草稿:run~向量:fail", "蒸馏中"
    assert matches(pipeline, status, "草稿:run")
    assert matches(pipeline, status, "向量化:fail")
    assert not matches(pipeline, status, WAIT_DRAFT)
    assert not matches(pipeline, status, DONE)


def test_sketching_is_not_finished_just_because_the_page_was_saved():
    assert matches("解析:ok,草稿:ok,并页:ok,速写:run", "蒸馏已结晶", "速写:run")
    assert matches("解析:ok,草稿:ok,并页:ok,润色:ok,速写:run", "蒸馏已结晶", "速写:run")
    assert not matches("解析:ok,草稿:ok,并页:ok,速写:run", "蒸馏已结晶", DONE)
    assert matches("完成:ok", "蒸馏已结晶", DONE)


def test_an_old_status_word_asks_the_lamp_question():
    assert matches("解析:ok,草稿:ok,并页:fail", "蒸馏失败", "蒸馏失败")
    assert matches("", "队列中", "已向量化待蒸馏")
    assert not matches("解析:ok,草稿:ok,并页:fail", "蒸馏失败", "速写:fail")
    assert not matches("解析:ok,草稿:ok,并页:ok,速写:run", "蒸馏已结晶", "蒸馏已结晶")
    assert matches("完成:ok", "蒸馏已结晶", "蒸馏已结晶")
    assert matches("解析:ok,草稿:run", "蒸馏已结晶", "蒸馏中")


def test_the_list_pages_by_the_lamp_not_the_stored_word(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    for index in range(51):
        engine.db().execute(
            """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pipeline)
               VALUES(?,?,?,'h',?,'蒸馏中','fast',?)""",
            (
                f"draft-{index}",
                "user",
                f"2026-02-01T00:{index:02d}:00+00:00",
                f"句子{index}",
                "解析:ok,草稿:run",
            ),
        )
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pipeline)
           VALUES('sketch','file','2026-03-01T00:00:00+00:00','hs','文件','蒸馏已结晶','fast',?)""",
        ("解析:ok,草稿:ok,并页:ok,速写:run",),
    )
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pipeline)
           VALUES('done','user','2026-04-01T00:00:00+00:00','hd','写完','蒸馏已结晶','fast','完成:ok')"""
    )
    engine.db().commit()
    drafting = engine.list_sources("session", status="草稿:run", page=1)
    assert drafting["total"] == 51 and len(drafting["rows"]) == 50
    rest = engine.list_sources("session", status="草稿:run", page=2)
    assert len(rest["rows"]) == 1
    assert engine.list_sources("session", status="完成")["rows"][0]["id"] == "done"
    assert engine.list_sources("file", status="速写:run")["rows"][0]["id"] == "sketch"
    assert engine.list_sources("session", status="蒸馏已结晶")["rows"][0]["id"] == "done"
    assert engine.list_sources("file", status="蒸馏已结晶")["rows"] == []


def test_an_old_polish_failure_stays_on_sketch_beside_the_vector_lamp():
    pipeline = "润色|解析:ok,草稿:ok,并页:ok,润色:fail~向量:ok"
    assert [state for _name, state in flow_states(pipeline, "蒸馏失败")] == ["ok", "ok", "ok", "fail"]
    assert vector_state(pipeline, "蒸馏失败") == "ok"
    assert matches(pipeline, "蒸馏失败", "速写:fail")


def test_a_quiet_line_follows_the_outcome_word_when_the_mark_was_left_behind():
    assert flow_states("完成:ok", "蒸馏无结晶") == flow_states("草稿:ok", "蒸馏无结晶")
    assert [state for _name, state in flow_states("完成:ok~向量:fail", "蒸馏无结晶")] == [
        "ok",
        "ok",
        "wait",
        "wait",
    ]
    assert matches("完成:ok~向量:fail", "蒸馏无结晶", "蒸馏无结晶")
    assert matches("完成:ok~向量:fail", "蒸馏无结晶", "向量化:fail")
    assert not matches("完成:ok~向量:fail", "蒸馏无结晶", DONE)
    assert matches("草稿:ok", "蒸馏已结晶", DONE)
    assert not matches("草稿:ok", "蒸馏已结晶", "蒸馏无结晶")
    assert not matches("解析:ok~向量:wait", "已解析待向量化", WAIT_DRAFT)
    assert matches("解析:ok~向量:run", "已解析待向量化", "向量化:run")


def test_recording_no_crystal_keeps_the_vector_lamp(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pipeline)
           VALUES('thin','file','2026-05-01T00:00:00+00:00','h','没有页','蒸馏中','fast','解析:ok,草稿:run~向量:fail')"""
    )
    engine.db().commit()
    engine._mark_no_crystal(engine.db(), "thin")
    engine.db().commit()
    row = engine.db().execute("SELECT status, pipeline FROM sources WHERE id='thin'").fetchone()
    assert row["status"] == "蒸馏无结晶"
    assert row["pipeline"].endswith("~向量:fail")
    assert "草稿:ok" in row["pipeline"]
    assert matches(row["pipeline"], row["status"], "蒸馏无结晶")
    assert matches(row["pipeline"], row["status"], "向量化:fail")

    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pipeline)
           VALUES('stale','file','2026-05-02T00:00:00+00:00','h2','旧的完成','蒸馏已结晶','fast','完成:ok~向量:ok')"""
    )
    engine.db().commit()
    engine.settle_file_statuses()
    stale = engine.db().execute("SELECT status, pipeline FROM sources WHERE id='stale'").fetchone()
    assert stale["status"] == "蒸馏无结晶"
    assert stale["pipeline"].endswith("~向量:ok")
    assert matches(stale["pipeline"], stale["status"], "蒸馏无结晶")
    assert not matches(stale["pipeline"], stale["status"], DONE)
