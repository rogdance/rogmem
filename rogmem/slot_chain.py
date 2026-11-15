"""Small helpers for a section's wording history.

There is no fixed slot list. A change chain is whatever was written under
one heading on the page.
"""

from __future__ import annotations

from urllib.parse import quote


def style_delta(previous: str, text: str) -> str:
    if not previous:
        return "新增"
    if text == previous:
        return "重复"
    if previous in text:
        extra = text.replace(previous, "", 1).strip("，,。 ")
        return f"新增了{extra}" if extra else "新增"
    return f"由「{previous}」改为「{text}」"


def session_href(session_id: str, turn_id: str = "") -> str:
    session = str(session_id or "").strip()
    if not session:
        return ""
    href = f"/admin/sources?tab=session&session={quote(session, safe='')}"
    turn = str(turn_id or "").strip()
    if turn:
        href += f"&turn={quote(turn, safe='')}"
    return href


def source_href(source_id: str, statement_id: str = "") -> str:
    sid = str(source_id or "").strip()
    if not sid:
        return ""
    href = f"/admin/sources/{quote(sid, safe='')}"
    cite = str(statement_id or "").strip()
    if cite:
        safe = quote(cite, safe="")
        href += f"?cite={safe}#cite-{safe}"
    return href


def origin_links(session_id: str = "", turn_id: str = "", source_id: str = "", statement_id: str = "") -> dict[str, str]:
    return {
        "session_href": session_href(session_id, turn_id),
        "source_href": source_href(source_id, statement_id),
    }
