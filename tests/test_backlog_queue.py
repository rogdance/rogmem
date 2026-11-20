"""Acceptance checks for the unattended file-pipeline queue."""

from __future__ import annotations

import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import json

from rogmem.crystal_store import CrystalStore
from rogmem.ui import page


def _spy_apply_ops(monkeypatch):
    """Record apply_ops calls on the production thin write path."""
    calls: list[dict] = []
    orig = CrystalStore.apply_ops

    def spy(self, crystal_id, expected_revision, ops, *, holder, commit=True):
        calls.append(
            {
                "crystal_id": crystal_id,
                "ops": list(ops),
                "holder": holder,
                "commit": commit,
            }
        )
        return orig(self, crystal_id, expected_revision, ops, holder=holder, commit=commit)

    monkeypatch.setattr(CrystalStore, "apply_ops", spy)
    return calls


def _engine(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def test_read_section_returns_the_chapter(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    engine.db().execute(
        "UPDATE crystallizations SET body_md=? WHERE id='owner'",
        ("## 事业经历\n\n2018 年在甲公司",),
    )
    engine.db().commit()
    found = engine.read_section("记忆主人", "事业经历")
    assert found["text"] == "2018 年在甲公司"
    assert found["missing"] is False
    missing = engine.read_section("记忆主人", "不存在的章节")
    assert missing["missing"] is True


def test_new_file_bytes_close_quotes_that_no_longer_match(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    first = tmp_path / "scan" / "note.txt"
    first.parent.mkdir(exist_ok=True)
    first.write_text("旧句子保留在这里。", encoding="utf-8")
    opened = engine.ingest_file(first)
    engine.db().execute(
        """INSERT INTO statements(id,crystal_id,field,text,quote,source_id,trust,rank,event_at,created_at)
           VALUES('stale','owner','概述','旧事实','旧句子保留在这里。',?,'written','user',?,?)""",
        (opened["id"], engine.utcnow(), engine.utcnow()),
    )
    engine.db().commit()
    first.write_text("新句子已经换掉。", encoding="utf-8")
    engine.ingest_file(first)
    row = engine.db().execute("SELECT invalid_at FROM statements WHERE id='stale'").fetchone()
    assert row["invalid_at"]


def test_waiting_vectors_are_filled_as_one_batch(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    engine._EMBED = "fastembed"
    engine._MODEL = None
    ids = []
    for name, text in (("one.txt", "第一段要补上向量。"), ("two.txt", "第二段也要补上向量。")):
        path = tmp_path / "scan" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        ids.append(engine.ingest_file(path)["id"])
    calls: list[list[str]] = []

    class _Batch:
        def embed(self, texts):
            calls.append(list(texts))
            return [[0.2, 0.8] for _ in texts]

    engine._MODEL = _Batch()
    assert engine._VECTOR_CATCHUP.acquire(blocking=False)
    try:
        assert engine.ensure_pending_vectors() == 0
        assert calls == []
    finally:
        engine._VECTOR_CATCHUP.release()
    changed = engine.ensure_pending_vectors()
    assert changed == 2
    assert len(calls) == 1
    assert len(calls[0]) >= 2
    for source_id in ids:
        status = engine.db().execute("SELECT status FROM sources WHERE id=?", (source_id,)).fetchone()
        queued = engine.db().execute("SELECT id FROM queue WHERE source_id=?", (source_id,)).fetchone()
        assert status["status"] == "已向量化待萃取"
        assert queued is not None


def test_distill_keeps_going_while_vectors_finish(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    engine._EMBED = "fastembed"
    engine._MODEL = None
    path = tmp_path / "scan" / "both.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("萃取先走，向量随后补上。", encoding="utf-8")
    source_id = engine.ingest_file(path)["id"]
    engine.db().execute("UPDATE sources SET status=? WHERE id=?", ("萃取中", source_id))
    engine.db().commit()

    class _Batch:
        def embed(self, texts):
            return [[0.4, 0.6] for _ in texts]

    engine._MODEL = _Batch()
    engine.ensure_pending_vectors()
    other = sqlite3.connect(tmp_path / "rogmem.sqlite")
    other.row_factory = sqlite3.Row
    row = other.execute("SELECT status, pipeline FROM sources WHERE id=?", (source_id,)).fetchone()
    queued = other.execute("SELECT COUNT(*) AS n FROM queue WHERE source_id=?", (source_id,)).fetchone()
    vec = other.execute(
        "SELECT vec_json FROM chunks WHERE source_id=? AND role!='parent'",
        (source_id,),
    ).fetchone()
    other.close()
    assert row["status"] == "萃取中"
    assert "向量:ok" in (row["pipeline"] or "")
    assert queued["n"] == 1
    assert vec["vec_json"]


def test_embedding_does_not_keep_the_database_transaction(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    engine._EMBED = "fastembed"
    engine._MODEL = None
    path = tmp_path / "scan" / "open.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("嵌入的时候数据库锁要放开。", encoding="utf-8")
    engine.ingest_file(path)
    seen: dict[str, bool] = {}

    class _Batch:
        def embed(self, texts):
            seen["open"] = engine.db().in_transaction
            return [[0.2, 0.3] for _ in texts]

    engine._MODEL = _Batch()
    engine.ensure_pending_vectors()
    assert seen["open"] is False


def test_missing_vectors_do_not_enter_distill(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    engine._EMBED = "fastembed"
    monkeypatch.setattr(engine, "_embed_one", lambda _text: None)
    path = tmp_path / "scan" / "plain.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("这段文字足够长，用来确认没有向量时萃取仍然先走。", encoding="utf-8")
    result = engine.ingest_file(path)
    queued = engine.db().execute("SELECT id FROM queue WHERE source_id=?", (result["id"],)).fetchone()
    status = engine.db().execute("SELECT status, pipeline FROM sources WHERE id=?", (result["id"],)).fetchone()
    assert queued is not None
    assert status["status"] == "已解析待向量化"
    assert "向量:run" in (status["pipeline"] or "")


def test_find_rogmem_waits_longer_than_a_slow_relay():
    from installer.rogmem_installer import find_rogmem

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            import time

            time.sleep(0.8)
            body = b'{"service":"rogmem"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert find_rogmem(port, port) == port
    finally:
        server.shutdown()


def test_hermes_config_keeps_bearer_and_adds_device_header(tmp_path):
    from installer.rogmem_installer import write_configs

    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes" / "config.yaml").write_text("memory:\n  provider: onemem\n", encoding="utf-8")
    written = write_configs(home, "http://127.0.0.1:18780", "rm_test_token", ["Hermes"])
    text = (home / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    assert written == ["Hermes"]
    assert "bearer_token: rm_test_token" in text
    assert "Authorization: Bearer rm_test_token" in text
    assert "X-Rogmem-Device:" in text
    assert "provider: rogmem" in text
    assert (home / ".hermes" / "plugins" / "rogmem" / "__init__.py").is_file()


def test_thin_file_does_not_call_the_model(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    path = tmp_path / "scan" / "empty.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("# \n\n---\n***\n", encoding="utf-8")
    result = engine.ingest_file(path)
    queued = engine.db().execute("SELECT id FROM queue WHERE source_id=?", (result["id"],)).fetchone()
    status = engine.db().execute("SELECT status FROM sources WHERE id=?", (result["id"],)).fetchone()
    assert queued is None
    assert status["status"] == "无正文"


def test_short_sentence_still_enters_distill(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    path = tmp_path / "scan" / "ride.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("李明喜欢在夜里骑行。", encoding="utf-8")
    result = engine.ingest_file(path)
    queued = engine.db().execute("SELECT id FROM queue WHERE source_id=?", (result["id"],)).fetchone()
    status = engine.db().execute("SELECT status FROM sources WHERE id=?", (result["id"],)).fetchone()
    assert queued is not None
    assert status["status"] == "已向量化待萃取"


def test_embed_switch_rejects_unknown_model(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    refused = engine.select_embed_model("some-other-model")
    assert refused["ok"] is False
    called = {}

    def fake_mode(mode):
        called["mode"] = mode

    monkeypatch.setattr(engine, "set_embed_mode", fake_mode)
    saved = engine.select_embed_model("BAAI/bge-small-zh-v1.5")
    assert saved["ok"] is True
    assert called["mode"] == "fastembed"


def test_prose_model_must_keep_the_quote(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    source_id = "src-prose"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready)
           VALUES(?,'user','本机','极空间',?,?,?,'2019 年在周生生任职。','已解析','slow',1)""",
        (source_id, now, "file:prose", "hash-prose"),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    item = {"chapter": "事业经历", "value": "2019 年在周生生任职。", "quote": "2019 年在周生生任职。", "crystal": "记忆主人"}

    def refuse(_base, _key, _payload):
        return "改写后丢掉了原文。", "stop"

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m") if role == "prose" else None)
    monkeypatch.setattr(engine, "_complete_chat", refuse)
    engine._write_chapter(source, item, source["body"])
    chapters = engine.db().execute("SELECT chapters_json FROM crystallizations WHERE id='owner'").fetchone()
    assert "周生生" not in chapters["chapters_json"]

    from rogmem.crystal_store import chapter_update_ops

    class _FakeStore:
        def __init__(self):
            self.calls = []

        def apply_ops(self, crystal_id, expected_revision, ops, *, holder, commit=True):
            self.calls.append({"ops": list(ops), "commit": commit})
            return {"crystal_id": crystal_id, "revision": int(expected_revision) + 1}

    def keep(_base, _key, _payload):
        return "他于 2019 年在周生生任职。", "stop"

    monkeypatch.setattr(engine, "_complete_chat", keep)
    fake = _FakeStore()
    engine._write_chapter(source, item, source["body"], store=fake)
    chapters = engine.db().execute("SELECT chapters_json FROM crystallizations WHERE id='owner'").fetchone()
    assert "他于 2019 年在周生生任职。" not in chapters["chapters_json"]
    assert fake.calls[0]["ops"] == chapter_update_ops("事业经历", "他于 2019 年在周生生任职。")
    assert fake.calls[0]["commit"] is False

    monkeypatch.setattr(engine, "llm_target", lambda _role: None)
    monkeypatch.setattr(engine, "_complete_chat", lambda *_args: (_ for _ in ()).throw(AssertionError("没选行文模型却调用了")))
    raw = _FakeStore()
    engine._write_chapter(
        source,
        {"chapter": "个人生活", "value": "住在深圳。", "quote": "住在深圳。", "crystal": "记忆主人"},
        "住在深圳。",
        store=raw,
    )
    chapters = engine.db().execute("SELECT chapters_json FROM crystallizations WHERE id='owner'").fetchone()
    assert "住在深圳。" not in chapters["chapters_json"]
    assert raw.calls[0]["ops"] == chapter_update_ops("个人生活", "住在深圳。")


def test_read_section_does_not_return_other_chapters(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    engine.db().execute(
        "UPDATE crystallizations SET body_md=? WHERE id='owner'",
        ("## 事业经历\n\n只在甲公司任职\n\n## 个人生活\n\n住在海边的房子里",),
    )
    engine.db().execute(
        """INSERT INTO chunks(source_id,seq,loc,text,role,embed_text)
           VALUES('src',1,'0','标题：事业经历\n只在甲公司任职\n标题：个人生活\n住在海边的房子里','parent','文件：a.md\n标题：事业经历')"""
    )
    engine.db().commit()
    found = engine.read_section("记忆主人", "事业经历")
    dumped = str(found)
    assert "只在甲公司任职" in dumped
    assert "住在海边的房子里" not in dumped


def test_revise_chunk_does_not_redistill(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    path = tmp_path / "scan" / "note.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("这段文字足够长，用来确认修订单块时不会整篇重萃取。", encoding="utf-8")
    opened = engine.ingest_file(path)
    chunk = engine.db().execute(
        "SELECT id, text FROM chunks WHERE source_id=? AND role='child' ORDER BY seq LIMIT 1",
        (opened["id"],),
    ).fetchone()
    before = engine.db().execute("SELECT id, state FROM queue WHERE source_id=?", (opened["id"],)).fetchall()
    result = engine.revise_chunk(chunk["id"], "修订后的单块正文仍然足够长 REVCHUNK99。")
    after = engine.db().execute("SELECT id, state FROM queue WHERE source_id=?", (opened["id"],)).fetchall()
    stored = engine.db().execute("SELECT text FROM chunks WHERE id=?", (chunk["id"],)).fetchone()
    revision = engine.db().execute("SELECT previous FROM chunk_revisions WHERE chunk_id=?", (chunk["id"],)).fetchone()
    indexed = engine.db().execute("SELECT text FROM fts_chunk WHERE chunk_id=?", (str(chunk["id"]),)).fetchone()
    assert result["ok"] is True
    assert result["queue"] is True
    assert [(row["id"], row["state"]) for row in before] == [(row["id"], row["state"]) for row in after]
    assert stored["text"] == "修订后的单块正文仍然足够长 REVCHUNK99。"
    assert revision["previous"] == chunk["text"]
    assert "REVCHUNK99" in indexed["text"]


def test_questions_stay_out_until_enabled(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    path = tmp_path / "scan" / "note.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text("捷安特自行车产自台湾，这段正文足够用来做问句索引。", encoding="utf-8")
    opened = engine.ingest_file(path)
    chunk = engine.db().execute(
        "SELECT id FROM chunks WHERE source_id=? AND role='child' ORDER BY seq LIMIT 1",
        (opened["id"],),
    ).fetchone()
    skipped = engine.record_questions(chunk["id"], ["这辆车是哪里生产的 QWHERE99"])
    indexed = engine.db().execute("SELECT text FROM fts_chunk WHERE chunk_id=?", (str(chunk["id"]),)).fetchone()
    assert skipped["enabled"] is False
    assert "QWHERE99" not in (indexed["text"] or "")
    engine.set_setting("index_questions", "on")
    engine.db().commit()
    written = engine.record_questions(chunk["id"], ["这辆车是哪里生产的 QWHERE99"])
    indexed = engine.db().execute("SELECT text FROM fts_chunk WHERE chunk_id=?", (str(chunk["id"]),)).fetchone()
    statements = engine.db().execute("SELECT COUNT(*) AS n FROM statements").fetchone()["n"]
    assert written["indexed"] == 1
    assert written["statements_unchanged"] is True
    assert "QWHERE99" in indexed["text"]
    assert statements == 0


def test_provider_key_is_not_stored_in_plaintext(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "probe_chat", lambda *_args: {"health": "ok", "health_detail": "正常"})
    secret = "sk-plain-secret-xyz"
    saved = engine.save_provider("桩", "http://127.0.0.1:9/v1", secret, "stub")
    engine.assign_models({"model_distill": saved["id"] + "|stub"})
    raw = engine.db().execute("SELECT v FROM settings WHERE k='providers'").fetchone()["v"]
    target = engine.llm_target("distill")
    assert secret not in raw
    assert raw.startswith("[") and "enc:v1:" in raw
    assert target[1] == secret


def test_loose_model_sheet_still_keeps_the_quote(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "我在周生生任职，做金饰零售。"
    loose = {
        "items": [
            {
                "kind": "field",
                "key": "所属组织及职务",
                "verdict": "写入",
                "value": "周生生，零售",
                "quote": body,
            }
        ],
        "sheet": None,
    }
    data = engine._normalize_extract(loose, body)
    assert data["items"][0]["op"] == "field"
    assert data["items"][0]["field"] == "所属组织及职务"
    assert any(entry["key"] == "所属组织及职务" and entry["verdict"] == "写入" for entry in data["sheet"][0]["entries"])
    empty = engine._normalize_extract(
        {"items": [{"op": "field", "field": "真名", "value": "不该发布", "quote": "交卷缺项", "crystal": "记忆主人"}], "sheet": []},
        "交卷缺项把真名改成不该发布",
    )
    assert empty["sheet"] == []
    source_id = "src-live-shape"
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready)
           VALUES(?,'user','本机','测试',?,?,?,?,'队列中','fast',1)""",
        (source_id, now, "turn:live", "hash-live", body),
    )
    engine.db().commit()
    source = engine.db().execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    calls = _spy_apply_ops(monkeypatch)
    used = engine._apply_items(source, data["items"], {}, data["sheet"], unit_body=body)
    assert used >= 1
    assert any(
        call["crystal_id"] == "owner"
        and any("周生生" in str(op.get("value") or "") for op in call["ops"])
        for call in calls
    )
    stmt = engine.db().execute(
        "SELECT text FROM statements WHERE source_id=? AND invalid_at IS NULL",
        (source_id,),
    ).fetchone()
    assert stmt is not None and "周生生" in (stmt["text"] or "")


def _source(engine, kind: str, body: str, sid: str):
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready)
           VALUES(?,?,'本机','测试',?,?,?,?,'队列中','slow',1)""",
        (sid, kind, now, sid, sid, body),
    )
    engine.db().commit()
    return engine.db().execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()


def _person(engine, name: str) -> None:
    summary = {field: "尚无来源" for field in engine.PERSON_FIELDS}
    chapters = {chapter: "暂无来源" for chapter in engine.PERSON_CHAPTERS}
    engine.db().execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) VALUES(?,?,?,'active',?,?)",
        ("p-" + name, "人物", name, json.dumps(summary, ensure_ascii=False), json.dumps(chapters, ensure_ascii=False)),
    )
    engine.db().commit()


def test_file_prompt_does_not_default_to_the_owner():
    from rogmem.engine import _distill_system

    file_prompt = _distill_system("file")
    user_prompt = _distill_system("user")
    assert file_prompt == user_prompt
    assert "说话人是记忆主人" in user_prompt
    assert "就是记忆主人" in user_prompt
    assert "说话人是不确定" in file_prompt
    assert "不是记忆主人在说话" in file_prompt


def test_file_claims_do_not_default_onto_the_owner(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "我喜欢在夜里骑行。"
    source = _source(engine, "file", body, "file-habit")
    data = engine._normalize_extract(
        {
            "items": [
                {"op": "field", "field": "兴趣爱好", "value": "夜里骑行", "quote": body, "crystal": "记忆主人", "type": "人物"},
                {"op": "chapter", "chapter": "个人生活", "value": "夜里骑行", "quote": body, "crystal": "记忆主人", "type": "人物"},
            ],
            "sheet": None,
        },
        body,
    )
    engine._apply_items(source, data["items"], {}, data["sheet"], unit_body=body)
    stored = engine.db().execute("SELECT text, quote FROM statements WHERE crystal_id='owner'").fetchall()
    assert stored == []
    summary = json.loads(engine.db().execute("SELECT summary_json FROM crystallizations WHERE id='owner'").fetchone()["summary_json"])
    assert "夜里骑行" not in json.dumps(summary, ensure_ascii=False)
    assert summary.get("兴趣爱好", "尚无来源") in {"", "尚无来源"}


def test_user_turn_still_defaults_a_habit_to_the_owner(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    body = "我喜欢在夜里骑行。"
    source = _source(engine, "user", body, "user-habit")
    data = engine._normalize_extract(
        {"items": [{"op": "field", "field": "兴趣爱好", "value": "夜里骑行", "quote": body, "type": "人物"}], "sheet": None},
        body,
    )
    calls = _spy_apply_ops(monkeypatch)
    used = engine._apply_items(source, data["items"], {}, data["sheet"], unit_body=body)
    assert used >= 1
    assert any(
        call["crystal_id"] == "owner"
        and any(op.get("value") == "夜里骑行" for op in call["ops"])
        for call in calls
    )
    stmt = engine.db().execute(
        "SELECT text FROM statements WHERE crystal_id='owner' AND field='兴趣爱好' AND invalid_at IS NULL"
    ).fetchone()
    assert stmt is not None and stmt["text"] == "夜里骑行"
    assert engine.crystal_detail("owner")["summary"]["兴趣爱好"] == "夜里骑行"


def test_named_file_habit_lands_on_that_person(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _person(engine, "李明")
    body = "李明喜欢在夜里骑行。"
    source = _source(engine, "file", body, "file-liming")
    data = engine._normalize_extract(
        {
            "items": [
                {
                    "op": "field",
                    "field": "兴趣爱好",
                    "value": "夜里骑行",
                    "quote": body,
                    "crystal": "李明",
                }
            ],
            "sheet": None,
        },
        body,
    )
    calls = _spy_apply_ops(monkeypatch)
    engine._apply_items(source, data["items"], {}, data["sheet"], unit_body=body)
    owner = engine.db().execute(
        "SELECT 1 FROM statements WHERE crystal_id='owner' AND field='兴趣爱好'"
    ).fetchone()
    assert owner is None
    assert any(
        call["crystal_id"] == "p-李明"
        and any(op.get("value") == "夜里骑行" for op in call["ops"])
        for call in calls
    )
    stmt = engine.db().execute(
        "SELECT text FROM statements WHERE crystal_id='p-李明' AND field='兴趣爱好' AND invalid_at IS NULL"
    ).fetchone()
    assert stmt is not None and stmt["text"] == "夜里骑行"


def test_file_guess_is_marked_unverified_on_that_person(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _person(engine, "李明")
    body = "有人喜欢在夜里骑行。"
    source = _source(engine, "file", body, "file-guess")
    data = engine._normalize_extract(
        {
            "items": [
                {
                    "op": "field",
                    "field": "兴趣爱好",
                    "value": "夜里骑行",
                    "quote": body,
                    "crystal": "李明",
                    "type": "人物",
                    "trust": "unverified",
                }
            ],
            "sheet": None,
        },
        body,
    )
    calls = _spy_apply_ops(monkeypatch)
    engine._apply_items(source, data["items"], {}, data["sheet"], unit_body=body)
    summary = json.loads(engine.db().execute("SELECT summary_json FROM crystallizations WHERE id='p-李明'").fetchone()["summary_json"])
    chapters = json.loads(engine.db().execute("SELECT chapters_json FROM crystallizations WHERE id='p-李明'").fetchone()["chapters_json"])
    owner = engine.db().execute("SELECT 1 FROM statements WHERE crystal_id='owner' AND quote=?", (body,)).fetchone()
    assert summary["兴趣爱好"] == "尚无来源"
    assert owner is None
    assert chapters["个人生活"] == "暂无来源"
    assert any(
        call["crystal_id"] == "p-李明"
        and any("待核实" in str(op.get("value") or "") and body in str(op.get("value") or "") for op in call["ops"])
        for call in calls
    )


def test_file_assignment_without_the_name_stays_a_guess(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _person(engine, "李明")
    body = "有人喜欢在夜里骑行。"
    source = _source(engine, "file", body, "file-assigned")
    data = engine._normalize_extract(
        {
            "items": [
                {
                    "op": "field",
                    "field": "兴趣爱好",
                    "value": "夜里骑行",
                    "quote": body,
                    "crystal": "李明",
                    "type": "人物",
                    "trust": "written",
                }
            ],
            "sheet": None,
        },
        body,
    )
    calls = _spy_apply_ops(monkeypatch)
    engine._apply_items(source, data["items"], {}, data["sheet"], unit_body=body)
    summary = json.loads(engine.db().execute("SELECT summary_json FROM crystallizations WHERE id='p-李明'").fetchone()["summary_json"])
    chapters = json.loads(engine.db().execute("SELECT chapters_json FROM crystallizations WHERE id='p-李明'").fetchone()["chapters_json"])
    assert summary["兴趣爱好"] == "尚无来源"
    assert chapters["个人生活"] == "暂无来源"
    assert any(
        call["crystal_id"] == "p-李明"
        and any("待核实" in str(op.get("value") or "") for op in call["ops"])
        for call in calls
    )

    unknown = "谁喜欢在黎明骑行。"
    other = _source(engine, "file", unknown, "file-unknown")
    missed = engine._normalize_extract(
        {
            "items": [
                {
                    "op": "field",
                    "field": "兴趣爱好",
                    "value": "黎明骑行",
                    "quote": unknown,
                    "crystal": "陈默",
                    "type": "人物",
                }
            ],
            "sheet": None,
        },
        unknown,
    )
    engine._apply_items(other, missed["items"], {}, missed["sheet"], unit_body=unknown)
    assert engine.db().execute("SELECT 1 FROM crystallizations WHERE name='陈默'").fetchone() is None
    assert engine.db().execute("SELECT 1 FROM statements WHERE quote=?", (unknown,)).fetchone() is None


def test_new_person_in_a_file_gets_a_page(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "_proper_name_ok", lambda *_args, **_kwargs: True)
    body = "李明喜欢在夜里骑行。"
    source = _source(engine, "file", body, "file-new-person")
    owner_sheet = engine._complete_sheet(
        [{"op": "field", "field": "兴趣爱好", "value": "夜里骑行", "quote": body, "crystal": "记忆主人", "type": "人物"}]
    )
    calls = _spy_apply_ops(monkeypatch)
    engine._apply_items(
        source,
        [{"op": "field", "field": "兴趣爱好", "value": "夜里骑行", "quote": body, "crystal": "李明", "trust": "written"}],
        {"李明": True},
        owner_sheet,
        unit_body=body,
    )
    person = engine.db().execute("SELECT id FROM crystallizations WHERE name='李明' AND status='active'").fetchone()
    owner = engine.db().execute(
        "SELECT 1 FROM statements WHERE crystal_id='owner' AND field='兴趣爱好'"
    ).fetchone()
    assert owner is None
    assert person is not None
    assert any(
        call["crystal_id"] == person["id"]
        and any(op.get("value") == "夜里骑行" for op in call["ops"])
        for call in calls
    )


def _seed_owner_real_name(engine, real_name: str) -> None:
    row = engine.db().execute(
        "SELECT summary_json, envelope_json FROM crystallizations WHERE id='owner'"
    ).fetchone()
    summary = json.loads(row["summary_json"] or "{}")
    summary["真名"] = real_name
    envelope = json.loads(row["envelope_json"] or "{}") if row["envelope_json"] else {}
    if not isinstance(envelope, dict):
        envelope = {}
    keys = dict(envelope.get("keys") or {})
    keys["真名"] = real_name
    envelope["keys"] = keys
    engine.db().execute(
        "UPDATE crystallizations SET summary_json=?, envelope_json=? WHERE id='owner'",
        (json.dumps(summary, ensure_ascii=False), json.dumps(envelope, ensure_ascii=False)),
    )
    engine.db().commit()


def test_file_does_not_treat_a_possessive_name_as_the_owner(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _seed_owner_real_name(engine, "韩跑跑")
    body = "韩跑跑的同事喜欢在夜里骑行。"
    source = _source(engine, "file", body, "file-colleague")
    data = engine._normalize_extract(
        {
            "items": [
                {"op": "field", "field": "兴趣爱好", "value": "夜里骑行", "quote": body, "crystal": "记忆主人", "type": "人物"}
            ],
            "sheet": None,
        },
        body,
    )
    engine._apply_items(source, data["items"], {}, data["sheet"], unit_body=body)
    stored = engine.db().execute(
        "SELECT text FROM statements WHERE crystal_id='owner' AND field='兴趣爱好'"
    ).fetchone()
    assert stored is None


def test_file_names_the_owner_when_the_true_name_is_in_the_quote(tmp_path, monkeypatch):
    engine = _engine(tmp_path, monkeypatch)
    _seed_owner_real_name(engine, "韩跑跑")
    body = "韩跑跑喜欢在夜里骑行。"
    source = _source(engine, "file", body, "file-owner-name")
    data = engine._normalize_extract(
        {
            "items": [
                {"op": "field", "field": "兴趣爱好", "value": "夜里骑行", "quote": body, "crystal": "记忆主人", "type": "人物"}
            ],
            "sheet": None,
        },
        body,
    )
    calls = _spy_apply_ops(monkeypatch)
    engine._apply_items(source, data["items"], {}, data["sheet"], unit_body=body)
    assert any(
        call["crystal_id"] == "owner"
        and any(op.get("value") == "夜里骑行" for op in call["ops"])
        for call in calls
    )
    stmt = engine.db().execute(
        "SELECT text FROM statements WHERE crystal_id='owner' AND field='兴趣爱好' AND invalid_at IS NULL"
    ).fetchone()
    assert stmt is not None and stmt["text"] == "夜里骑行"


def test_confirmed_real_home_import_posts_turns(tmp_path, monkeypatch):
    from installer.rogmem_installer import import_history

    monkeypatch.setattr("installer.rogmem_installer._real_user_home", lambda _home: True)
    session = tmp_path / ".hermes" / "sessions"
    session.mkdir(parents=True)
    (session / "session_ok.json").write_text(
        json.dumps(
            {
                "session_id": "real-1",
                "messages": [{"role": "user", "content": "真实主目录里的原句戊", "timestamp": "2026-10-04T02:00:00+00:00"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    seen = {}

    def post(turns):
        seen["n"] = len(turns)
        seen["text"] = turns[0]["text"]
        return {"ok": True}

    refused = import_history(tmp_path, confirm=False, post=post)
    posted = import_history(tmp_path, confirm=True, post=post)
    assert refused["refused"] == "real-home"
    assert refused["posted"] == 0
    assert posted["ok"] is True
    assert seen["text"] == "真实主目录里的原句戊"


def test_page_converts_timestamps_in_the_browser():
    html = page("结晶", "crystals", "<time datetime='2026-10-26T12:00:00+00:00'>2026-10-26T12:00:00+00:00</time>")
    assert "toLocaleString" in html
    assert "admin.password" not in html
