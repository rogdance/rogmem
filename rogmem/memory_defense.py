"""策略化 Memory Defense（Hindsight P2 吸收）。

在现有 ``redact`` 检测之上加 allow / redact / block 三态策略，
命中只留指纹预览，不落原值。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum

from rogmem.redact import REPLACEMENT, _spans, redact_text


class DefenseAction(str, Enum):
    ALLOW = "allow"
    REDACT = "redact"
    BLOCK = "block"


_VALID_ACTIONS = {a.value for a in DefenseAction}

# 内建检测器：复用 redact 的结构化 + 高熵密钥识别。
DETECTOR_SENSITIVE = "sensitive_data"


@dataclass(frozen=True)
class PolicyRule:
    on: str
    action: DefenseAction


@dataclass(frozen=True)
class DefensePolicy:
    enabled: bool = True
    rules: tuple[PolicyRule, ...] = ()


@dataclass
class DefenseDecision:
    action: DefenseAction
    detector: str | None = None
    message: str = ""
    redacted_content: str | None = None
    matched_types: list[str] = field(default_factory=list)
    # ``{"detector": label, "preview": fingerprint}`` — preview 永非原值。
    hits: list[dict] = field(default_factory=list)


def fingerprint_value(value: str) -> str:
    """长度感知的命中预览：够认供应商前缀，不够还原密钥。"""
    n = len(value or "")
    if n < 6:
        return "[redacted]"
    if n <= 15:
        return f"{value[:2]}...{value[-2:]}"
    return f"{value[:4]}...{value[-4:]}"


def default_policy() -> DefensePolicy:
    """默认与现行「检出即脱敏」对齐：启用 + sensitive_data → redact。"""
    return DefensePolicy(
        enabled=True,
        rules=(PolicyRule(on=DETECTOR_SENSITIVE, action=DefenseAction.REDACT),),
    )


def parse_policy(raw: dict | None) -> DefensePolicy:
    if raw is None:
        return default_policy()
    rules: list[PolicyRule] = []
    for item in raw.get("rules") or []:
        on_raw = item.get("on")
        if not isinstance(on_raw, str) or not on_raw.strip():
            raise ValueError(f"invalid on {on_raw!r}; must be a non-empty string")
        action_raw = item.get("action")
        if action_raw not in _VALID_ACTIONS:
            raise ValueError(
                f"invalid action {action_raw!r}; must be one of {sorted(_VALID_ACTIONS)}"
            )
        rules.append(PolicyRule(on=on_raw.strip(), action=DefenseAction(action_raw)))
    if "enabled" not in raw and not rules:
        return default_policy()
    return DefensePolicy(enabled=bool(raw.get("enabled", False)), rules=tuple(rules))


def policy_from_json(blob: str | None) -> DefensePolicy:
    if not (blob or "").strip():
        return default_policy()
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ValueError("memory_defense 不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("memory_defense 须是对象")
    return parse_policy(data)


def dump_policy(policy: DefensePolicy) -> str:
    return json.dumps(
        {
            "enabled": policy.enabled,
            "rules": [{"on": r.on, "action": r.action.value} for r in policy.rules],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _sensitive_hits(text: str) -> list[dict]:
    hits: list[dict] = []
    for start, end in _spans(text or ""):
        raw = text[start:end]
        hits.append(
            {
                "detector": DETECTOR_SENSITIVE,
                "preview": fingerprint_value(raw),
            }
        )
    return hits


def screen(content: str, policy: DefensePolicy | None = None) -> DefenseDecision:
    """按策略审查正文。未命中或策略关闭 → allow。"""
    policy = policy or default_policy()
    if not policy.enabled:
        return DefenseDecision(action=DefenseAction.ALLOW)

    rule = next((r for r in policy.rules if r.on == DETECTOR_SENSITIVE), None)
    if rule is None or rule.action is DefenseAction.ALLOW:
        return DefenseDecision(action=DefenseAction.ALLOW)

    hits = _sensitive_hits(content)
    if not hits:
        return DefenseDecision(action=DefenseAction.ALLOW)

    if rule.action is DefenseAction.BLOCK:
        return DefenseDecision(
            action=DefenseAction.BLOCK,
            detector=DETECTOR_SENSITIVE,
            message="敏感数据命中，按策略拒绝入库",
            matched_types=[DETECTOR_SENSITIVE],
            hits=hits,
        )

    scrubbed = redact_text(content, replacement=REPLACEMENT)
    return DefenseDecision(
        action=DefenseAction.REDACT,
        detector=DETECTOR_SENSITIVE,
        message="敏感数据命中，已按策略脱敏",
        redacted_content=scrubbed,
        matched_types=[DETECTOR_SENSITIVE],
        hits=hits,
    )
