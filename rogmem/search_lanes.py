"""Graph and temporal retrieval lanes for ``engine.search``.

Mechanism aligned with Hindsight recall arms, mapped onto RogMem tables:

- **Graph**: seed from query-hit crystal ids, walk live ``edges`` (ended=0)
  for 1–2 hops, emit neighbour statements / page excerpts. Score grows with
  shared-edge count and decays by hop (Hindsight link-expansion spirit;
  RogMem has no precomputed semantic/causal link weights).
- **Temporal**: parse a window from the query, filter by statement/source
  ``event_at`` (never ``ingested_at``), rank by residual-text relevance then
  temporal proximity, and spread across coverage buckets.

Vectors stay out of uniqueness — these lanes only feed recall RRF.
"""

from __future__ import annotations

import math
import re
import sqlite3
from datetime import datetime, timezone
from typing import Callable

from rogmem.retrieve import select_with_temporal_coverage
from rogmem.temporal_query import extract_temporal_window, strip_temporal_phrases

GRAPH_HOPS = 2
GRAPH_SEED_LIMIT = 20
# Hindsight caps entity expansion with link_expansion_per_entity_limit (default 200).
# RogMem has no unit_entities table; cap live edges walked per frontier node instead.
GRAPH_PER_SEED_LIMIT = 200
TEMPORAL_POOL = 60
TEMPORAL_ENTRY_POINTS = 10
TEMPORAL_BUCKETS = 8


def _parse_event_at(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _token_overlap(query: str, text: str) -> float:
    """Cheap relevance when embeddings are off: shared character/word mass."""
    q = (query or "").strip().lower()
    t = (text or "").strip().lower()
    if not q or not t:
        return 0.0
    # CJK: overlapping bigrams; Latin: whitespace tokens.
    if re.search(r"[\u4e00-\u9fff]", q):
        if len(q) < 2:
            return 1.0 if q in t else 0.0
        grams = {q[i : i + 2] for i in range(len(q) - 1)}
        if not grams:
            return 0.0
        hit = sum(1 for g in grams if g in t)
        return hit / len(grams)
    q_parts = {p for p in re.split(r"[^\w]+", q) if p}
    if not q_parts:
        return 0.0
    return sum(1 for p in q_parts if p in t) / len(q_parts)


def graph_seed_ids(
    con: sqlite3.Connection,
    query: str,
    *,
    identity_crystal_ids: list[str],
    statement_crystal_ids: list[str],
    limit: int = GRAPH_SEED_LIMIT,
) -> list[str]:
    """Entry crystals for graph expansion: identity hits, then FTS/name hits."""
    ordered: list[str] = []
    seen: set[str] = set()

    def add(cid: str) -> None:
        cid = str(cid or "").strip()
        if not cid or cid in seen:
            return
        seen.add(cid)
        ordered.append(cid)

    for cid in identity_crystal_ids:
        add(cid)
    for cid in statement_crystal_ids:
        add(cid)
    text = (query or "").strip()
    if text:
        for row in con.execute(
            """SELECT id FROM crystallizations
               WHERE status='active' AND (name=? OR id=?)
               LIMIT ?""",
            (text, text, limit),
        ):
            add(row["id"])
        # Loose name contains for short proper nouns already tokenised elsewhere.
        if 1 < len(text) <= 32:
            for row in con.execute(
                """SELECT id FROM crystallizations
                   WHERE status='active' AND name LIKE ?
                   LIMIT ?""",
                (f"%{text}%", max(4, limit // 2)),
            ):
                add(row["id"])
    return ordered[:limit]


def _neighbours(
    con: sqlite3.Connection,
    seeds: list[str],
    *,
    hops: int = GRAPH_HOPS,
    per_seed_limit: int = GRAPH_PER_SEED_LIMIT,
) -> dict[str, tuple[int, int]]:
    """Map neighbour crystal id → (min_hop, edge_count) along live edges."""
    if not seeds or hops < 1:
        return {}
    frontier = set(seeds)
    reached: dict[str, tuple[int, int]] = {}
    visited = set(seeds)
    for hop in range(1, hops + 1):
        if not frontier:
            break
        placeholders = ",".join("?" * len(frontier))
        params = list(frontier)
        rows = con.execute(
            f"""SELECT e.src, e.dst FROM edges e
                WHERE e.ended=0
                  AND (e.src IN ({placeholders}) OR e.dst IN ({placeholders}))
                  AND EXISTS (SELECT 1 FROM crystallizations a WHERE a.id=e.src AND a.status='active')
                  AND EXISTS (SELECT 1 FROM crystallizations b WHERE b.id=e.dst AND b.status='active')""",
            params + params,
        ).fetchall()
        # Group undirected neighbours per frontier node, then cap fan-out so one
        # hub crystal cannot explode the expansion (Hindsight per-entity LATERAL cap).
        # Preserve multi-edge multiplicity while still capping unique neighbours.
        by_node: dict[str, list[str]] = {node: [] for node in frontier}
        for row in rows:
            for node, other in ((row["src"], row["dst"]), (row["dst"], row["src"])):
                if node not in frontier:
                    continue
                if other in frontier or other in seeds:
                    continue
                by_node[node].append(other)
        nxt: set[str] = set()
        for node, others in by_node.items():
            # Stable order keeps the cap deterministic across SQLite builds.
            counts: dict[str, int] = {}
            for other in others:
                counts[other] = counts.get(other, 0) + 1
            capped = sorted(counts)[: max(0, per_seed_limit)]
            for other in capped:
                add = counts[other]
                if other in visited and other not in reached:
                    continue
                prev = reached.get(other)
                if prev is None:
                    reached[other] = (hop, add)
                    nxt.add(other)
                    visited.add(other)
                else:
                    reached[other] = (min(prev[0], hop), prev[1] + add)
        frontier = nxt
    return reached


def _page_hit(con: sqlite3.Connection, crystal_id: str, query: str) -> tuple[str, dict] | None:
    from rogmem.crystal_index import excerpt, readable_text

    page = con.execute(
        """SELECT id, name, status, IFNULL(body_md,'') AS body_md, IFNULL(sketch_md,'') AS sketch_md
           FROM crystallizations WHERE id=?""",
        (crystal_id,),
    ).fetchone()
    if not page or page["status"] != "active":
        return None
    shown = readable_text(page["name"] or "", page["body_md"] or "", page["sketch_md"] or "")
    return (
        "page:" + page["id"],
        {
            "crystal": page["name"] or "",
            "field": "正文",
            "text": excerpt(shown, query) if shown else (page["name"] or ""),
            "source_id": "",
            "match": "图谱",
        },
    )


def _statement_hits(con: sqlite3.Connection, crystal_id: str, *, limit: int = 3) -> list[tuple[str, dict]]:
    rows = con.execute(
        """SELECT s.id, s.field, s.text, s.source_id, c.name AS crystal
           FROM statements s
           JOIN crystallizations c ON c.id=s.crystal_id
           WHERE s.crystal_id=? AND s.invalid_at IS NULL AND s.trust='written'
           ORDER BY s.created_at DESC
           LIMIT ?""",
        (crystal_id, limit),
    ).fetchall()
    hits: list[tuple[str, dict]] = []
    for row in rows:
        hits.append(
            (
                row["id"],
                {
                    "crystal": row["crystal"] or "",
                    "field": row["field"],
                    "text": row["text"],
                    "source_id": row["source_id"],
                    "match": "图谱",
                },
            )
        )
    return hits


def graph_lane(
    con: sqlite3.Connection,
    query: str,
    *,
    identity_crystal_ids: list[str],
    statement_crystal_ids: list[str],
    budget: int,
) -> list[tuple[str, dict]]:
    """Return (ranked hits, pre-budget count) from edge expansion."""
    seeds = graph_seed_ids(
        con,
        query,
        identity_crystal_ids=identity_crystal_ids,
        statement_crystal_ids=statement_crystal_ids,
    )
    if not seeds:
        return [], 0
    neighbours = _neighbours(con, seeds, hops=GRAPH_HOPS)
    if not neighbours:
        return [], 0
    scored: list[tuple[float, str, dict]] = []
    for cid, (hop, edges) in neighbours.items():
        # tanh(edge_count * 0.5) mirrors Hindsight entity-link saturation;
        # hop decay mirrors temporal/causal propagation decay spirit (0.7^hop).
        activation = math.tanh(edges * 0.5) * (0.7 ** (hop - 1))
        page = _page_hit(con, cid, query)
        if page is not None:
            scored.append((activation + 0.05, page[0], page[1]))
        for key, payload in _statement_hits(con, cid):
            scored.append((activation, key, payload))
    scored.sort(key=lambda item: (-item[0], item[1]))
    out: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for _score, key, payload in scored:
        if key in seen:
            continue
        seen.add(key)
        out.append((key, payload))
    return out[:budget], len(out)


def temporal_lane(
    con: sqlite3.Connection,
    query: str,
    *,
    budget: int,
    reference_date: datetime | None = None,
    parse_event: Callable[[object], datetime | None] | None = None,
) -> list[tuple[str, dict]]:
    """Return (ranked hits, pre-budget count) whose ``event_at`` is in-window."""
    window = extract_temporal_window(query, reference_date)
    if window is None:
        return [], 0
    start, end = window
    residual = strip_temporal_phrases(query)
    parse = parse_event or _parse_event_at
    # Pull a pool of dated written statements; prefer speech/event clocks.
    rows = con.execute(
        """SELECT s.id, s.crystal_id, s.field, s.text, s.source_id, s.event_at,
                  c.name AS crystal,
                  src.event_at AS source_event_at,
                  src.event_basis AS source_event_basis,
                  src.ingested_at AS source_ingested_at
           FROM statements s
           JOIN crystallizations c ON c.id=s.crystal_id AND c.status='active'
           LEFT JOIN sources src ON src.id=s.source_id
           WHERE s.invalid_at IS NULL AND s.trust='written'
             AND (
               (s.event_at IS NOT NULL AND s.event_at != '')
               OR (src.event_at IS NOT NULL AND src.event_at != '')
             )
           ORDER BY s.created_at DESC
           LIMIT 400"""
    ).fetchall()
    pool: list[dict] = []
    for row in rows:
        event = parse(row["event_at"])
        if event is None:
            # Fall back to source event_at only when it is a real event clock,
            # not a file mtime or a row that merely copied ingested_at.
            basis = str(row["source_event_basis"] or "").strip()
            if basis == "file":
                continue
            event = parse(row["source_event_at"])
            if event is None:
                continue
            if not basis:
                arrived = parse(row["source_ingested_at"])
                if arrived is not None and event == arrived:
                    continue
        if event < start or event > end:
            continue
        text = row["text"] or ""
        rel = _token_overlap(residual, text) if residual else 0.0
        mid = start + (end - start) / 2
        span = (end - start).total_seconds()
        if span > 0:
            proximity = 1.0 - min(abs((event - mid).total_seconds()) / (span / 2), 1.0)
        else:
            proximity = 1.0
        # Similarity-first (Hindsight temporal arm); proximity is a tie-break boost.
        similarity = rel * 0.85 + proximity * 0.15 if residual else proximity
        pool.append(
            {
                "id": row["id"],
                "similarity": similarity,
                "event_at": event,
                "payload": {
                    "crystal": row["crystal"] or "",
                    "field": row["field"],
                    "text": text,
                    "source_id": row["source_id"],
                    "match": "时间",
                },
            }
        )
    if not pool:
        return [], 0
    # Similarity-first pool (Hindsight temporal arm), then coverage buckets.
    pool.sort(key=lambda r: float(r["similarity"]), reverse=True)
    entry_limit = max(budget, TEMPORAL_ENTRY_POINTS)
    selected = select_with_temporal_coverage(
        pool[:TEMPORAL_POOL],
        start,
        end,
        limit=min(entry_limit, len(pool)),
        n_buckets=TEMPORAL_BUCKETS,
    )
    # Re-sort selected by similarity for RRF rank positions.
    selected.sort(key=lambda r: float(r["similarity"]), reverse=True)
    hits = [(row["id"], row["payload"]) for row in selected]
    return hits[:budget], len(hits)
