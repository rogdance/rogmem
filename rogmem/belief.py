"""Belief consolidation: observation-like units above cite-gated statements.

Hindsight spirit (proof set + near-dup reconcile) without dropping RogMem
gates: every belief source keeps a continuous original quote, and vectors
only probe near-duplicate *beliefs* — never names_correspond / entity
uniqueness.
"""

from __future__ import annotations

import json
import math
import re
import secrets
import unicodedata
from difflib import SequenceMatcher
from typing import Any

# Hindsight DEFAULT_CONSOLIDATION_DEDUP_THRESHOLD
DEDUP_COSINE = 0.97
# When embed is off / unavailable: text affinity for the same probe role.
DEDUP_TEXT_RATIO = 0.92
DEDUP_TOP_K = 5
# Substring reinforce needs enough mass that incidental overlap is unlikely.
# Four characters (old floor) lets "18080" / short slots inflate proof.
REINFORCE_QUOTE_MIN = 12

# Include CJK sentence punctuation so "断电。" and "断电" share a fold key.
_SEPARATORS = re.compile(r"[\s_\-·.．。!！?？;；:：,，、/\"'“”‘’「」『』]+")
_DIGIT_RUN = re.compile(r"\d+")
_NEGATION = re.compile(
    r"(不|没|無|无|非|未|勿|别|別|并非|並非|never|not|no|without)\s*",
    re.IGNORECASE,
)


def ensure_belief_schema(con) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS beliefs (
             id TEXT PRIMARY KEY,
             crystal_id TEXT NOT NULL,
             place TEXT NOT NULL DEFAULT '',
             text TEXT NOT NULL,
             quote TEXT NOT NULL,
             source_id TEXT NOT NULL,
             event_at TEXT,
             created_at TEXT NOT NULL,
             invalid_at TEXT,
             merged_into TEXT,
             vec_json TEXT
           )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS belief_sources (
             belief_id TEXT NOT NULL,
             source_id TEXT NOT NULL,
             statement_id TEXT,
             quote TEXT NOT NULL,
             created_at TEXT NOT NULL,
             PRIMARY KEY (belief_id, source_id)
           )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS belief_statements (
             belief_id TEXT NOT NULL,
             statement_id TEXT NOT NULL,
             PRIMARY KEY (belief_id, statement_id)
           )"""
    )
    con.execute(
        """CREATE INDEX IF NOT EXISTS beliefs_crystal_place
           ON beliefs(crystal_id, place)
           WHERE invalid_at IS NULL AND IFNULL(merged_into, '') = ''"""
    )


def belief_place(field: str | None, chapter: str | None) -> str:
    return str(chapter or "").strip() or str(field or "").strip()


def fold_belief_text(text: str) -> str:
    return _SEPARATORS.sub("", unicodedata.normalize("NFKC", text or "").casefold())


def text_affinity(left: str, right: str) -> float:
    """Deterministic near-dup score in [0, 1]. Not used for name uniqueness."""
    a = fold_belief_text(left)
    b = fold_belief_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def texts_conflict(left: str, right: str) -> bool:
    """Hard keep: digit runs or negation polarity disagree."""
    a = fold_belief_text(left)
    b = fold_belief_text(right)
    if not a or not b:
        return False
    digits_a = _DIGIT_RUN.findall(a)
    digits_b = _DIGIT_RUN.findall(b)
    if digits_a and digits_b and digits_a != digits_b:
        return True
    neg_a = 1 if _NEGATION.search(left or "") else 0
    neg_b = 1 if _NEGATION.search(right or "") else 0
    if neg_a != neg_b and text_affinity(left, right) >= 0.75:
        return True
    return False


def cosine(left: list[float] | None, right: list[float] | None) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    ln = math.sqrt(sum(a * a for a in left))
    rn = math.sqrt(sum(b * b for b in right))
    if ln == 0.0 or rn == 0.0:
        return 0.0
    return dot / (ln * rn)


def should_merge(anchor: str, candidate: str, similarity: float, *, threshold: float) -> bool:
    if similarity < threshold:
        return False
    if texts_conflict(anchor, candidate):
        return False
    return True


def parse_vec(raw: str | None) -> list[float] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, list) or not data:
        return None
    try:
        return [float(x) for x in data]
    except (TypeError, ValueError):
        return None


def _utcnow() -> str:
    from rogmem.engine import utcnow

    return utcnow()


def _embed_text(text: str) -> list[float] | None:
    """Optional dense probe. Failures become text-affinity fallback.

    Belief landing runs inside the page's write transaction. An embedding call there
    would hold SQLite's single write lock for the length of a network or CPU wait, so
    inside an open write transaction the probe falls back to text affinity instead.
    """
    from rogmem import engine as eng

    if getattr(eng, "_EMBED", "off") == "off":
        return None
    con = getattr(eng._LOCAL, "con", None)
    try:
        # hash 模式是本地算一次摘要，不等任何东西；真模型才需要避开写事务。
        if eng._EMBED != "hash" and con is not None and con.in_transaction:
            return None
    except Exception:
        pass
    try:
        return eng._embed_one(text)
    except Exception:
        return None


def belief_proof_count(con, belief_id: str) -> int:
    bid = str(belief_id or "").strip()
    if not bid:
        return 0
    row = con.execute(
        "SELECT COUNT(*) AS n FROM belief_sources WHERE belief_id=?",
        (bid,),
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def statement_belief_id(con, statement_id: str) -> str | None:
    sid = str(statement_id or "").strip()
    if not sid:
        return None
    row = con.execute(
        """SELECT bs.belief_id AS belief_id
           FROM belief_statements bs
           JOIN beliefs b ON b.id = bs.belief_id
           WHERE bs.statement_id=?
             AND b.invalid_at IS NULL
             AND IFNULL(b.merged_into, '') = ''
           LIMIT 1""",
        (sid,),
    ).fetchone()
    return str(row["belief_id"]) if row else None


def proof_count_for_statement(con, statement_id: str) -> int:
    """Evidence strength: max(statement_sources, owning belief sources)."""
    sid = str(statement_id or "").strip()
    if not sid or sid.startswith("page:"):
        return 0
    row = con.execute(
        "SELECT COUNT(*) AS n FROM statement_sources WHERE statement_id=?",
        (sid,),
    ).fetchone()
    stmt_n = int(row["n"] or 0) if row else 0
    if stmt_n <= 0:
        st = con.execute("SELECT source_id FROM statements WHERE id=?", (sid,)).fetchone()
        stmt_n = 1 if st and str(st["source_id"] or "").strip() else 0
    bid = statement_belief_id(con, sid)
    belief_n = belief_proof_count(con, bid) if bid else 0
    return max(stmt_n, belief_n)


def _link_belief_source(
    con,
    belief_id: str,
    source_id: str,
    quote: str,
    statement_id: str | None,
) -> None:
    q = str(quote or "").strip()
    if not belief_id or not source_id or not q:
        # Citation gate: no quote → no belief source row.
        return
    con.execute(
        """INSERT INTO belief_sources(belief_id, source_id, statement_id, quote, created_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(belief_id, source_id) DO NOTHING""",
        (belief_id, source_id, statement_id, q, _utcnow()),
    )


def _link_belief_statement(con, belief_id: str, statement_id: str) -> None:
    if not belief_id or not statement_id:
        return
    con.execute(
        """INSERT INTO belief_statements(belief_id, statement_id)
           VALUES(?,?)
           ON CONFLICT(belief_id, statement_id) DO NOTHING""",
        (belief_id, statement_id),
    )


def _active_beliefs(con, crystal_id: str, place: str) -> list[Any]:
    return list(
        con.execute(
            """SELECT id, text, quote, source_id, vec_json, event_at, created_at
               FROM beliefs
               WHERE crystal_id=? AND place=?
                 AND invalid_at IS NULL
                 AND IFNULL(merged_into, '') = ''
               ORDER BY COALESCE(event_at, created_at), created_at""",
            (crystal_id, place),
        )
    )


def probe_near_dup(
    con,
    *,
    crystal_id: str,
    place: str,
    text: str,
    exclude_id: str | None = None,
    anchor_vec: list[float] | None = None,
) -> tuple[Any | None, float, float]:
    """Return (best_row, similarity, threshold_used).

    Embedding is a probe only. Threshold defaults to Hindsight 0.97 when a
    vector is present; otherwise text affinity at DEDUP_TEXT_RATIO.
    """
    rows = _active_beliefs(con, crystal_id, place)
    if not rows:
        return None, 0.0, DEDUP_TEXT_RATIO
    vec = anchor_vec if anchor_vec is not None else _embed_text(text)
    use_vec = vec is not None
    threshold = DEDUP_COSINE if use_vec else DEDUP_TEXT_RATIO
    scored: list[tuple[float, Any]] = []
    for row in rows:
        rid = str(row["id"])
        if exclude_id and rid == exclude_id:
            continue
        if use_vec:
            other = parse_vec(row["vec_json"])
            if other is None:
                sim = text_affinity(text, row["text"])
                # Mixed: require text floor so a missing twin vector cannot win
                # solely on weak char overlap under the cosine threshold.
                if sim < DEDUP_TEXT_RATIO:
                    continue
            else:
                sim = cosine(vec, other)
        else:
            sim = text_affinity(text, row["text"])
        if sim >= threshold:
            scored.append((sim, row))
    if not scored:
        return None, 0.0, threshold
    scored.sort(key=lambda item: (-item[0], str(item[1]["id"])))
    best_sim, best = scored[0]
    return best, best_sim, threshold


def _create_belief(
    con,
    *,
    crystal_id: str,
    place: str,
    text: str,
    quote: str,
    source_id: str,
    event_at: str | None,
    statement_id: str | None,
    vec: list[float] | None,
) -> str:
    bid = secrets.token_hex(8)
    con.execute(
        """INSERT INTO beliefs(
             id, crystal_id, place, text, quote, source_id, event_at, created_at, vec_json
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            bid,
            crystal_id,
            place,
            text,
            quote,
            source_id,
            event_at,
            _utcnow(),
            json.dumps(vec) if vec else None,
        ),
    )
    _link_belief_source(con, bid, source_id, quote, statement_id)
    if statement_id:
        _link_belief_statement(con, bid, statement_id)
    return bid


def _fold_into_belief(
    con,
    *,
    twin_id: str,
    text: str,
    quote: str,
    source_id: str,
    statement_id: str | None,
    vec: list[float] | None,
) -> str:
    """Accumulate evidence on the twin. Prefer longer canonical wording."""
    twin = con.execute(
        "SELECT id, text, vec_json FROM beliefs WHERE id=?",
        (twin_id,),
    ).fetchone()
    if twin is None:
        return twin_id
    new_text = text if len(text or "") > len(str(twin["text"] or "")) else twin["text"]
    updates = ["text=?"]
    args: list[Any] = [new_text]
    if vec and not twin["vec_json"]:
        updates.append("vec_json=?")
        args.append(json.dumps(vec))
    args.append(twin_id)
    con.execute(f"UPDATE beliefs SET {', '.join(updates)} WHERE id=?", args)
    _link_belief_source(con, twin_id, source_id, quote, statement_id)
    if statement_id:
        _link_belief_statement(con, twin_id, statement_id)
    return twin_id


def attach_statement_belief(
    con,
    *,
    statement_id: str,
    crystal_id: str,
    field: str | None,
    chapter: str | None,
    text: str,
    quote: str,
    source_id: str,
    event_at: str | None = None,
    trust: str = "written",
) -> str | None:
    """Land or reinforce a belief for one cite-gated statement.

    Returns belief id, or None when citation/trust gates refuse.
    """
    ensure_belief_schema(con)
    sid = str(statement_id or "").strip()
    cid = str(crystal_id or "").strip()
    body = str(text or "").strip()
    q = str(quote or "").strip()
    src = str(source_id or "").strip()
    if not sid or not cid or not body or not q or not src:
        return None
    if trust != "written":
        return None
    place = belief_place(field, chapter)
    if not place:
        return None

    existing = statement_belief_id(con, sid)
    if existing:
        _link_belief_source(con, existing, src, q, sid)
        return existing

    # Exact fold key → same belief as any statement already carrying this wording.
    exact = con.execute(
        """SELECT b.id AS id
           FROM beliefs b
           WHERE b.crystal_id=? AND b.place=? AND b.text=?
             AND b.invalid_at IS NULL AND IFNULL(b.merged_into, '') = ''
           LIMIT 1""",
        (cid, place, body),
    ).fetchone()
    if exact:
        bid = str(exact["id"])
        _link_belief_source(con, bid, src, q, sid)
        _link_belief_statement(con, bid, sid)
        return bid

    vec = _embed_text(body)
    twin, sim, threshold = probe_near_dup(
        con, crystal_id=cid, place=place, text=body, anchor_vec=vec
    )
    if twin is not None and should_merge(body, twin["text"], sim, threshold=threshold):
        return _fold_into_belief(
            con,
            twin_id=str(twin["id"]),
            text=body,
            quote=q,
            source_id=src,
            statement_id=sid,
            vec=vec,
        )
    return _create_belief(
        con,
        crystal_id=cid,
        place=place,
        text=body,
        quote=q,
        source_id=src,
        event_at=event_at,
        statement_id=sid,
        vec=vec,
    )


def reinforce_restated_statements(
    con,
    *,
    crystal_id: str,
    source_id: str,
    body: str,
) -> int:
    """Unified landing: material that again contains a live quote reinforces proof.

    Not a merge-JSON special case — any body that re-cites an existing continuous
    quote attaches that source to the statement and its belief.
    """
    ensure_belief_schema(con)
    cid = str(crystal_id or "").strip()
    src = str(source_id or "").strip()
    hay = str(body or "")
    if not cid or not src or not hay:
        return 0
    reinforced = 0
    rows = con.execute(
        """SELECT id, field, chapter, text, quote, trust
           FROM statements
           WHERE crystal_id=? AND invalid_at IS NULL AND trust='written'""",
        (cid,),
    ).fetchall()
    for row in rows:
        quote = str(row["quote"] or "").strip()
        if len(quote) < REINFORCE_QUOTE_MIN or quote not in hay:
            continue
        from rogmem.engine import _link_statement_source

        _link_statement_source(con, row["id"], src, quote)
        attach_statement_belief(
            con,
            statement_id=str(row["id"]),
            crystal_id=cid,
            field=row["field"],
            chapter=row["chapter"],
            text=str(row["text"] or ""),
            quote=quote,
            source_id=src,
            trust="written",
        )
        reinforced += 1
    return reinforced
