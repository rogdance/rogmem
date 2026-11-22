"""Page delivery gate for run logs. Pure functions, no database.

A page body is mid/long-term memory. A section whose heading names it as a
run or change log (修订记录, 更新日志, changelog ...) only lists what each run
did; the revisions table already keeps that history, and the source material
keeps the detail. Such a section is dropped when the page is delivered.

Only the heading decides. The section runs to the next heading of the same or a
higher level, or to the page-end note definitions, whichever comes first. A
note definition whose mark lived only inside the dropped section goes with it.
"""

from __future__ import annotations

import re

# Closed list on purpose: the program validates, the prompt does not enumerate.
LOG_HEADINGS = frozenset(
    {
        "修订记录",
        "修订历史",
        "修订日志",
        "修改记录",
        "修改历史",
        "更新记录",
        "更新历史",
        "更新日志",
        "变更记录",
        "变更历史",
        "变更日志",
        "版本记录",
        "版本历史",
        "改动记录",
        "运行记录",
        "changelog",
        "change log",
        "revision history",
        "revisions",
        "update log",
    }
)

_HEADING = re.compile(r"^(#{2,6})\s+(.*?)\s*#*\s*$")
_NOTE_DEF = re.compile(r"^\[\^([^\]]+)\][:：]")
_MARK = re.compile(r"\[\^([^\]]+)\](?![:：])")
# Leading numbering such as 一、 3. (2) 第三节
_NUMBERING = re.compile(r"^(?:第?[0-9一二三四五六七八九十百]+[、.．)）节章]?\s*|[（(][0-9一二三四五六七八九十]+[)）]\s*)")
_TRAILER = re.compile(r"\s*(?:[（(][^）)]*[）)]|[:：].*)$")
_EMPHASIS = re.compile(r"[*_`]+")


def log_heading(title: str) -> bool:
    """True when a heading's text names a run or change log."""
    text = _EMPHASIS.sub("", title or "").strip()
    text = _NUMBERING.sub("", text).strip()
    text = _TRAILER.sub("", text).strip()
    return text.casefold() in LOG_HEADINGS


def drop_log_sections(body: str) -> tuple[str, int]:
    """Remove run/change-log sections. Returns the new body and the characters dropped.

    A body without such a section comes back unchanged, character for character.
    """
    raw = body or ""
    lines = raw.split("\n")
    kept: list[str] = []
    dropped: list[str] = []
    skip_level = 0
    for line in lines:
        stripped = line.strip()
        head = _HEADING.match(stripped)
        if skip_level:
            ends = (head and len(head.group(1)) <= skip_level) or _NOTE_DEF.match(stripped)
            if not ends:
                dropped.append(line)
                continue
            skip_level = 0
            if kept:
                kept.append("")
        if head and log_heading(head.group(2)):
            skip_level = len(head.group(1))
            while kept and not kept[-1].strip():
                kept.pop()
            dropped.append(line)
            continue
        kept.append(line)
    if not dropped:
        return raw, 0
    gone_marks = {m.group(1) for line in dropped for m in _MARK.finditer(line)}
    if gone_marks:
        still = {m.group(1) for line in kept if not _NOTE_DEF.match(line.strip()) for m in _MARK.finditer(line)}
        orphan = gone_marks - still
        kept = [
            line
            for line in kept
            if not ((m := _NOTE_DEF.match(line.strip())) and m.group(1) in orphan)
        ]
    text = "\n".join(kept).strip()
    return text, max(0, len(raw.strip()) - len(text))
