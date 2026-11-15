"""Red-team corpus and wiring for systemic secret redaction."""

from __future__ import annotations

import json
from pathlib import Path

from rogmem.redact import SAMPLES, find_secrets, redact_text


def test_corpus_detects_required_shapes():
    required = {
        "AKIA" + "0" * 16,
        "eyJhbGciOiJIUzI1NiJ9.e30.sig",
        "Bearer aaa.bbb.ccc",
        "password=hunter2",
        "sk-abc123DEF456",
        "普通中文句子没有密钥",
    }
    labels = {sample for sample, _expect in SAMPLES}
    assert required <= labels
    for sample, expect in SAMPLES:
        hits = find_secrets(sample)
        assert bool(hits) is expect, (sample, hits)


def test_find_secrets_and_redact_text_interfaces():
    text = "令牌是 sk-abc123DEF456 然后普通中文句子没有密钥"
    hits = find_secrets(text)
    assert isinstance(hits, list)
    assert hits == ["sk-abc123DEF456"]
    redacted = redact_text(text)
    assert redacted == "令牌是 «令牌» 然后普通中文句子没有密钥"
    assert redact_text(text, replacement="«HIDDEN»") == "令牌是 «HIDDEN» 然后普通中文句子没有密钥"


def test_high_entropy_token_but_not_chinese_prose():
    token = "Xk9mP2qR7tL4nB8wY3cV6hJ1"
    prose = "这是一段很长的中文说明文字用来确认脱敏不会把普通叙述误判成令牌。"
    assert find_secrets(f"备用口令 {token}") == [token]
    assert find_secrets(prose) == []
    assert "github.com/rogdance/rogmem" not in find_secrets("仓库 https://github.com/rogdance/rogmem")
    assert find_secrets("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef") == []


def test_engine_uses_redact_module_not_local_regex():
    text = Path("rogmem/engine.py").read_text(encoding="utf-8")
    assert "from rogmem.redact import redact_text, find_secrets" in text
    assert "SECRET_RE" not in text
    assert "redact_text(" in text
    assert "find_secrets(" in text


def test_llm_extract_redacts_before_model(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_LLM_BASE", "http://example.invalid/v1")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    seen: dict[str, str] = {}

    def fake_complete(_base, _key, payload):
        seen["content"] = payload["messages"][-1]["content"]
        return json.dumps({"items": [], "sheet": [], "critic": {}}), "stop"

    monkeypatch.setattr(engine, "_complete_chat", fake_complete)
    engine._llm_extract("令牌是 sk-abc123DEF456 普通中文句子没有密钥")
    assert "sk-abc123DEF456" not in seen["content"]
    assert "«令牌»" in seen["content"]
    assert "普通中文句子没有密钥" in seen["content"]
