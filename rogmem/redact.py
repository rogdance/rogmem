"""Recognize and replace secrets before they leave the machine."""

from __future__ import annotations

import re

REPLACEMENT = "«令牌»"

# Shared red-team corpus: (sample, should_detect). Used by unit tests and live_check.
SAMPLES: list[tuple[str, bool]] = [
    ("AKIA" + "0" * 16, True),
    ("eyJhbGciOiJIUzI1NiJ9.e30.sig", True),
    ("Bearer aaa.bbb.ccc", True),
    ("password=hunter2", True),
    ("sk-abc123DEF456", True),
    ("普通中文句子没有密钥", False),
    ("ghp_abcdefghijklmnopQRSTUV1234567890", True),
    ("gho_abcdefghijklmnopQRSTUV1234567890", True),
    ("xoxb-123456789012-abcdefghijklmnop", True),
    ("ASIA" + "1" * 16, True),
    ("api_key=rotating-secret-value", True),
    ("api-key=rotating-secret-value", True),
    ("Xk9mP2qR7tL4nB8wY3cV6hJ1", True),
    ("这是一段很长的中文说明文字用来确认脱敏不会把普通叙述误判成令牌。", False),
]

_STRUCTURED = [
    re.compile(r"(?:sk-|ghp_|gho_|xox[A-Za-z0-9]*-)[A-Za-z0-9_\-]{6,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(r"eyJ[A-Za-z0-9_-]+=*(?:\.[A-Za-z0-9_-]+=*){2}"),
    re.compile(r"Bearer\s+\S+", re.I),
    re.compile(r"password\s*=\s*\S+", re.I),
    re.compile(r"api[_-]?key\s*=\s*\S+", re.I),
]
_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_\-+=/]{24,}(?![A-Za-z0-9])")
_HEX = re.compile(r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


def _high_entropy(token: str) -> bool:
    if _HEX.match(token):
        return False
    has_lower = any("a" <= c <= "z" for c in token)
    has_upper = any("A" <= c <= "Z" for c in token)
    has_digit = any(c.isdigit() for c in token)
    if not has_digit or not (has_lower or has_upper):
        return False
    if len(set(token)) / len(token) < 0.45:
        return False
    return True


def _spans(text: str) -> list[tuple[int, int]]:
    if not text:
        return []
    spans: list[tuple[int, int]] = []
    for pattern in _STRUCTURED:
        for match in pattern.finditer(text):
            spans.append(match.span())
    for match in _TOKEN.finditer(text):
        if _high_entropy(match.group(0)):
            spans.append(match.span())
    if not spans:
        return []
    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def find_secrets(text: str) -> list[str]:
    if not text:
        return []
    return [text[start:end] for start, end in _spans(text)]


def redact_text(text: str, replacement: str = REPLACEMENT) -> str:
    if not text:
        return text
    spans = _spans(text)
    if not spans:
        return text
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(text[cursor:start])
        parts.append(replacement)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)
