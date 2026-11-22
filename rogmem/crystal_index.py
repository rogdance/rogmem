"""The crystal index is the page a reader can open.

Claims live in their own index. A page can be full while that claim index is
empty, and search would then miss the crystal. This index follows the stored
page: its name, the short sketch, and the body.
"""

from __future__ import annotations

import sqlite3

import jieba


def tokens(text: str) -> str:
    parts = [part for part in jieba.cut_for_search(text or "") if part.strip()]
    return " ".join(parts)


def readable_text(name: str, body: str, sketch: str) -> str:
    from rogmem.pages_md import strip_source_marks

    # 〔源 …〕 marks are addresses, not words a search should match.
    parts = [
        part.strip()
        for part in (name or "", strip_source_marks(sketch or ""), strip_source_marks(body or ""))
        if part and part.strip()
    ]
    return "\n".join(parts)


def excerpt(page: str, query: str, limit: int = 240) -> str:
    text = (page or "").strip()
    if len(text) <= limit:
        return text
    at = -1
    for part in jieba.cut_for_search(query or ""):
        piece = part.strip()
        if not piece:
            continue
        at = text.find(piece)
        if at >= 0:
            break
    if at < 0:
        return text[:limit].rstrip()
    start = max(0, at - 40)
    return text[start : start + limit].strip()


def sync_crystal(con: sqlite3.Connection, crystal_id: str, name: str, body: str, sketch: str, status: str) -> None:
    try:
        con.execute("DELETE FROM fts_crystal WHERE crystal_id=?", (crystal_id,))
    except sqlite3.OperationalError:
        return
    if status != "active":
        return
    page = readable_text(name, body, sketch)
    if not page:
        return
    con.execute("INSERT INTO fts_crystal(crystal_id, text) VALUES(?,?)", (crystal_id, tokens(page)))


def reindex_crystals(con: sqlite3.Connection) -> None:
    con.execute("DELETE FROM fts_crystal")
    rows = con.execute("SELECT id, name, status, IFNULL(body_md,'') AS body_md, IFNULL(sketch_md,'') AS sketch_md FROM crystallizations")
    for row in rows:
        sync_crystal(con, row["id"], row["name"] or "", row["body_md"] or "", row["sketch_md"] or "", row["status"] or "")
    # 段级索引不在这里全量重建：它只在口径升版或首次接入时重建一次（ensure_index_current），
    # 每次启动都重切段+逐段分词是纯开销。
