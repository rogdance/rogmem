"""Pre-model triage for distillation units. Pure functions, no database.

A unit that carries nothing new for mid/long-term memory never reaches the
model. The gate is conservative: anything it is not sure about is distilled.

R1 重复材料: the same normalized material already finished distilling.
R2 例行任务无新增: a long fixed instruction that keeps coming back, distilled
   within the last day, and the reply still reads like that last distilled one.
R3 回复只是宿主报错: a short user line answered only by a host/client error.
"""

from __future__ import annotations

from rogmem.pipeline_knobs import DEFAULTS as _KNOB_DEFAULTS

import hashlib
import re
from datetime import datetime, timezone

TEMPLATE_MIN_USER_CHARS = _KNOB_DEFAULTS["triage_template_min_chars"]
ROUTINE_MIN_EARLIER = _KNOB_DEFAULTS["triage_routine_min_earlier"]
ROUTINE_WINDOW_HOURS = _KNOB_DEFAULTS["triage_routine_window_hours"]
ROUTINE_AGENT_JACCARD = _KNOB_DEFAULTS["triage_routine_similarity"] / 100
AGENT_SAMPLE_CHARS = 2000
NGRAM = 3

REASON_DUPLICATE = "重复材料"
REASON_ROUTINE = "例行任务无新增"
REASON_HOST_ERROR = "回复只是宿主报错"

DECISION_PENDING = "pending"
DECISION_DISTILLED = "distilled"
DECISION_SKIPPED = "skipped"
FINISHED = (DECISION_DISTILLED, DECISION_SKIPPED)

_WS = re.compile(r"\s+", re.UNICODE)
# Lines the program itself puts into a packet, not words anyone said.
_HEADER_LINE = re.compile(r"^\s*【rogmem[^】]*】")

# Closed list of host/client error envelopes. Each sentence of the reply must
# match one of these in full for the reply to count as "only an error".
_HOST_ERRORS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"authentication (is )?required",
        r"please (use|run) (the )?/login( command)?( to (sign|log) in( to your account)?)?",
        r"(you are |you're )?not logged in",
        r"invalid api key",
        r"(error: ?)?unauthorized",
        r"(api )?rate limits? (exceeded|reached)",
        r"too many requests",
        r"credit balance is too low",
        r"api error: ?(401|403|429|5\d\d)\b.*",
        r"请(先)?(使用 ?/login ?(命令)?)?登录",
    )
)
_SENTENCE_SPLIT = re.compile(r"[.。!！;；\n]+")
_EDGE_PUNCT = " \t\"'`*_-—:：,，()（）[]【】<>"


def normalize_material(text: str) -> str:
    """Drop program header lines and collapse whitespace."""
    lines = [line for line in str(text or "").splitlines() if not _HEADER_LINE.match(line)]
    return _WS.sub(" ", "\n".join(lines)).strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unit_hash(text: str) -> str:
    return _sha(normalize_material(text))


def template_key(user_text: str, *, min_chars: int | None = None) -> str:
    """A long user side is a template. A short one is not."""
    norm = normalize_material(user_text)
    if len(norm) < (TEMPLATE_MIN_USER_CHARS if min_chars is None else min_chars):
        return ""
    return _sha(norm)


def agent_sample(agent_text: str) -> str:
    return normalize_material(agent_text)[:AGENT_SAMPLE_CHARS]


def char_ngrams(text: str, n: int = NGRAM) -> set[str]:
    norm = normalize_material(text)
    if not norm:
        return set()
    if len(norm) < n:
        return {norm}
    return {norm[i : i + n] for i in range(len(norm) - n + 1)}


def jaccard(a: str, b: str) -> float:
    left, right = char_ngrams(a), char_ngrams(b)
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _is_error_piece(piece: str) -> bool:
    piece = piece.strip(_EDGE_PUNCT)
    if not piece:
        return True
    return any(pattern.fullmatch(piece) for pattern in _HOST_ERRORS)


def is_host_error_reply(agent_text: str) -> bool:
    """True only when every line of the reply is a known host error envelope."""
    lines = [line.strip() for line in str(agent_text or "").splitlines() if line.strip()]
    if not lines:
        return False
    for line in lines:
        if _is_error_piece(line):
            continue
        pieces = [piece for piece in _SENTENCE_SPLIT.split(line) if piece.strip(_EDGE_PUNCT)]
        if not pieces or not all(_is_error_piece(piece) for piece in pieces):
            return False
    return True


def parse_instant(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _within_window(left: str, right: str, hours: float = ROUTINE_WINDOW_HOURS) -> bool:
    a, b = parse_instant(left), parse_instant(right)
    if a is None or b is None:
        return False
    return abs((a - b).total_seconds()) < hours * 3600


def routine_repeat(
    agent_text: str,
    unit_at: str,
    history: list[dict],
    *,
    min_earlier: int = ROUTINE_MIN_EARLIER,
    window_hours: float = ROUTINE_WINDOW_HOURS,
    similarity: float = ROUTINE_AGENT_JACCARD,
) -> bool:
    """history: earlier finished units with the same template key.

    Each item has decision, agent_sample, unit_at.
    """
    finished = [item for item in history if item.get("decision") in FINISHED]
    if len(finished) < min_earlier:
        return False
    distilled = [item for item in finished if item.get("decision") == DECISION_DISTILLED]
    if not distilled:
        return False
    latest = max(distilled, key=lambda item: parse_instant(item.get("unit_at") or "") or datetime.min.replace(tzinfo=timezone.utc))
    if not _within_window(unit_at, latest.get("unit_at") or "", window_hours):
        return False
    return jaccard(agent_sample(agent_text), latest.get("agent_sample") or "") >= similarity


def decide(
    *,
    is_file: bool,
    user_text: str,
    agent_text: str,
    unit_at: str,
    duplicate_of_distilled: bool,
    template_history: list[dict],
    min_chars: int = TEMPLATE_MIN_USER_CHARS,
    min_earlier: int = ROUTINE_MIN_EARLIER,
    window_hours: float = ROUTINE_WINDOW_HOURS,
    similarity: float = ROUTINE_AGENT_JACCARD,
) -> str:
    """The skip reason, or empty to distill. Thresholds default to the module constants."""
    if duplicate_of_distilled:
        return REASON_DUPLICATE
    if is_file:
        return ""
    if template_key(user_text, min_chars=min_chars) and routine_repeat(
        agent_text, unit_at, template_history, min_earlier=min_earlier, window_hours=window_hours, similarity=similarity
    ):
        return REASON_ROUTINE
    if len(normalize_material(user_text)) < min_chars and is_host_error_reply(agent_text):
        return REASON_HOST_ERROR
    return ""
