"""Wiki links live in the page text.

A page points at another page by writing ``[[篇名]]``. Saving the page
replaces that page's outgoing links. The other direction is the same row
read from the far side. The reconcile job rewrites the marks so the brackets are exactly the
mentions that resolve to one other page still in the table. A merged or
withdrawn page is still there. It does not start a timer of its own.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3

LINK_KIND = "提及"
_MARK = re.compile(r"\[\[([^\[\]\n]{1,80})\]\]")


def marked_titles(body: str) -> list[str]:
    seen: list[str] = []
    for match in _MARK.finditer(body or ""):
        title = match.group(1).strip()
        if title and title not in seen:
            seen.append(title)
    return seen


def _labels(name: str, envelope_json: str) -> list[str]:
    labels = [name.strip()] if name and name.strip() else []
    try:
        envelope = json.loads(envelope_json or "{}")
    except (TypeError, ValueError):
        envelope = {}
    aliases = envelope.get("aliases") if isinstance(envelope, dict) else None
    if isinstance(aliases, list):
        labels.extend(str(item).strip() for item in aliases if str(item).strip())
    return labels


def title_index(con: sqlite3.Connection) -> dict[str, str | None]:
    """Map a label to one page that still has a row.

    已合并 and 已撤回 are not deleted; the list can still open them. Every
    remaining row counts, whatever its status. When an active page carries
    the same writing, the link points there: that is the page that remains.
    Two active pages, or several non-active pages and no single active one,
    map to None.
    """
    grouped: dict[str, dict[str, str]] = {}
    rows = con.execute("SELECT id, name, status, envelope_json FROM crystallizations")
    for row in rows:
        status = row["status"] or ""
        for label in _labels(row["name"] or "", row["envelope_json"] or ""):
            grouped.setdefault(label, {})[row["id"]] = status
    index: dict[str, str | None] = {}
    for label, pages in grouped.items():
        active = [pid for pid, status in pages.items() if status == "active"]
        if len(active) == 1:
            index[label] = active[0]
        elif len(active) > 1:
            index[label] = None
        elif len(pages) == 1:
            index[label] = next(iter(pages))
        else:
            index[label] = None
    return index


def target_ids(con: sqlite3.Connection, body: str, self_id: str, index: dict[str, str | None] | None = None) -> list[str]:
    index = title_index(con) if index is None else index
    found: list[str] = []
    for title in marked_titles(body):
        dest = index.get(title)
        if not dest or dest == self_id or dest in found:
            continue
        found.append(dest)
    return found


def _structural_spans(body: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Citation lines and fenced code stay verbatim. Alias lines stay labels."""
    frozen: list[tuple[int, int]] = []
    alias: list[tuple[int, int]] = []
    offset = 0
    fence_at: int | None = None
    for line in body.splitlines(keepends=True):
        end = offset + len(line)
        stripped = line.strip()
        if fence_at is None and stripped.startswith("```"):
            fence_at = offset
        elif fence_at is not None:
            if stripped.startswith("```"):
                frozen.append((fence_at, end))
                fence_at = None
        elif re.match(r"^\[\^[^\]]+\]:", stripped):
            frozen.append((offset, end))
        else:
            probe = stripped
            while probe.startswith("#"):
                probe = probe[1:].strip()
            if probe.startswith("别名：") or probe.startswith("别名:"):
                alias.append((offset, end))
        offset = end
    if fence_at is not None:
        frozen.append((fence_at, len(body)))
    return frozen, alias


def _covered(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(left <= start and end <= right for left, right in spans)


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < right and end > left for left, right in spans)


def _autolink_labels(index: dict[str, str | None], self_id: str) -> list[str]:
    """Labels that can be written as ``[[ ]]`` and point at one other page.

    One character is not filled in: the same floor as an exact name scan.
    A label longer than the mark grammar, or one two pages share, is not guessed.
    """
    labels: list[str] = []
    for label, dest in index.items():
        if not dest or dest == self_id:
            continue
        if not 2 <= len(label) <= 80:
            continue
        if any(mark in label for mark in "[]\n"):
            continue
        labels.append(label)
    labels.sort(key=lambda item: (-len(item), item))
    return labels


def align_wikilinks(body: str, index: dict[str, str | None], self_id: str) -> str:
    """Make ``[[ ]]`` the mentions that resolve to one other existing page.

    A mark whose text is not a title or alias of any page still in the table
    loses the brackets and keeps the words. A bare span equal to one such
    label gains brackets. A shorter label covered by a longer one is not a
    second mention.
    A label two pages share is left as written. This page's own labels are not
    newly marked. Citation lines and fenced code stay verbatim. An alias line
    is a label list: brackets there are removed and none are added.
    """
    text = body or ""
    if not text:
        return text
    frozen, alias_lines = _structural_spans(text)

    parts: list[str] = []
    cursor = 0
    for match in _MARK.finditer(text):
        parts.append(text[cursor : match.start()])
        if _covered(match.start(), match.end(), frozen):
            parts.append(match.group(0))
        elif _covered(match.start(), match.end(), alias_lines):
            parts.append(match.group(1))
        elif match.group(1).strip() in index:
            parts.append(match.group(0))
        else:
            parts.append(match.group(1))
        cursor = match.end()
    parts.append(text[cursor:])
    unwrapped = "".join(parts)

    frozen, alias_lines = _structural_spans(unwrapped)
    blocked = [(match.start(), match.end()) for match in _MARK.finditer(unwrapped)]
    blocked.extend(frozen)
    blocked.extend(alias_lines)
    hits: list[tuple[int, int]] = []
    for label in _autolink_labels(index, self_id):
        start = 0
        while True:
            at = unwrapped.find(label, start)
            if at < 0:
                break
            end = at + len(label)
            if _overlaps(at, end, blocked):
                start = at + 1
                continue
            hits.append((at, end))
            blocked.append((at, end))
            start = end
    if not hits:
        return unwrapped
    hits.sort()
    out: list[str] = []
    cursor = 0
    for at, end in hits:
        out.append(unwrapped[cursor:at])
        out.append("[[" + unwrapped[at:end] + "]]")
        cursor = end
    out.append(unwrapped[cursor:])
    return "".join(out)


def sync_wikilinks(
    con: sqlite3.Connection,
    crystal_id: str,
    body: str,
    *,
    active: bool = True,
    index: dict[str, str | None] | None = None,
) -> None:
    """Replace this page's outgoing wiki links so they match ``[[ ]]`` in the body.

    一次巡检要走过每一页。调用方把已经算好的 ``index`` 传进来，这里就不再一页重读
    整张 crystallizations 去拼同一份索引。
    """
    wanted = target_ids(con, body, crystal_id, index) if active else []
    keep = set(wanted)
    rows = con.execute(
        "SELECT id, dst FROM edges WHERE src=? AND kind=? AND ended=0",
        (crystal_id, LINK_KIND),
    ).fetchall()
    for row in rows:
        if row["dst"] in keep:
            keep.discard(row["dst"])
            continue
        con.execute("UPDATE edges SET ended=1 WHERE id=?", (row["id"],))
    for dest in wanted:
        if dest not in keep:
            continue
        con.execute(
            "INSERT INTO edges(id,src,dst,kind,statement_id,ended) VALUES(?,?,?,?,?,0)",
            (secrets.token_hex(8), crystal_id, dest, LINK_KIND, ""),
        )


def linked_pages(con: sqlite3.Connection, crystal_id: str) -> tuple[list[dict], list[dict]]:
    outgoing = _side(con, crystal_id, "src", "dst")
    incoming = _side(con, crystal_id, "dst", "src")
    return outgoing, incoming


def _side(con: sqlite3.Connection, crystal_id: str, near: str, far: str) -> list[dict]:
    rows = con.execute(
        f"""SELECT b.id, b.name FROM edges e
            JOIN crystallizations b ON b.id=e.{far}
            WHERE e.{near}=? AND e.kind=? AND e.ended=0
            ORDER BY b.name""",
        (crystal_id, LINK_KIND),
    ).fetchall()
    return [{"id": row["id"], "name": row["name"]} for row in rows]
