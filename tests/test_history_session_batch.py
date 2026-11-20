"""历史会话：近的先导入，一场合成一个问答萃取。"""

from __future__ import annotations

from pathlib import Path

from rogmem.exchange import Side, form_exchanges, join_exchange_text


def _side(
    source_id: str,
    kind: str,
    *,
    session_id: str = "s",
    turn_id: str = "",
    event_at: str = "",
    rowid: int = 0,
    lane: str = "p1",
    chars: int = 20,
    qid: int = 0,
    pair_ready: int = 1,
    wait_until: str = "",
) -> Side:
    return Side(
        source_id=source_id,
        session_id=session_id,
        turn_id=turn_id or f"{session_id}:{source_id}",
        kind=kind,
        event_at=event_at,
        ingested_at=event_at,
        rowid=rowid,
        pair_ready=pair_ready,
        wait_until=wait_until,
        qid=qid or rowid,
        lane=lane,
        chars=chars,
    )


def _ids(groups) -> list[list[str]]:
    return [[side.source_id for side in group.sides] for group in groups]


def test_a_history_session_is_one_ask_and_answer_dialogue():
    groups = form_exchanges(
        [
            _side("u1", "user", event_at="2026-01-01T00:00:00+00:00", rowid=1),
            _side("a1", "agent", event_at="2026-01-01T00:00:01+00:00", rowid=2),
            _side("u2", "user", event_at="2026-01-01T00:00:02+00:00", rowid=3),
            _side("a2", "agent", event_at="2026-01-01T00:00:03+00:00", rowid=4),
        ]
    )
    assert _ids(groups) == [["u1", "a1", "u2", "a2"]]
    text = join_exchange_text(
        [
            {"id": "a2", "kind": "agent", "body": "合成句：后答。", "event_at": "2026-01-01T00:00:03+00:00", "rowid": 4},
            {"id": "u1", "kind": "user", "body": "合成句：先问。", "event_at": "2026-01-01T00:00:00+00:00", "rowid": 1},
            {"id": "a1", "kind": "agent", "body": "合成句：先答。", "event_at": "2026-01-01T00:00:01+00:00", "rowid": 2},
            {"id": "u2", "kind": "user", "body": "合成句：后问。", "event_at": "2026-01-01T00:00:02+00:00", "rowid": 3},
        ]
    )
    assert text.index("合成句：先问。") < text.index("合成句：先答。") < text.index("合成句：后问。") < text.index("合成句：后答。")
    assert "用户\n合成句：先问。" in text
    assert "助手\n合成句：后答。" in text


def test_a_live_session_still_closes_each_round():
    groups = form_exchanges(
        [
            _side("u1", "user", lane="p0", event_at="2026-01-01T00:00:00+00:00", rowid=1),
            _side("a1", "agent", lane="p0", event_at="2026-01-01T00:00:01+00:00", rowid=2),
            _side("u2", "user", lane="p0", event_at="2026-01-01T00:00:02+00:00", rowid=3),
            _side("a2", "agent", lane="p0", event_at="2026-01-01T00:00:03+00:00", rowid=4),
        ]
    )
    assert _ids(groups) == [["u1", "a1"], ["u2", "a2"]]


def test_a_history_session_splits_only_between_rounds_when_it_is_too_large():
    from rogmem.exchange import SESSION_DISTILL_CHARS

    half = SESSION_DISTILL_CHARS // 2 + 1
    groups = form_exchanges(
        [
            _side("u1", "user", event_at="2026-01-01T00:00:00+00:00", rowid=1, chars=half),
            _side("a1", "agent", event_at="2026-01-01T00:00:01+00:00", rowid=2, chars=half),
            _side("u2", "user", event_at="2026-01-01T00:00:02+00:00", rowid=3, chars=half),
            _side("a2", "agent", event_at="2026-01-01T00:00:03+00:00", rowid=4, chars=half),
        ]
    )
    assert _ids(groups) == [["u2", "a2"], ["u1", "a1"]] or _ids(groups) == [["u1", "a1"], ["u2", "a2"]]
    newest = next(group for group in groups if group.sides[0].source_id == "u2")
    assert [side.source_id for side in newest.sides] == ["u2", "a2"]
    huge = form_exchanges(
        [
            _side("big-u", "user", event_at="2026-02-01T00:00:00+00:00", rowid=1, chars=SESSION_DISTILL_CHARS + 10),
            _side("big-a", "agent", event_at="2026-02-01T00:00:01+00:00", rowid=2, chars=10),
        ]
    )
    assert _ids(huge) == [["big-u", "big-a"]]


def test_history_order_posts_the_newer_session_and_its_later_lines_first():
    from rogmem.exchange import order_history_newest_first

    ordered = order_history_newest_first(
        [
            {"session_id": "old", "event_at": "2020-01-01T00:00:00+00:00", "seq": 1, "text": "合成句：旧问。"},
            {"session_id": "old", "event_at": "2020-01-01T00:00:01+00:00", "seq": 2, "text": "合成句：旧答。"},
            {"session_id": "new", "event_at": "2026-05-01T00:00:00+00:00", "seq": 1, "text": "合成句：新问。"},
            {"session_id": "new", "event_at": "2026-05-01T00:00:01+00:00", "seq": 2, "text": "合成句：新答。"},
        ]
    )
    assert [item["text"] for item in ordered] == [
        "合成句：新答。",
        "合成句：新问。",
        "合成句：旧答。",
        "合成句：旧问。",
    ]


def _open(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_TURN_WAIT", "600")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _pair(session_id: str, ask_at: str, answer_at: str, ask: str, answer: str, seq: int) -> list[dict]:
    return [
        {
            "role": "user",
            "text": ask,
            "event_at": ask_at,
            "session_id": session_id,
            "turn_id": f"{session_id}:u{seq}",
            "seq": seq,
        },
        {
            "role": "assistant",
            "text": answer,
            "event_at": answer_at,
            "session_id": session_id,
            "turn_id": f"{session_id}:a{seq}",
            "seq": seq + 1,
        },
    ]


def test_newer_history_is_leased_before_older_history(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.ingest_history(
        _pair(
            "old",
            "2020-01-01T00:00:00+00:00",
            "2020-01-01T00:00:01+00:00",
            "合成句：旧场的问。",
            "合成句：旧场的答。",
            1,
        )
        + _pair(
            "new",
            "2026-06-01T00:00:00+00:00",
            "2026-06-01T00:00:01+00:00",
            "合成句：新场的问。",
            "合成句：新场的答。",
            1,
        )
    )
    leased = engine.lease_next()
    assert leased is not None
    bodies = [
        row["body"]
        for row in engine.db().execute(
            "SELECT body FROM sources WHERE id IN ({})".format(",".join("?" * len(leased["source_ids"]))),
            leased["source_ids"],
        )
    ]
    assert any("新场的问" in body for body in bodies)
    assert all("旧场" not in body for body in bodies)


def test_one_history_session_is_one_distill_call(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    engine.ingest_history(
        [
            {
                "role": "user",
                "text": "合成句：这场先问。",
                "event_at": "2026-03-01T00:00:00+00:00",
                "session_id": "chat",
                "turn_id": "chat:u1",
                "seq": 1,
            },
            {
                "role": "assistant",
                "text": "合成句：这场先答。",
                "event_at": "2026-03-01T00:00:01+00:00",
                "session_id": "chat",
                "turn_id": "chat:a1",
                "seq": 2,
            },
            {
                "role": "user",
                "text": "合成句：这场后问。",
                "event_at": "2026-03-01T00:00:02+00:00",
                "session_id": "chat",
                "turn_id": "chat:u2",
                "seq": 3,
            },
            {
                "role": "assistant",
                "text": "合成句：这场后答。",
                "event_at": "2026-03-02T00:00:03+00:00",
                "session_id": "chat",
                "turn_id": "chat:a2",
                "seq": 4,
            },
        ]
    )
    calls: list[str] = []

    def fake(source, qid):
        del qid
        calls.append(engine._material_for_distill(source))
        return True

    monkeypatch.setattr(engine, "_distill", fake)
    assert engine.pump_one()
    assert engine.pump_one() is None
    assert len(calls) == 1
    text = calls[0]
    assert text.index("这场先问") < text.index("这场先答") < text.index("这场后问") < text.index("这场后答")
    assert "用户\n合成句：这场先问。" in text
    assert "助手\n合成句：这场后答。" in text


def test_a_history_batch_clock_is_the_latest_speech_and_quotes_the_whole_session(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    imported = engine.ingest_history(
        [
            {
                "role": "user",
                "text": "合成句：用户只说了前半。",
                "event_at": "2024-01-01T00:00:00+00:00",
                "event_basis": "utterance",
                "session_id": "batch",
                "turn_id": "batch:u",
                "seq": 1,
            },
            {
                "role": "assistant",
                "text": "合成句：助手把后半段说完。",
                "event_at": "2026-08-01T00:00:00+00:00",
                "event_basis": "utterance",
                "session_id": "batch",
                "turn_id": "batch:a",
                "seq": 2,
            },
        ]
    )
    assert imported["ok"] is True
    seen: list[str] = []

    def fake_complete(_base, _key, payload, **_kwargs):
        seen.append(payload["messages"][1]["content"])
        return "# 记忆主人\n类型：人物\n\n记下后半段。[^1]\n\n[^1]: 实 「合成句：助手把后半段说完。」\n", "stop"

    monkeypatch.setattr(engine, "llm_target", lambda role: ("http://stub/v1", "k", "m"))
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    monkeypatch.setattr(engine, "_land_markdown_pages", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "_remember_draft", lambda *args, **kwargs: None)
    assert engine.pump_one()
    draft = next(item for item in seen if "材料类型：" in item)
    assert "事件时间：2026-08-01T00:00:00+00:00" in draft
    assert "说话人：记忆主人" in draft
    assert "合成句：助手把后半段说完。" in draft
    user = engine.db().execute("SELECT * FROM sources WHERE turn_id='batch:u'").fetchone()
    agent = engine.db().execute("SELECT id FROM sources WHERE turn_id='batch:a'").fetchone()
    token = engine._EXCHANGE.set(({"qid": 1, "source_id": user["id"]}, {"qid": 2, "source_id": agent["id"]}))
    try:
        page = {
            "name": "北窗",
            "type": "概念",
            "body": "记下。[^1]\n\n[^1]: 实 「合成句：助手把后半段说完。」",
        }
        engine._merge_one_page(user, page, {"old": "", "id": "", "at": "", "name": "", "type": ""})
    finally:
        engine._EXCHANGE.reset(token)
    assert f"/admin/sources/{user['id']}" in page["body"]


def test_newer_means_the_later_instant_not_the_spelling_of_the_offset():
    from rogmem.exchange import order_history_newest_first

    ordered = order_history_newest_first(
        [
            {"session_id": "s", "event_at": "2026-01-01T20:00:00+00:00", "text": "合成句：世界时这一句。"},
            {"session_id": "s", "event_at": "2026-01-01T13:00:00-08:00", "text": "合成句：西岸更晚的一句。"},
        ]
    )
    assert [item["text"] for item in ordered] == ["合成句：西岸更晚的一句。", "合成句：世界时这一句。"]


def test_upload_batches_do_not_cut_a_session():
    from rogmem.exchange import chunk_whole_sessions

    short = [{"session_id": "a", "n": i} for i in range(3)] + [{"session_id": "b", "n": i} for i in range(3)]
    batches = chunk_whole_sessions(short, 4)
    assert [[item["session_id"] for item in batch] for batch in batches] == [["a", "a", "a"], ["b", "b", "b"]]
    long = [{"session_id": "long", "n": i} for i in range(5)]
    assert chunk_whole_sessions(long, 4) == [long]


def test_a_held_history_session_is_not_ready_until_it_is_released():
    from datetime import datetime, timezone

    from rogmem.exchange import exchange_ready

    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    held = form_exchanges(
        [
            _side("u", "user", pair_ready=0, wait_until="hold:job-1", event_at="2026-01-01T00:00:00+00:00", rowid=1),
            _side("a", "agent", pair_ready=0, wait_until="hold:job-1", event_at="2026-01-01T00:00:01+00:00", rowid=2),
        ]
    )
    assert exchange_ready(held[0], now) is False
    released = form_exchanges(
        [
            _side("u", "user", pair_ready=1, event_at="2026-01-01T00:00:00+00:00", rowid=1),
            _side("a", "agent", pair_ready=1, event_at="2026-01-01T00:00:01+00:00", rowid=2),
        ]
    )
    assert exchange_ready(released[0], now) is True


def test_a_split_job_upload_distills_the_whole_session_only_after_it_finishes(tmp_path, monkeypatch):
    import hashlib
    import secrets

    engine = _open(tmp_path, monkeypatch)
    agent_id = secrets.token_hex(8)
    token = "rm_" + secrets.token_urlsafe(8)
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO agents
           (id, device_id, device_name, program, display_name, status, pubkey, key_hash,
            created_at, connected_at, call_count, capabilities)
           VALUES(?,?,?,?,?,?,?,?,?,?,0,?)""",
        (
            agent_id,
            "dev-hold",
            "dev-hold",
            "Cursor",
            "dev-hold · Cursor",
            "启用",
            "pk",
            hashlib.sha256(token.encode()).hexdigest(),
            now,
            now,
            "host_import_jobs:v1",
        ),
    )
    engine.db().commit()
    created = engine.create_host_import_job(agent_id=agent_id, program="Cursor", request_id="hold-1")
    assert created["ok"]
    job = engine.claim_host_import_jobs(agent_id=agent_id)[0]
    later = _pair(
        "chat",
        "2026-06-01T00:00:02+00:00",
        "2026-06-01T00:00:03+00:00",
        "合成句：后到的问。",
        "合成句：后到的答。",
        3,
    )
    earlier = _pair(
        "chat",
        "2026-06-01T00:00:00+00:00",
        "2026-06-01T00:00:01+00:00",
        "合成句：先说的问。",
        "合成句：先说的答。",
        1,
    )
    first = engine.ingest_history_for_job(
        agent_id=agent_id,
        job_id=job["id"],
        lease_token=job["lease_token"],
        program="Cursor",
        turns=later,
    )
    assert first["ok"] and first["posted"] == 2
    assert engine.lease_next() is None
    second = engine.ingest_history_for_job(
        agent_id=agent_id,
        job_id=job["id"],
        lease_token=job["lease_token"],
        program="Cursor",
        turns=earlier,
    )
    assert second["ok"] and second["posted"] == 2
    assert engine.lease_next() is None
    acked = engine.ack_host_import_job(
        agent_id=agent_id,
        job_id=job["id"],
        lease_token=job["lease_token"],
        status="completed",
    )
    assert acked["ok"]
    leased = engine.lease_next()
    assert leased is not None
    assert len(leased["source_ids"]) == 4
    assert engine.lease_next() is None


def _enable_cursor(engine, agent_id: str, device_name: str) -> None:
    import hashlib
    import secrets

    now = engine.utcnow()
    token = "rm_" + secrets.token_urlsafe(8)
    engine.db().execute(
        """INSERT INTO agents
           (id, device_id, device_name, program, display_name, status, pubkey, key_hash,
            created_at, connected_at, call_count, capabilities)
           VALUES(?,?,?,?,?,?,?,?,?,?,0,?)""",
        (
            agent_id,
            "dev-" + agent_id,
            device_name,
            "Cursor",
            f"{device_name} · Cursor",
            "启用",
            "pk-" + agent_id,
            hashlib.sha256(token.encode()).hexdigest(),
            now,
            now,
            "host_import_jobs:v1",
        ),
    )
    engine.db().commit()


def test_a_job_stores_the_lines_before_any_embedding_and_counts_them_on_that_agent(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    _enable_cursor(engine, "local-agent", "书房的电脑")
    _enable_cursor(engine, "other-agent", "另一台电脑")
    calls: list[int] = []
    real_plan = engine._planned_chunks

    def wrapped(*args, **kwargs):
        calls.append(1)
        return real_plan(*args, **kwargs)

    monkeypatch.setattr(engine, "_planned_chunks", wrapped)
    created = engine.create_host_import_job(agent_id="local-agent", program="Cursor", request_id="store-1")
    job = engine.claim_host_import_jobs(agent_id="local-agent")[0]
    stored = engine.ingest_history_for_job(
        agent_id="local-agent",
        job_id=job["id"],
        lease_token=job["lease_token"],
        program="Cursor",
        turns=_pair(
            "chat",
            "2026-06-01T00:00:00+00:00",
            "2026-06-01T00:00:01+00:00",
            "合成句：先落库的问。",
            "合成句：先落库的答。",
            1,
        ),
        device_name="",
    )
    assert created["ok"] and stored["ok"] and stored["posted"] == 2
    assert calls == []
    assert engine.db().execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    counts = {row["id"]: row["history_count"] for row in engine.list_connected_hosts()["rows"]}
    assert counts["local-agent"] == 2
    assert counts["other-agent"] == 0
    assert engine.lease_next() is None
    assert calls == []
    acked = engine.ack_host_import_job(
        agent_id="local-agent",
        job_id=job["id"],
        lease_token=job["lease_token"],
        status="completed",
    )
    assert acked["ok"]
    leased = engine.lease_next()
    assert leased is not None and len(leased["source_ids"]) == 2
    assert calls


def test_a_later_upload_claims_lines_that_never_named_a_host(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    turns = _pair(
        "chat",
        "2026-06-01T00:00:00+00:00",
        "2026-06-01T00:00:01+00:00",
        "合成句：先前没挂上宿主的问。",
        "合成句：先前没挂上宿主的答。",
        1,
    )
    first = engine.ingest_history(turns, program="Cursor", device_name="")
    assert first["ok"] and first["duplicates"] == 0
    assert engine.db().execute("SELECT COUNT(*) FROM sources WHERE IFNULL(agent_id,'')=''").fetchone()[0] == 2
    _enable_cursor(engine, "local-agent", "书房的电脑")
    _enable_cursor(engine, "other-agent", "另一台电脑")
    created = engine.create_host_import_job(agent_id="local-agent", program="Cursor", request_id="claim-1")
    job = engine.claim_host_import_jobs(agent_id="local-agent")[0]
    again = engine.ingest_history_for_job(
        agent_id="local-agent",
        job_id=job["id"],
        lease_token=job["lease_token"],
        program="Cursor",
        turns=turns,
        device_name="",
    )
    assert created["ok"] and again["ok"] and again["posted"] == 0 and again["duplicates"] == 2
    counts = {row["id"]: row["history_count"] for row in engine.list_connected_hosts()["rows"]}
    assert counts["local-agent"] == 2
    assert counts["other-agent"] == 0


def test_newer_session_files_are_readable_before_older_ones(tmp_path):
    import json
    import os

    from installer.rogmem_installer import read_program_history

    root = tmp_path / ".cursor" / "projects" / "demo"
    root.mkdir(parents=True)
    older = root / "old-chat.jsonl"
    newer = root / "new-chat.jsonl"

    def line(role: str, text: str, stamp: str) -> str:
        return json.dumps(
            {"type": "message", "role": role, "content": text, "timestamp": stamp},
            ensure_ascii=False,
        )

    older.write_text(
        line("user", "合成句：旧文件的问。", "2020-01-01T00:00:00Z")
        + "\n"
        + line("assistant", "合成句：旧文件的答。", "2020-01-01T00:00:01Z")
        + "\n",
        encoding="utf-8",
    )
    newer.write_text(
        line("user", "合成句：新文件的问。", "2026-06-01T00:00:00Z")
        + "\n"
        + line("assistant", "合成句：新文件的答。", "2026-06-01T00:00:01Z")
        + "\n",
        encoding="utf-8",
    )
    os.utime(older, (10, 1_000))
    os.utime(newer, (10, 2_000))
    hermes = tmp_path / ".hermes" / "sessions"
    hermes.mkdir(parents=True)
    (hermes / "session_side.json").write_text('{"role":"user","content":"合成句：不该被 Cursor 读到。"}', encoding="utf-8")
    sessions = list(read_program_history(tmp_path, "Cursor", allow_real=True))
    assert [item[0]["session_id"] for item in sessions] == ["new-chat", "old-chat"]
    assert all("不该被 Cursor 读到" not in turn["text"] for session in sessions for turn in session)
