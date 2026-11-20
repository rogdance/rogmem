"""One reading of a source's lamps, shared by the list and its filter.

The writer stores a pipeline mark and a coarse status. The page paints lamps
from those two fields. The filter must ask the same question the lamps answer:
which lamp is lit, and how. A second list of status words drifts.
"""

from __future__ import annotations

STAGES = ("解析", "草稿", "并页", "速写")
VECTOR = "向量化"
LAMP_WORD = {"ok": "通过", "run": "进行中", "retry": "重试中", "fail": "失败", "wait": "未到"}
ACTIVE = ("run", "retry", "fail")

ST_PARSED = "已解析待向量化"
ST_PARSE_FAIL = "解析失败"
ST_VEC_READY = "已向量化待萃取"
ST_VEC_FAIL = "向量化失败"
ST_DISTILLING = "萃取中"
ST_CRYSTAL = "萃取已结晶"
ST_NO_CRYSTAL = "萃取无结晶"
ST_DISTILL_FAIL = "萃取失败"
ST_NO_BODY = "无正文"
# 导入先收条、后读文：文件已在扫描目录，行已建好，读文在后台。这两句是那段时间的读数。
ST_QUEUED = "排队中"
ST_PARSING = "解析中"

WAIT_DRAFT = "待萃取"
DONE = "完成"

_STORED_LAMP = {"ok", "run", "fail", "retry", "wait"}
_LEGACY_STATUS = {
    "正文不足": ST_NO_BODY,
    "向量未就绪": ST_VEC_FAIL,
    "覆盖完成": ST_CRYSTAL,
    "队列中": ST_VEC_READY,
    # 旧字：库里存过「蒸馏」，界面上现在叫「萃取」。
    "蒸馏队列中": ST_VEC_READY,
    "已向量化待蒸馏": ST_VEC_READY,
    "蒸馏中": ST_DISTILLING,
    "蒸馏已结晶": ST_CRYSTAL,
    "蒸馏无结晶": ST_NO_CRYSTAL,
    "蒸馏失败": ST_DISTILL_FAIL,
    "已解析": ST_PARSED,
}


def canonical_status(status: str) -> str:
    """The word a stored status means now. Unknown words stay as stored."""
    text = status or ""
    return _LEGACY_STATUS.get(text, text)


def peel_vector(pipeline: str) -> tuple[str, str]:
    text = pipeline or ""
    base, sep, vector = text.rpartition("~")
    if not sep or not vector.startswith("向量:"):
        return text, ""
    state = vector.split(":", 1)[1].strip()
    if state not in _STORED_LAMP:
        state = "wait"
    return base, state


def _legacy_flow(status: str) -> str:
    """Rows saved before pipeline marks. The status word only fills the lamps."""
    shown = canonical_status(status)
    if shown in {ST_PARSE_FAIL, ST_NO_BODY}:
        return "解析:fail"
    if shown in {ST_VEC_FAIL, ST_PARSED, ST_VEC_READY}:
        return "解析:ok"
    if shown == ST_DISTILLING:
        return "草稿:run"
    if shown == ST_CRYSTAL:
        return "完成:ok"
    if shown == ST_NO_CRYSTAL:
        return "草稿:ok"
    if shown == ST_DISTILL_FAIL:
        return "草稿:fail"
    return "解析:run"


def retire_polish(pipeline: str) -> str:
    """A stored polish lamp is not a stage. One that was still going continues at 速写.

    The vector lamp is glued on with ``~`` and is not part of the stage name.
    Take it off before reading stages, then put it back.
    """
    text = pipeline or ""
    if "润色" not in text:
        return text
    body, vector = peel_vector(text)
    memory, bar, display = body.partition("|")
    if not bar:
        memory, display = "", body

    def rewrite(section: str, *, as_memory: bool) -> str:
        kept: list[str] = []
        polish_state = ""
        for part in section.split(","):
            if not part:
                continue
            name, colon, state = part.partition(":")
            if name != "润色":
                kept.append(part)
                continue
            if colon:
                polish_state = state
            elif as_memory:
                polish_state = polish_state or "fail"
        if as_memory:
            if polish_state in {"run", "retry", "fail"} and "速写" not in kept:
                kept.append("速写")
            return ",".join(kept)
        if polish_state in {"run", "retry", "fail"} and not any(item.startswith("速写:") for item in kept):
            kept.append(f"速写:{polish_state}")
        if kept:
            return ",".join(kept)
        if polish_state == "ok":
            return "并页:ok"
        if polish_state in {"run", "retry", "fail"}:
            return f"速写:{polish_state}"
        return ""

    shown = rewrite(display, as_memory=False)
    if bar:
        remembered = rewrite(memory, as_memory=True)
        body = remembered + "|" + shown if remembered else shown
    else:
        body = shown
    if vector:
        return f"{body}~向量:{vector}" if body else f"解析:ok~向量:{vector}"
    return body


def _raw_flow(pipeline: str, status: str) -> list[tuple[str, str]]:
    raw = peel_vector(retire_polish((pipeline or "").strip()))[0] or _legacy_flow(status or "")
    if "|" in raw:
        raw = raw.split("|", 1)[1].strip()
    if "," in raw:
        marks = {}
        for part in raw.split(","):
            name, sep, state = part.partition(":")
            if sep and name in STAGES and state in {"ok", "run", "fail", "retry"}:
                marks[name] = state
        return [(name, marks.get(name, "wait")) for name in STAGES]
    if raw.startswith("完成"):
        return [(name, "ok") for name in STAGES]
    stage, _, state = raw.partition(":")
    if stage not in STAGES:
        stage, state = "解析", "run"
    if state not in {"run", "ok", "fail", "retry"}:
        state = "run"
    index = STAGES.index(stage)
    lights = []
    for at, name in enumerate(STAGES):
        if at < index:
            lights.append((name, "ok"))
        elif at == index:
            lights.append((name, state))
        else:
            lights.append((name, "wait"))
    return lights


def _distill_quiet(states: list[tuple[str, str]]) -> bool:
    return all(state not in ACTIVE for _name, state in states)


def flow_states(pipeline: str, status: str) -> list[tuple[str, str]]:
    """Lamps for the distill line.

    A lamp that is still going is the position. When the line is quiet and the
    stored word disagrees with that summary, the word wins: it is the outcome
    the writer recorded, and the summary was left behind.
    """
    states = _raw_flow(pipeline, status)
    if not _distill_quiet(states):
        return states
    stored = canonical_status(status)
    board = {name: state for name, state in states}
    if stored == ST_NO_CRYSTAL and not _stopped_after_draft(board):
        return _raw_flow("", ST_NO_CRYSTAL)
    if stored == ST_CRYSTAL and _stopped_after_draft(board):
        return _raw_flow("", ST_CRYSTAL)
    if stored in {ST_NO_BODY, ST_PARSE_FAIL} and board.get(STAGES[0]) != "fail":
        return _raw_flow("", stored)
    return states


def vector_state(pipeline: str, status: str) -> str:
    """向量化 sits beside 草稿. It does not gate the distill lamps."""
    _base, marked = peel_vector(pipeline or "")
    if marked:
        return marked
    shown = canonical_status(status)
    if shown == ST_VEC_FAIL:
        return "fail"
    if shown == ST_PARSED:
        return "run"
    if shown in {ST_VEC_READY, ST_DISTILLING, ST_CRYSTAL, ST_NO_CRYSTAL, ST_DISTILL_FAIL}:
        return "ok"
    return "wait"


def lamp_board(pipeline: str, status: str) -> dict[str, str]:
    board = {name: state for name, state in flow_states(pipeline, status)}
    board[VECTOR] = vector_state(pipeline, status)
    return board


def _parked_before_draft(board: dict[str, str]) -> bool:
    """Vector has passed and draft has not started. A vector still waiting is not this."""
    if board.get(STAGES[0]) != "ok":
        return False
    if any(board.get(name) != "wait" for name in STAGES[1:]):
        return False
    return board.get(VECTOR) == "ok"


def _stopped_after_draft(board: dict[str, str]) -> bool:
    if board.get("草稿") != "ok":
        return False
    return all(board.get(name) == "wait" for name in STAGES[2:])


def processing_filters() -> tuple[tuple[str, str], ...]:
    """Selectable lamps, in the order the work runs. Both lists use this."""
    options = [
        (f"{STAGES[0]}:fail", f"{STAGES[0]}{LAMP_WORD['fail']}"),
        (ST_NO_BODY, ST_NO_BODY),
    ]
    options.extend((f"{VECTOR}:{state}", f"{VECTOR}{LAMP_WORD[state]}") for state in ACTIVE)
    options.append((WAIT_DRAFT, WAIT_DRAFT))
    for stage in STAGES[1:]:
        options.extend((f"{stage}:{state}", f"{stage}{LAMP_WORD[state]}") for state in ACTIVE)
    options.append((ST_NO_CRYSTAL, ST_NO_CRYSTAL))
    options.append((DONE, "已完成"))
    return tuple(options)


_RUNNING = frozenset(f"{stage}:{state}" for stage in STAGES[1:] for state in ("run", "retry"))
_FAILED = frozenset(f"{stage}:fail" for stage in STAGES[1:])
# Old list words are names for a lamp question. They are not a second column check.
_ALIAS = {
    ST_PARSE_FAIL: f"{STAGES[0]}:fail",
    ST_PARSED: f"{VECTOR}:run",
    ST_VEC_FAIL: f"{VECTOR}:fail",
    ST_VEC_READY: WAIT_DRAFT,
    ST_NO_BODY: ST_NO_BODY,
    ST_NO_CRYSTAL: ST_NO_CRYSTAL,
    ST_CRYSTAL: DONE,
}


def places(pipeline: str, status: str) -> frozenset[str]:
    """Every filter token this row occupies. Read off the same board the lamps use."""
    stored = canonical_status(status)
    board = lamp_board(pipeline, status)
    if stored == ST_NO_BODY:
        return frozenset({ST_NO_BODY})
    if board.get(STAGES[0]) == "fail":
        return frozenset({f"{STAGES[0]}:fail"})
    found = set()
    for stage in (*STAGES[1:], VECTOR):
        state = board.get(stage)
        if state in ACTIVE:
            found.add(f"{stage}:{state}")
    if _parked_before_draft(board):
        found.add(WAIT_DRAFT)
    elif _stopped_after_draft(board):
        found.add(ST_NO_CRYSTAL)
    elif all(board.get(name) == "ok" for name in STAGES):
        found.add(DONE)
    return frozenset(found)


def mark_is_no_crystal(pipeline: str, status: str) -> bool:
    """The stored mark, before quiet-line repair, says nothing was written after draft."""
    if not (pipeline or "").strip():
        return True
    states = _raw_flow(pipeline, status)
    if not _distill_quiet(states):
        return False
    return _stopped_after_draft({name: state for name, state in states})


def matches(pipeline: str, status: str, selected: str) -> bool:
    """True when the selected lamp question is true of this row.

    An active lamp matches on its own, so draft and vector can both hit. Old
    status words ask the same questions: 萃取已结晶 means every distill lamp
    passed, 萃取中 means one of them is still going.
    """
    chosen = (selected or "").strip()
    if not chosen:
        return True
    word = canonical_status(chosen)
    found = places(pipeline, status)
    if word == ST_DISTILLING:
        return bool(found & _RUNNING)
    if word == ST_DISTILL_FAIL:
        return bool(found & _FAILED)
    return _ALIAS.get(word, word) in found


def is_active(pipeline: str, status: str) -> bool:
    """True while this row can still move on its own.

    The file list subscribes to the rows that answer yes and drops them as they
    go quiet. A failed or finished row never comes back without a reader.
    """
    if canonical_status(status) in {ST_QUEUED, ST_PARSING}:
        return True
    return any(state in {"run", "retry"} for state in lamp_board(pipeline, status).values())


def stage_note(pipeline: str, status: str, *, done: int = 0, total: int = 0) -> str:
    """The one stage this row is on, in words. Empty when the row is not moving.

    ``done``/``total`` are only meaningful for 向量化, the one stage that can count
    its units. Everything else is a single call and has no honest fraction.
    """
    word = canonical_status(status)
    if word == ST_QUEUED:
        return ST_QUEUED
    board = lamp_board(pipeline, status)
    moving = [name for name, state in board.items() if state in {"run", "retry"}]
    if not moving:
        return ST_PARSING if word == ST_PARSING else ""
    name = moving[0]
    if name == VECTOR:
        return f"{name} {done}/{total}" if total else name
    return f"{name}中"

