"""Typed enum answers for narrow gate workers.

Hindsight P1-1 absorption: binary gates (专名核对 / 身份合并) take an enumerated
answer instead of chat+JSON. Open page writing stays with large models. Vectors
never enter uniqueness.
"""

from __future__ import annotations

import json
import re

YES_NO = frozenset({"是", "否"})

# Bare enum, optional light punctuation / quotes the model sometimes wraps.
_BARE_YES_NO = re.compile(
    r"^[\s「」『』【】《》\"'“”‘’]*(是|否)[\s「」『』【】《》\"'“”‘’。．\.!！?？]*$"
)


def parse_yes_no(content: str) -> str:
    """Accept ``是`` / ``否``, or legacy thin JSON ``{"answer":"是"|"否"}``.

    Anything else raises ``ValueError``. Program acceptance stays strict; the
    transitional JSON path only covers stubs and unfinished cutovers.
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("空答复")
    matched = _BARE_YES_NO.match(text)
    if matched:
        return matched.group(1)
    data = _thin_answer_object(text)
    if isinstance(data, dict):
        answer = data.get("answer")
        if answer in YES_NO:
            return str(answer)
    raise ValueError("不是是或否")


def _thin_answer_object(text: str) -> dict | None:
    blob = text
    if blob.startswith("```"):
        blob = re.sub(r"^```(?:json)?\s*", "", blob)
        blob = re.sub(r"\s*```$", "", blob)
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        start = blob.find("{")
        end = blob.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(blob[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None
