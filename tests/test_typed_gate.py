"""Typed yes/no gates accept an enum, not a chat JSON object."""

from __future__ import annotations

import pytest

from rogmem import engine
from rogmem.engine import ModelCallFailed
from rogmem.prompts import MERGE_ENTITY_SYSTEM, PROPER_NAME_SYSTEM, worker_prompts
from rogmem.typed_gate import parse_yes_no


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("是", "是"),
        ("否", "否"),
        ("  是。", "是"),
        ("「否」", "否"),
        ('{"answer":"是"}', "是"),
        ('```json\n{"answer":"否"}\n```', "否"),
        ('结果：{"answer":"是"}', "是"),
    ],
)
def test_parse_yes_no_accepts_bare_and_legacy_json(raw, expected):
    assert parse_yes_no(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "也许",
        "是的",
        '{"answer":"maybe"}',
        '{"same":"是"}',
        '["是"]',
    ],
)
def test_parse_yes_no_refuses_non_enum(raw):
    with pytest.raises(ValueError):
        parse_yes_no(raw)


def test_gate_prompts_ask_for_bare_enum_not_json():
    prompts = worker_prompts(sketch_limit=3500)
    for key in ("merge-entity", "proper-name"):
        text = prompts[key]
        assert "「是」或「否」" in text
        assert "不要 JSON" in text
        assert '{"answer"' not in text
    assert "不要 JSON" in MERGE_ENTITY_SYSTEM
    assert "不要 JSON" in PROPER_NAME_SYSTEM
    assert "name，name 用那一条" not in MERGE_ENTITY_SYSTEM


def test_identity_question_reads_bare_yes(monkeypatch):
    monkeypatch.setattr(engine, "_judge_model", lambda role: ("base", "key", "model"))
    monkeypatch.setattr(engine, "_complete_chat", lambda *a, **k: ("是", "stop"))
    assert engine._ask_same_entity("甲", "乙", [], "") == "是"


def test_identity_question_refuses_unfinished_reply(monkeypatch):
    monkeypatch.setattr(engine, "_judge_model", lambda role: ("base", "key", "model"))
    monkeypatch.setattr(engine, "_complete_chat", lambda *a, **k: ("是", "length"))
    with pytest.raises(ModelCallFailed):
        engine._ask_yes_no("merge", "系统", "问题")


def test_identity_question_refuses_prose(monkeypatch):
    monkeypatch.setattr(engine, "_judge_model", lambda role: ("base", "key", "model"))
    monkeypatch.setattr(engine, "_complete_chat", lambda *a, **k: ("同一主体", "stop"))
    with pytest.raises(RuntimeError, match="结论无效"):
        engine._ask_same_entity("甲", "乙", [], "")


def test_name_check_accepts_bare_yes(monkeypatch):
    monkeypatch.setattr(engine, "llm_target", lambda role: ("base", "key", "model"))
    monkeypatch.setattr(engine, "_complete_chat", lambda *a, **k: ("是", "stop"))
    assert engine._proper_name_ok("甲", "人物", "引文") is True


def test_name_check_that_stopped_for_room_is_not_a_yes(monkeypatch):
    monkeypatch.setattr(engine, "llm_target", lambda role: ("base", "key", "model"))
    monkeypatch.setattr(engine, "_complete_chat", lambda *a, **k: ("是", "length"))
    assert engine._proper_name_ok("甲", "人物", "引文") is False


def test_name_check_legacy_json_still_works(monkeypatch):
    monkeypatch.setattr(engine, "llm_target", lambda role: ("base", "key", "model"))
    monkeypatch.setattr(
        engine, "_complete_chat", lambda *a, **k: ('{"answer":"否"}', "stop")
    )
    assert engine._proper_name_ok("甲", "人物", "引文") is False


def test_yes_no_gate_caps_max_tokens(monkeypatch):
    seen: dict = {}

    def fake_complete(base, key, payload, **kwargs):
        seen["max_tokens"] = payload.get("max_tokens")
        return "否", "stop"

    monkeypatch.setattr(engine, "_judge_model", lambda role: ("base", "key", "model"))
    monkeypatch.setattr(engine, "_chat_max_tokens", lambda *a, **k: 8000)
    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    assert engine._ask_yes_no("merge", "系统", "问题") == "否"
    assert seen["max_tokens"] == engine._TYPED_YES_NO_MAX_TOKENS
