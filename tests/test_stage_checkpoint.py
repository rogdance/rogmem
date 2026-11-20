"""A finished stage stays on disk. The next attempt starts at the stage that failed."""

import json


def _open(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _page(engine, name: str, body: str) -> None:
    engine.db().execute(
        """INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,body_md)
           VALUES(?,?,?,'active','{}','{}',?)""",
        (name, "人物", name, body),
    )
    engine.db().commit()


def _requeue(engine, source_id: str) -> None:
    engine.db().execute(
        "UPDATE queue SET state='queued', next_at=NULL, leased_until=NULL WHERE source_id=?",
        (source_id,),
    )
    engine.db().commit()


def _body(engine, name: str) -> str:
    row = engine.db().execute("SELECT body_md FROM crystallizations WHERE name=?", (name,)).fetchone()
    return row["body_md"] or ""


def test_a_merged_page_is_kept_when_the_next_page_fails(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _page(engine, "甲页", "甲的旧说法。")
    _page(engine, "乙页", "乙的旧说法。")
    drafted = {"n": 0}
    merged: list[str] = []

    def produce(_text, _kind="", **_kwargs):
        drafted["n"] += 1
        return {
            "payload_kind": "markdown",
            "pages": [
                {"name": "甲页", "type": "人物", "body": "甲在南港。"},
                {"name": "乙页", "type": "人物", "body": "乙在北城。"},
            ],
        }

    def merge(_old, new, *_args, **_kwargs):
        merged.append(new)
        if "乙在北城" in (new or "") and merged.count(new) == 1:
            raise engine.ModelCallFailed("timeout")
        return new, [], "", "", False, "", ""

    monkeypatch.setattr(engine, "_produce_distill_payload", produce)
    monkeypatch.setattr(engine, "_merge_existing_page", merge)
    monkeypatch.setattr(engine, "_ask_sketch", lambda *_args, **_kwargs: "")
    path = tmp_path / "scan" / "两页.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("甲在南港。乙在北城。", encoding="utf-8")
    opened = engine.ingest_file(path)
    assert engine.pump_one() == opened["id"]
    assert drafted["n"] == 1
    assert merged == ["甲在南港。", "乙在北城。"]
    assert "甲在南港" in _body(engine, "甲页")
    assert _body(engine, "乙页").strip() == "乙的旧说法。"
    stored = json.loads(
        engine.db().execute("SELECT stage_json FROM sources WHERE id=?", (opened["id"],)).fetchone()["stage_json"]
    )
    assert stored["phase"] == "draft"
    assert any(page.get("name") == "甲页" and page.get("landed") for page in stored["pages"])
    _requeue(engine, opened["id"])
    assert engine.pump_one() == opened["id"]
    assert drafted["n"] == 1
    assert merged == ["甲在南港。", "乙在北城。", "乙在北城。"]
    assert "乙在北城" in _body(engine, "乙页")


def test_a_failed_sketch_keeps_the_landed_page_and_retries_only_the_sketch(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _page(engine, "甲页", "甲的旧说法。")
    drafted = {"n": 0}
    merged = {"n": 0}
    sketches: list[str] = []

    def produce(_text, _kind="", **_kwargs):
        drafted["n"] += 1
        return {"payload_kind": "markdown", "pages": [{"name": "甲页", "type": "人物", "body": "甲在南港。"}]}

    def merge(_old, new, *_args, **_kwargs):
        merged["n"] += 1
        return new, [], "", "", False, "", ""

    def sketch(body, *_args, **_kwargs):
        sketches.append(body)
        if len(sketches) == 1:
            raise engine.ModelCallFailed("timeout")
        return "甲的速写。"

    monkeypatch.setattr(engine, "_produce_distill_payload", produce)
    monkeypatch.setattr(engine, "_merge_existing_page", merge)
    monkeypatch.setattr(engine, "_ask_sketch", sketch)
    path = tmp_path / "scan" / "一页.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("甲在南港。", encoding="utf-8")
    opened = engine.ingest_file(path)
    assert engine.pump_one() == opened["id"]
    assert "甲在南港" in _body(engine, "甲页")
    assert engine.db().execute("SELECT sketch_md FROM crystallizations WHERE name='甲页'").fetchone()["sketch_md"] in {
        None,
        "",
    }
    kept = json.loads(
        engine.db().execute("SELECT stage_json FROM sources WHERE id=?", (opened["id"],)).fetchone()["stage_json"]
    )
    assert kept["phase"] == "landed"
    assert kept["pending_sketches"]
    _requeue(engine, opened["id"])
    assert engine.pump_one() == opened["id"]
    assert drafted["n"] == 1
    assert merged["n"] == 1
    assert len(sketches) == 2
    assert engine.db().execute("SELECT sketch_md FROM crystallizations WHERE name='甲页'").fetchone()["sketch_md"] == "甲的速写。"
    done = engine.db().execute("SELECT stage_json, pipeline FROM sources WHERE id=?", (opened["id"],)).fetchone()
    assert not done["stage_json"]
    assert (done["pipeline"] or "").startswith("完成:ok")


def test_a_finished_sketch_is_not_asked_again(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _page(engine, "甲页", "甲的旧说法。")
    _page(engine, "乙页", "乙的旧说法。")
    sketches: list[str] = []

    def produce(_text, _kind="", **_kwargs):
        return {
            "payload_kind": "markdown",
            "pages": [
                {"name": "甲页", "type": "人物", "body": "甲在南港。"},
                {"name": "乙页", "type": "人物", "body": "乙在北城。"},
            ],
        }

    def sketch(body, *_args, **_kwargs):
        sketches.append(body)
        if "乙在北城" in (body or "") and sketches.count(body) == 1:
            raise engine.ModelCallFailed("timeout")
        return "速写。"

    monkeypatch.setattr(engine, "_produce_distill_payload", produce)
    monkeypatch.setattr(engine, "_merge_existing_page", lambda _old, new, *_a, **_k: (new, [], "", "", False, "", ""))
    monkeypatch.setattr(engine, "_ask_sketch", sketch)
    path = tmp_path / "scan" / "速写.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("甲在南港。乙在北城。", encoding="utf-8")
    opened = engine.ingest_file(path)
    assert engine.pump_one() == opened["id"]
    assert any("甲在南港" in item for item in sketches)
    assert any("乙在北城" in item for item in sketches)
    _requeue(engine, opened["id"])
    assert engine.pump_one() == opened["id"]
    rest = sketches[2:]
    assert rest and not any("甲在南港" in item for item in rest)
    assert any("乙在北城" in item for item in rest)


def test_a_reset_lamp_does_not_redraft_a_landed_checkpoint(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _page(engine, "甲页", "甲在南港。")
    payload = json.dumps(
        {
            "phase": "landed",
            "pages": [
                {
                    "name": "甲页",
                    "type": "人物",
                    "body": "甲在南港。",
                    "gist": "",
                    "id": "甲页",
                    "settled": True,
                    "landed": True,
                    "crystal_id": "甲页",
                }
            ],
            "pending_sketches": ["甲页"],
        },
        ensure_ascii=False,
    )
    pipeline = engine._begin_attempt("解析:ok,草稿:ok,并页:ok,速写:run")
    engine.db().execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,pipeline,stage_json,pair_ready)
           VALUES('src-kept','file','2026-11-11T00:00:00+00:00','h','甲在南港。','萃取中','fast',?, ?,1)""",
        (pipeline, payload),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id='src-kept'").fetchone()
    monkeypatch.setattr(
        engine,
        "_produce_distill_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("已经落地的页不该重做草稿")),
    )
    monkeypatch.setattr(
        engine,
        "_merge_existing_page",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("已经落地的页不该重做并页")),
    )
    seen: list[str] = []
    monkeypatch.setattr(engine, "_write_sketch", lambda crystal_id, **_kwargs: seen.append(crystal_id))
    assert engine._distill(source, 0) is True
    assert seen == ["甲页"]
    row = engine.db().execute("SELECT stage_json, pipeline FROM sources WHERE id='src-kept'").fetchone()
    assert not row["stage_json"]
    assert (row["pipeline"] or "").startswith("完成:ok")


def test_a_merge_that_already_includes_the_stored_page_does_not_repeat_it(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _page(engine, "甲页", "旧说法。")
    calls = {"n": 0}

    def produce(_text, _kind="", **_kwargs):
        return {
            "payload_kind": "markdown",
            "pages": [
                {"name": "甲页", "type": "人物", "body": "第一段。"},
                {"name": "甲页", "type": "人物", "body": "第二段。"},
            ],
        }

    def merge(old, _new, *_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return f"{old}\n第一段。", [], "", "", False, "", ""
        if calls["n"] == 2:
            raise engine.ModelCallFailed("timeout")
        return f"{old}\n第二段。", [], "", "", False, "", ""

    monkeypatch.setattr(engine, "_produce_distill_payload", produce)
    monkeypatch.setattr(engine, "_merge_existing_page", merge)
    monkeypatch.setattr(engine, "_ask_sketch", lambda *_args, **_kwargs: "")
    path = tmp_path / "scan" / "同一页.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("第一段。第二段。", encoding="utf-8")
    opened = engine.ingest_file(path)
    assert engine.pump_one() == opened["id"]
    _requeue(engine, opened["id"])
    assert engine.pump_one() == opened["id"]
    body = _body(engine, "甲页")
    assert body.count("第一段") == 1
    assert body.count("旧说法") == 1
    assert "第二段" in body


def test_finishing_one_sketch_is_recorded_with_the_sketch(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _page(engine, "甲页", "甲在南港。")
    _page(engine, "乙页", "乙在北城。")
    asked: list[str] = []

    def sketch(body, *_args, **_kwargs):
        asked.append(body)
        if "乙在北城" in (body or ""):
            raise engine.ModelCallFailed("timeout")
        return "甲的速写。"

    monkeypatch.setattr(engine, "_produce_distill_payload", lambda *_a, **_k: {
        "payload_kind": "markdown",
        "pages": [
            {"name": "甲页", "type": "人物", "body": "甲在南港。"},
            {"name": "乙页", "type": "人物", "body": "乙在北城。"},
        ],
    })
    monkeypatch.setattr(engine, "_merge_existing_page", lambda _old, new, *_a, **_k: (new, [], "", "", False, "", ""))
    monkeypatch.setattr(engine, "_ask_sketch", sketch)
    path = tmp_path / "scan" / "两篇速写.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("甲在南港。乙在北城。", encoding="utf-8")
    opened = engine.ingest_file(path)
    assert engine.pump_one() == opened["id"]
    stored = json.loads(
        engine.db().execute("SELECT stage_json FROM sources WHERE id=?", (opened["id"],)).fetchone()["stage_json"]
    )
    assert stored["pending_sketches"] == ["乙页"]
    assert engine.db().execute("SELECT sketch_md FROM crystallizations WHERE id='甲页'").fetchone()["sketch_md"] == "甲的速写。"
    asked_before = len(asked)
    source = engine.db().execute("SELECT * FROM sources WHERE id=?", (opened["id"],)).fetchone()
    try:
        engine._land_markdown_pages(source, stored["pages"], start="落地")
    except engine.ModelCallFailed:
        pass
    again = asked[asked_before:]
    assert again and not any("甲在南港" in item for item in again)
    assert any("乙在北城" in item for item in again)
