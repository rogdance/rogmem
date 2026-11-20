"""Model JSON is read the way the model actually writes it.

A long reply routinely carries a raw newline inside a string value instead of an
escaped ``\\n``. RFC-strict parsing rejects the whole reply with "Invalid control
character at line N column M", which drops a page that was otherwise complete and
sends the unit back for an identical retry. The reader tolerates that idiom while
still refusing a reply that is genuinely cut off.
"""

import json

import pytest

from rogmem import engine
from rogmem.engine import ModelCallFailed, _model_json, _parse_model_json


def test_a_raw_newline_inside_a_string_is_accepted():
    # The model wrapped the value across lines without escaping — the exact shape
    # that produced "Invalid control character" on a 200k-char page merge.
    text = '{"same":"是","page":"第一行\n第二行\n第三行"}'
    # The raw text is invalid JSON by RFC — which is exactly what the model sent.
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)
    data = _parse_model_json(text)
    assert data["page"] == "第一行\n第二行\n第三行"


def test_a_tab_control_character_inside_a_string_is_accepted():
    text = '{"items":[{"quote":"a\tb","value":"x"}]}'
    data = _parse_model_json(text)
    assert data["items"][0]["quote"] == "a\tb"


def test_a_truncated_reply_is_still_refused():
    # Stopped for room mid-string: no closing brace/quote. Must not be salvaged.
    text = '{"same":"是","page":"写了半句就停住'
    with pytest.raises(json.JSONDecodeError):
        _parse_model_json(text)


def test_a_fenced_json_reply_is_read():
    text = "```json\n{\"page\":\"正文\"}\n```"
    assert _parse_model_json(text)["page"] == "正文"


def test_json_after_prose_is_read_from_the_first_brace():
    text = '这里是结果：\n{"page":"正文",\n"gist":"摘要"}\n以上。'
    assert _parse_model_json(text)["gist"] == "摘要"


def test_a_non_object_reply_is_refused():
    with pytest.raises(ValueError):
        _parse_model_json("[1, 2, 3]")


def test_a_reply_that_stopped_for_room_is_never_a_result():
    # The reply parses, but finish_reason says the model ran out of room: the part
    # that was reached must not be accepted as the whole answer.
    content = '{"items":[{"quote":"只有前半段","value":"a"}]}'
    with pytest.raises(ModelCallFailed):
        _model_json(content, "length")


def test_a_finished_reply_is_read():
    assert _model_json('{"answer":"是"}', "stop")["answer"] == "是"


def test_a_missing_finish_reason_is_treated_as_finished():
    assert _model_json('{"answer":"否"}', "")["answer"] == "否"


def test_the_identity_question_refuses_a_reply_that_stopped_for_room(monkeypatch):
    # _ask_model is the other reader of a model JSON reply. A truncated answer that
    # happens to parse must not be taken as the model's whole answer here either.
    monkeypatch.setattr(engine, "_judge_model", lambda role: ("base", "key", "model"))
    monkeypatch.setattr(
        engine, "_complete_chat", lambda *a, **k: ('{"answer":"是","why":"写到一半', "length")
    )
    with pytest.raises(ModelCallFailed):
        engine._ask_model("merge", "系统", "问题")


def test_a_name_check_that_stopped_for_room_is_not_a_yes(monkeypatch):
    # A proper-name check reads the same kind of reply. Stopped-for-room must fall to
    # the safe side (not a name), never a partial "是" that would merge two entities.
    monkeypatch.setattr(engine, "llm_target", lambda role: ("base", "key", "model"))
    monkeypatch.setattr(
        engine, "_complete_chat", lambda *a, **k: ('{"answer":"是"', "length")
    )
    assert engine._proper_name_ok("甲", "人物", "引文") is False


