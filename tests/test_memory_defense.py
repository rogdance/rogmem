"""策略化 Memory Defense：allow / redact / block + 不含原值的命中指纹。"""

from __future__ import annotations

import json

import pytest

from rogmem.memory_defense import (
    DefenseAction,
    default_policy,
    fingerprint_value,
    parse_policy,
    screen,
)


def test_fingerprint_never_contains_raw_secret():
    secret = "sk-abc123DEF4567890xyz"
    preview = fingerprint_value(secret)
    assert secret not in preview
    assert "..." in preview or preview == "[redacted]"
    assert fingerprint_value("ab") == "[redacted]"
    assert fingerprint_value("abcdef") == "ab...ef"


def test_parse_policy_allow_redact_block():
    policy = parse_policy(
        {
            "enabled": True,
            "rules": [{"on": "sensitive_data", "action": "block"}],
        }
    )
    assert policy.enabled is True
    assert policy.rules[0].action is DefenseAction.BLOCK

    with pytest.raises(ValueError, match="action"):
        parse_policy({"enabled": True, "rules": [{"on": "sensitive_data", "action": "drop"}]})


def test_default_policy_redacts_sensitive_data():
    policy = default_policy()
    assert policy.enabled is True
    assert policy.rules[0].on == "sensitive_data"
    assert policy.rules[0].action is DefenseAction.REDACT


def test_screen_redact_scrubs_and_fingerprints():
    text = "令牌是 sk-abc123DEF456 然后普通中文"
    decision = screen(text, default_policy())
    assert decision.action is DefenseAction.REDACT
    assert decision.detector == "sensitive_data"
    assert "sk-abc123DEF456" not in (decision.redacted_content or "")
    assert "«令牌»" in (decision.redacted_content or "")
    assert decision.hits
    for hit in decision.hits:
        assert "sk-abc123DEF456" not in hit["preview"]
        assert hit["detector"]


def test_screen_block_refuses_without_redacted_body():
    policy = parse_policy(
        {"enabled": True, "rules": [{"on": "sensitive_data", "action": "block"}]}
    )
    decision = screen("密钥 sk-abc123DEF456", policy)
    assert decision.action is DefenseAction.BLOCK
    assert decision.redacted_content is None
    assert decision.hits


def test_screen_allow_passes_secrets_through():
    policy = parse_policy(
        {"enabled": True, "rules": [{"on": "sensitive_data", "action": "allow"}]}
    )
    text = "密钥 sk-abc123DEF456"
    decision = screen(text, policy)
    assert decision.action is DefenseAction.ALLOW
    assert decision.redacted_content is None
    assert decision.hits == []


def test_screen_disabled_is_allow():
    policy = parse_policy({"enabled": False, "rules": [{"on": "sensitive_data", "action": "block"}]})
    decision = screen("sk-abc123DEF456", policy)
    assert decision.action is DefenseAction.ALLOW


def test_submit_text_honours_block_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine
    from rogmem.memory_defense import DefenseAction, parse_policy

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_setting(
        "memory_defense",
        json.dumps(
            {"enabled": True, "rules": [{"on": "sensitive_data", "action": "block"}]},
            ensure_ascii=False,
        ),
    )
    agent = engine.db().execute("SELECT * FROM agents LIMIT 1").fetchone()
    if agent is None:
        # Minimal agent row for submit_text
        engine.db().execute(
            """INSERT INTO agents(id, device_id, device_name, program, display_name, status, pubkey, created_at)
               VALUES('a1','d1','本机','cursor','Cursor','active','pub',?)""",
            (engine.utcnow(),),
        )
        engine.db().commit()
        agent = engine.db().execute("SELECT * FROM agents WHERE id='a1'").fetchone()

    result = engine.submit_text(
        kind="user",
        text="请记住 sk-abc123DEF456",
        session_id="s1",
        turn_id="t1",
        seq=1,
        agent=agent,
        event_at=None,
        project=None,
    )
    assert result["ok"] is False
    assert result.get("defense") == DefenseAction.BLOCK.value
    assert "sk-abc123DEF456" not in json.dumps(result, ensure_ascii=False)
