"""Pre-model triage: units with nothing new for long-term memory never reach the model.

Every sentence here is synthetic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from rogmem import distill_triage as dt

LONG_TASK = (
    "合成例行任务：请每小时巡检一次虚构的星河花园留言板，"
    "逐条读出新留言，判断是否需要回复，若无新增则只报告本轮无新增。"
) * 5
REPLY = "合成回复：本轮巡检完成，星河花园留言板无新增反馈，只读零写入，共核对四十条留言。"
REPLY_NEAR = "合成回复：本轮巡检完成，星河花园留言板无新增反馈，只读零写入，共核对四十一条留言。"
REPLY_FAR = "合成回复：发现三条新留言，其中一条提到下周三把蓝色木门漆成橙色，已记下待办并回复了作者。"


def _at(hours: float) -> str:
    base = datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)
    return (base + timedelta(hours=hours)).isoformat()


def _history(decisions: list[tuple[str, str, str]]) -> list[dict]:
    return [{"decision": d, "agent_sample": s, "unit_at": a} for d, s, a in decisions]


# ---------- pure functions ----------


def test_normalize_collapses_whitespace_and_drops_program_headers():
    text = "【rogmem 历史包】\n合成句  甲\n\n\t乙  \n【rogmem 插件】请更新\n丙"
    assert dt.normalize_material(text) == "合成句 甲 乙 丙"
    assert dt.unit_hash("合成句 甲") == dt.unit_hash("  合成句\n甲 ")
    assert dt.unit_hash("合成句 甲") != dt.unit_hash("合成句 乙")


def test_template_key_needs_a_long_user_side():
    assert dt.template_key("短") == ""
    assert dt.template_key("字" * (dt.TEMPLATE_MIN_USER_CHARS - 1)) == ""
    assert dt.template_key("字" * dt.TEMPLATE_MIN_USER_CHARS)
    assert dt.template_key(LONG_TASK) == dt.template_key("\n " + LONG_TASK + "\t")


def test_jaccard_bounds_and_similarity():
    assert dt.jaccard("", "") == 1.0
    assert dt.jaccard("合成句", "") == 0.0
    assert dt.jaccard(REPLY, REPLY) == 1.0
    assert dt.jaccard(REPLY, REPLY_NEAR) >= dt.ROUTINE_AGENT_JACCARD
    assert dt.jaccard(REPLY, REPLY_FAR) < dt.ROUTINE_AGENT_JACCARD


def test_host_error_reply_is_a_closed_list():
    assert dt.is_host_error_reply("Authentication required. Please use /login command to sign in to your account")
    assert dt.is_host_error_reply("API rate limit exceeded\nToo many requests.")
    assert dt.is_host_error_reply("API Error: 429 {\"type\":\"error\"}")
    assert not dt.is_host_error_reply("")
    assert not dt.is_host_error_reply("Authentication required. 合成句：顺便我把橙色木门的事记下了。")
    assert not dt.is_host_error_reply("合成句：登录页面的按钮颜色改成了绿色。")


def test_r1_duplicate_skips_even_for_files():
    common = dict(user_text="合成句", agent_text="合成回复", unit_at=_at(0), template_history=[])
    assert dt.decide(is_file=False, duplicate_of_distilled=True, **common) == dt.REASON_DUPLICATE
    assert dt.decide(is_file=True, duplicate_of_distilled=True, **common) == dt.REASON_DUPLICATE
    assert dt.decide(is_file=False, duplicate_of_distilled=False, **common) == ""


def _routine(agent: str, unit_at: str, history: list[dict], *, is_file: bool = False) -> str:
    return dt.decide(
        is_file=is_file,
        user_text=LONG_TASK,
        agent_text=agent,
        unit_at=unit_at,
        duplicate_of_distilled=False,
        template_history=history,
    )


def test_r2_routine_repeat_skips_only_with_enough_recent_similar_precedent():
    three = _history([("distilled", REPLY, _at(0)), ("skipped", REPLY, _at(1)), ("skipped", REPLY, _at(2))])
    assert _routine(REPLY_NEAR, _at(3), three) == dt.REASON_ROUTINE
    # only two earlier units
    assert _routine(REPLY_NEAR, _at(3), three[:2]) == ""
    # reply diverges from the last distilled one
    assert _routine(REPLY_FAR, _at(3), three) == ""
    # last distilled one is a day old
    assert _routine(REPLY_NEAR, _at(dt.ROUTINE_WINDOW_HOURS), three) == ""
    assert _routine(REPLY_NEAR, _at(dt.ROUTINE_WINDOW_HOURS - 0.01), three) == dt.REASON_ROUTINE
    # none of the precedents went to the model
    skipped = _history([("skipped", REPLY, _at(0))] * 3)
    assert _routine(REPLY_NEAR, _at(1), skipped) == ""
    # pending rows are not precedents
    pending = _history([("distilled", REPLY, _at(0)), ("pending", REPLY, _at(1)), ("pending", REPLY, _at(2))])
    assert _routine(REPLY_NEAR, _at(3), pending) == ""
    # files are never routine
    assert _routine(REPLY_NEAR, _at(3), three, is_file=True) == ""


def test_r2_compares_with_the_most_recent_distilled_reply():
    history = _history(
        [("distilled", REPLY_FAR, _at(0)), ("distilled", REPLY, _at(2)), ("skipped", REPLY, _at(3))]
    )
    assert _routine(REPLY_NEAR, _at(4), history) == dt.REASON_ROUTINE
    flipped = _history(
        [("distilled", REPLY, _at(0)), ("distilled", REPLY_FAR, _at(2)), ("skipped", REPLY, _at(3))]
    )
    assert _routine(REPLY_NEAR, _at(4), flipped) == ""


def test_r3_host_error_only_for_short_user_side():
    error = "Authentication required. Please use /login command to sign in to your account"
    kw = dict(unit_at=_at(0), duplicate_of_distilled=False, template_history=[])
    assert dt.decide(is_file=False, user_text="合成句：帮我看看。", agent_text=error, **kw) == dt.REASON_HOST_ERROR
    assert dt.decide(is_file=False, user_text=LONG_TASK, agent_text=error, **kw) == ""
    assert dt.decide(is_file=False, user_text="合成句：帮我看看。", agent_text=REPLY, **kw) == ""
    assert dt.decide(is_file=True, user_text="", agent_text=error, **kw) == ""


# ---------- engine ----------


def _open(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_TURN_WAIT", "600")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    calls: list[str] = []

    def fake_payload(text, kind="", **_kw):
        calls.append(text)
        return {"payload_kind": "markdown", "pages": []}

    monkeypatch.setattr(engine, "_produce_distill_payload", fake_payload)
    monkeypatch.setattr(engine, "_land_markdown_pages", lambda *_a, **_k: None)
    return engine, calls


def _unit(engine, session: str, user: str, agent: str, hours: float) -> None:
    imported = engine.ingest_history(
        [
            {"role": "user", "text": user, "event_at": _at(hours), "session_id": session, "turn_id": f"{session}:u", "seq": 1},
            {
                "role": "assistant",
                "text": agent,
                "event_at": _at(hours + 0.001),
                "session_id": session,
                "turn_id": f"{session}:a",
                "seq": 2,
            },
        ]
    )
    assert imported["ok"] is True


def _pump(engine) -> str:
    done = engine.pump_one()
    assert done
    assert engine.pump_one() is None
    return done


def _fingerprint(engine, source_id: str):
    return engine.db().execute("SELECT * FROM distill_fingerprints WHERE source_id=?", (source_id,)).fetchone()


def test_duplicate_unit_makes_no_model_call_and_finishes(tmp_path, monkeypatch):
    engine, calls = _open(tmp_path, monkeypatch)
    _unit(engine, "s1", "合成句：把橙色木门的尺寸记下来。", "合成回复：已记下，宽九十厘米。", 0)
    first = _pump(engine)
    assert len(calls) == 1
    assert _fingerprint(engine, first)["decision"] == "distilled"

    _unit(engine, "s2", "合成句：把橙色木门的尺寸记下来。", "合成回复：已记下，宽九十厘米。", 1)
    second = _pump(engine)
    assert len(calls) == 1
    row = _fingerprint(engine, second)
    assert row["decision"] == "skipped" and row["reason"] == dt.REASON_DUPLICATE
    states = [r["state"] for r in engine.db().execute("SELECT state FROM queue ORDER BY id")]
    assert states == ["done"] * 4
    for source in engine.db().execute("SELECT * FROM sources WHERE session_id='s2'"):
        assert source["status"] == engine.ST_NO_CRYSTAL
        assert not source["last_error"]
        assert "完成:ok" in (source["pipeline"] or "")
    note = engine.db().execute("SELECT summary, outcome FROM run_log WHERE ref_id=?", (second,)).fetchone()
    assert note["outcome"] == "跳过"
    assert note["summary"] == "萃取跳过：重复材料"


def test_failed_attempt_is_not_a_precedent(tmp_path, monkeypatch):
    engine, calls = _open(tmp_path, monkeypatch)

    def boom(text, kind="", **_kw):
        calls.append(text)
        raise RuntimeError("合成故障")

    monkeypatch.setattr(engine, "_produce_distill_payload", boom)
    _unit(engine, "s1", "合成句：把橙色木门的尺寸记下来。", "合成回复：已记下。", 0)
    first = engine.pump_one()
    assert _fingerprint(engine, first)["decision"] == "pending"
    _unit(engine, "s2", "合成句：把橙色木门的尺寸记下来。", "合成回复：已记下。", 1)
    engine.db().execute("UPDATE queue SET next_at='2000-01-01T00:00:00+00:00'")
    engine.db().commit()
    for _ in range(3):
        engine.pump_one()
    assert len(calls) >= 2
    assert not engine.db().execute("SELECT 1 FROM distill_fingerprints WHERE decision='skipped'").fetchone()


def test_routine_repeat_within_a_day_is_skipped_but_a_divergent_reply_is_distilled(tmp_path, monkeypatch):
    engine, calls = _open(tmp_path, monkeypatch)
    outcomes = []
    def near(count: str) -> str:
        return REPLY.replace("四十条", count)

    plan = [
        (0, near("三十一条")),
        (1, near("三十二条")),
        (2, near("三十三条")),
        (3, near("三十四条")),  # three earlier, last distilled 1h ago, similar -> skip
        (4, REPLY_FAR),  # diverges -> distill
        (30, near("三十五条")),  # last distilled 26h ago -> distill
    ]
    for index, (hours, reply) in enumerate(plan):
        _unit(engine, f"r{index}", LONG_TASK, reply, hours)
        lead = _pump(engine)
        outcomes.append(_fingerprint(engine, lead)["decision"])
    assert outcomes == ["distilled", "distilled", "distilled", "skipped", "distilled", "distilled"]
    assert len(calls) == 5


def test_host_error_reply_is_skipped(tmp_path, monkeypatch):
    engine, calls = _open(tmp_path, monkeypatch)
    _unit(
        engine,
        "e1",
        "合成句：帮我看看留言板。",
        "Authentication required. Please use /login command to sign in to your account",
        0,
    )
    lead = _pump(engine)
    assert calls == []
    assert _fingerprint(engine, lead)["reason"] == dt.REASON_HOST_ERROR


def test_setting_off_disables_the_gate(tmp_path, monkeypatch):
    engine, calls = _open(tmp_path, monkeypatch)
    engine.set_setting("distill_triage", "0")
    engine.db().commit()
    for index in range(2):
        _unit(engine, f"o{index}", "合成句：把橙色木门的尺寸记下来。", "合成回复：已记下。", index)
        _pump(engine)
    _unit(engine, "o-err", "合成句：再看一次。", "Authentication required.", 3)
    _pump(engine)
    assert len(calls) == 3
    assert not engine.db().execute("SELECT 1 FROM distill_fingerprints").fetchone()
