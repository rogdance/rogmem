"""Cross-page standing sketches: question → answer, ready for core.

A standing sketch is a projected answer over a scoped set of crystallizations.
It is not a per-crystal sketch slot, and it is not free-form LLM cache.

Contract:
- core read path only touches ``standing_sketches`` (no crystallizations).
- Refresh runs off that path (after crystal body/sketch writes) and reassembles
  from already-accepted crystal sketches and quoted statements.
- No citeable source → empty answer (never invent an unsourced wall).
- Scope crystal body/sketch change → mark stale → refresh before the next core pack.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable

ANSWER_CHAR_CAP = 2000
_PROJECT_SEP = "::"

_DDL = """
CREATE TABLE IF NOT EXISTS standing_sketches (
  id TEXT PRIMARY KEY,
  question TEXT NOT NULL,
  answer_md TEXT NOT NULL DEFAULT '',
  source_crystal_ids TEXT NOT NULL DEFAULT '[]',
  source_fingerprint TEXT NOT NULL DEFAULT '',
  cite_json TEXT NOT NULL DEFAULT '[]',
  refreshed_at TEXT NOT NULL DEFAULT '',
  stale INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS standing_sketches_stale ON standing_sketches(stale);
"""


@dataclass(frozen=True)
class StandingSpec:
    id: str
    question: str
    scope_ids: tuple[str, ...] = ()
    scope_types: tuple[str, ...] = ()
    prefer_tokens: tuple[str, ...] = ()
    # When true, project overlays live as id::repo rows; baseline id stays owner-only.
    project_filter: bool = False


BUILTIN_SPECS: tuple[StandingSpec, ...] = (
    StandingSpec(
        id="owner-collaboration",
        question="记忆主人协作约定",
        scope_ids=("owner",),
        prefer_tokens=(
            "协作",
            "约定",
            "偏好",
            "沟通",
            "确认",
            "不得",
            "禁止",
            "不要",
            "先问",
            "验收",
            "核验",
        ),
    ),
    StandingSpec(
        id="engineering-invariants",
        question="当前工程不可改约束",
        scope_ids=("owner",),
        scope_types=("工程",),
        project_filter=True,
        prefer_tokens=(
            "端口",
            "18080",
            "18081",
            "卷",
            "rogmem-data",
            "rogmem-scan",
            "项目 id",
            "项目id",
            "不可",
            "不得",
            "生产",
            "清空",
            "重置",
            "删除卷",
            "改端口",
        ),
    ),
)


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(_DDL)


def ensure_registry(con: sqlite3.Connection) -> None:
    """Seed builtin baseline questions. Overlay rows are created on refresh."""
    ensure_schema(con)
    for spec in BUILTIN_SPECS:
        row = con.execute("SELECT id, question FROM standing_sketches WHERE id=?", (spec.id,)).fetchone()
        if row is None:
            con.execute(
                """INSERT INTO standing_sketches(
                     id, question, answer_md, source_crystal_ids, source_fingerprint,
                     cite_json, refreshed_at, stale
                   ) VALUES(?,?, '', '[]', '', '[]', '', 1)""",
                (spec.id, spec.question),
            )
        elif (row["question"] if hasattr(row, "keys") else row[1]) != spec.question:
            # 只在问题文字真的变了时才写：无变化的 UPDATE 也会开写事务、占住写锁。
            con.execute(
                "UPDATE standing_sketches SET question=? WHERE id=?",
                (spec.question, spec.id),
            )


def base_id(sketch_id: str) -> str:
    return (sketch_id or "").split(_PROJECT_SEP, 1)[0]


def overlay_id(spec_id: str, project: str) -> str:
    key = _project_key(project)
    if not key:
        return spec_id
    return f"{spec_id}{_PROJECT_SEP}{key}"


def _project_key(project: str) -> str:
    raw = (project or "").strip()
    if not raw:
        return ""
    from rogmem.hermes_memory import repository_slug

    slug = repository_slug(raw, declared=True) or repository_slug(raw)
    if slug:
        return slug.lower()
    return raw.lower()


def spec_by_id(sketch_id: str) -> StandingSpec | None:
    wanted = base_id(sketch_id)
    for spec in BUILTIN_SPECS:
        if spec.id == wanted:
            return spec
    return None


def _project_hits(ref: str, address: str) -> bool:
    if not ref or not address:
        return False
    from rogmem.hermes_memory import repository_slug

    left = repository_slug(ref, declared=True) or repository_slug(ref)
    right = repository_slug(address)
    if left and right:
        return left == right
    return ref.strip() == address.strip()


def resolve_scope(
    con: sqlite3.Connection,
    spec: StandingSpec,
    *,
    project: str = "",
) -> list[str]:
    """Crystal ids this standing sketch may read. Order is stable."""
    found: list[str] = []
    seen: set[str] = set()

    def add(cid: str) -> None:
        if cid and cid not in seen:
            seen.add(cid)
            found.append(cid)

    for cid in spec.scope_ids:
        row = con.execute(
            "SELECT id FROM crystallizations WHERE id=? AND status='active'",
            (cid,),
        ).fetchone()
        if row:
            add(row["id"])

    wanted = (project or "").strip()
    if spec.scope_types:
        if spec.project_filter and not wanted:
            return found
        rows = con.execute(
            """SELECT id, name, envelope_json, body_md FROM crystallizations
               WHERE status='active' AND type IN ({})""".format(
                ",".join("?" * len(spec.scope_types))
            ),
            tuple(spec.scope_types),
        ).fetchall()
        for row in rows:
            if spec.project_filter and wanted:
                repo = ""
                try:
                    env = json.loads(row["envelope_json"] or "{}")
                    keys = env.get("keys") if isinstance(env, dict) else {}
                    if isinstance(keys, dict):
                        repo = str(keys.get("仓库地址") or "")
                except json.JSONDecodeError:
                    repo = ""
                hay = "\n".join([repo, row["name"] or "", row["body_md"] or ""])
                if not (
                    _project_hits(wanted, repo)
                    or _project_hits(wanted, row["name"] or "")
                    or wanted in hay
                ):
                    continue
            add(row["id"])
    return found


def _token_hit(text: str, tokens: Iterable[str]) -> bool:
    body = text or ""
    if not tokens:
        return True
    return any(tok and tok in body for tok in tokens)


def _split_blocks(text: str) -> list[str]:
    parts = re.split(r"\n\s*\n+", (text or "").strip())
    return [p.strip() for p in parts if p and p.strip()]


def _fingerprint(parts: list[dict]) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _gather_material(
    con: sqlite3.Connection,
    crystal_ids: list[str],
    prefer_tokens: tuple[str, ...],
) -> tuple[list[dict], list[dict]]:
    """Return (cite pieces for assembly, fingerprint parts)."""
    cites: list[dict] = []
    prints: list[dict] = []
    for cid in crystal_ids:
        row = con.execute(
            """SELECT id, name, IFNULL(sketch_md,'') AS sketch_md, IFNULL(body_md,'') AS body_md
               FROM crystallizations WHERE id=? AND status='active'""",
            (cid,),
        ).fetchone()
        if row is None:
            continue
        name = row["name"] or cid
        sketch = (row["sketch_md"] or "").strip()
        sketch_blocks = _split_blocks(sketch)
        matched = [b for b in sketch_blocks if _token_hit(b, prefer_tokens)]
        # Prefer token-matched sketch blocks; if none match but tokens exist, skip sketch
        # rather than dumping the whole page (keeps the answer on-question).
        use_blocks = matched if prefer_tokens else sketch_blocks
        if use_blocks:
            text = "\n\n".join(use_blocks).strip()
            if text:
                cites.append(
                    {
                        "kind": "sketch",
                        "crystal_id": cid,
                        "crystal_name": name,
                        "text": text,
                    }
                )
                prints.append({"crystal_id": cid, "kind": "sketch", "text": text})

        stmts = con.execute(
            """SELECT id, field, chapter, text, quote FROM statements
               WHERE crystal_id=? AND invalid_at IS NULL AND trust='written'
               ORDER BY created_at ASC, id ASC""",
            (cid,),
        ).fetchall()
        for stmt in stmts:
            quote = (stmt["quote"] or "").strip()
            claim = (stmt["text"] or "").strip()
            if not quote or not claim:
                continue
            hay = "\n".join(
                [
                    claim,
                    quote,
                    stmt["field"] or "",
                    stmt["chapter"] or "",
                ]
            )
            if prefer_tokens and not _token_hit(hay, prefer_tokens):
                continue
            cites.append(
                {
                    "kind": "statement",
                    "crystal_id": cid,
                    "crystal_name": name,
                    "statement_id": stmt["id"],
                    "text": claim,
                    "quote": quote,
                }
            )
            prints.append(
                {
                    "crystal_id": cid,
                    "kind": "statement",
                    "statement_id": stmt["id"],
                    "text": claim,
                    "quote": quote,
                }
            )
    return cites, prints


def assemble_answer(question: str, cites: list[dict], *, cap: int = ANSWER_CHAR_CAP) -> str:
    """Build markdown from cited pieces only. Empty cites → empty answer."""
    if not cites:
        return ""
    chunks: list[str] = [question.strip(), ""]
    for item in cites:
        name = item.get("crystal_name") or item.get("crystal_id") or ""
        if item.get("kind") == "sketch":
            chunks.append(f"（来自「{name}」速写）")
            chunks.append(str(item.get("text") or "").strip())
            chunks.append("")
        else:
            quote = str(item.get("quote") or "").strip()
            claim = str(item.get("text") or "").strip()
            chunks.append(f"（来自「{name}」陈述；引文连续原文）")
            chunks.append(claim)
            if quote:
                chunks.append(f"> {quote}")
            chunks.append("")
    text = "\n".join(chunks).strip()
    if len(text) <= cap:
        return text
    keep = text[: cap - 1].rstrip()
    cut = keep.rfind("\n\n")
    if cut > len(question) + 2:
        keep = keep[:cut].rstrip()
    return keep + "…"


def mark_stale_for_crystal(con: sqlite3.Connection, crystal_id: str) -> int:
    """Mark standing sketches whose stored answer depends on this crystal."""
    ensure_schema(con)
    cid = (crystal_id or "").strip()
    if not cid:
        return 0
    changed = 0
    for row in con.execute("SELECT id, source_crystal_ids FROM standing_sketches").fetchall():
        try:
            ids = json.loads(row["source_crystal_ids"] or "[]")
        except json.JSONDecodeError:
            ids = []
        if not isinstance(ids, list):
            ids = []
        spec = spec_by_id(row["id"])
        in_registry = bool(spec and cid in spec.scope_ids)
        in_last = cid in [str(x) for x in ids]
        if in_registry or in_last:
            con.execute("UPDATE standing_sketches SET stale=1 WHERE id=?", (row["id"],))
            changed += 1
    return changed


def _utcnow() -> str:
    from rogmem.engine import utcnow

    return utcnow()


def refresh_one(
    con: sqlite3.Connection,
    sketch_id: str,
    *,
    project: str = "",
    now: str = "",
) -> dict:
    ensure_registry(con)
    spec = spec_by_id(sketch_id)
    if spec is None:
        return {"ok": False, "error": "unknown standing sketch"}
    row_id = overlay_id(spec.id, project) if (project and spec.project_filter) else spec.id
    question = spec.question
    row = con.execute("SELECT id FROM standing_sketches WHERE id=?", (row_id,)).fetchone()
    if row is None:
        con.execute(
            """INSERT INTO standing_sketches(
                 id, question, answer_md, source_crystal_ids, source_fingerprint,
                 cite_json, refreshed_at, stale
               ) VALUES(?,?, '', '[]', '', '[]', '', 1)""",
            (row_id, question),
        )
    scope = resolve_scope(con, spec, project=project if spec.project_filter else "")
    cites, prints = _gather_material(con, scope, spec.prefer_tokens)
    answer = assemble_answer(question, cites)
    fp = _fingerprint(prints)
    stamp = now or _utcnow()
    con.execute(
        """UPDATE standing_sketches SET
             question=?, answer_md=?, source_crystal_ids=?, source_fingerprint=?,
             cite_json=?, refreshed_at=?, stale=0
           WHERE id=?""",
        (
            question,
            answer,
            json.dumps(scope, ensure_ascii=False),
            fp,
            json.dumps(cites, ensure_ascii=False),
            stamp,
            row_id,
        ),
    )
    return {
        "ok": True,
        "id": row_id,
        "question": question,
        "answer_md": answer,
        "source_crystal_ids": scope,
        "cite_count": len(cites),
        "fingerprint": fp,
        "empty": not bool(answer),
    }


def ensure_fresh(
    con: sqlite3.Connection,
    *,
    project: str = "",
    force: bool = False,
    commit: bool = False,
) -> list[dict]:
    """Rebuild stale (or all) baseline rows. Optionally also refresh one project overlay."""
    ensure_registry(con)
    out: list[dict] = []
    rows = con.execute(
        "SELECT id, stale FROM standing_sketches WHERE id NOT LIKE ? ORDER BY id",
        (f"%{_PROJECT_SEP}%",),
    ).fetchall()
    for row in rows:
        if force or int(row["stale"] or 0) == 1:
            out.append(refresh_one(con, row["id"], project=""))
    wanted = (project or "").strip()
    if wanted:
        for spec in BUILTIN_SPECS:
            if not spec.project_filter:
                continue
            oid = overlay_id(spec.id, wanted)
            overlay = con.execute(
                "SELECT stale FROM standing_sketches WHERE id=?",
                (oid,),
            ).fetchone()
            if force or overlay is None or int(overlay["stale"] or 0) == 1:
                out.append(refresh_one(con, spec.id, project=wanted))
    if commit and out:
        con.commit()
    return out


def refresh_after_crystal_change(
    con: sqlite3.Connection,
    crystal_id: str,
    *,
    commit: bool = False,
) -> list[dict]:
    """Mark + rebuild baselines (and project overlays that listed this crystal)."""
    ensure_registry(con)
    mark_stale_for_crystal(con, crystal_id)
    out = ensure_fresh(con, project="", force=False, commit=False)
    # Rebuild overlays that already listed this crystal.
    cid = (crystal_id or "").strip()
    for row in con.execute(
        "SELECT id, source_crystal_ids, stale FROM standing_sketches WHERE id LIKE ?",
        (f"%{_PROJECT_SEP}%",),
    ).fetchall():
        try:
            ids = json.loads(row["source_crystal_ids"] or "[]")
        except json.JSONDecodeError:
            ids = []
        if cid and cid in [str(x) for x in ids]:
            con.execute("UPDATE standing_sketches SET stale=1 WHERE id=?", (row["id"],))
            base = base_id(row["id"])
            project = row["id"].split(_PROJECT_SEP, 1)[1]
            out.append(refresh_one(con, base, project=project))
    # New 工程 page: also materialize its overlay so core(project=…) can hit it next.
    crow = con.execute(
        "SELECT type, envelope_json, name FROM crystallizations WHERE id=?",
        (cid,),
    ).fetchone()
    if crow and (crow["type"] or "") == "工程":
        repo = ""
        try:
            env = json.loads(crow["envelope_json"] or "{}")
            keys = env.get("keys") if isinstance(env, dict) else {}
            if isinstance(keys, dict):
                repo = str(keys.get("仓库地址") or "")
        except json.JSONDecodeError:
            repo = ""
        project = repo or (crow["name"] or "")
        if project:
            for spec in BUILTIN_SPECS:
                if spec.project_filter:
                    out.append(refresh_one(con, spec.id, project=project))
    if commit and out:
        con.commit()
    return out


def list_ready(con: sqlite3.Connection) -> list[dict]:
    ensure_registry(con)
    rows = con.execute(
        """SELECT id, question, answer_md, source_crystal_ids, cite_json,
                  refreshed_at, stale, source_fingerprint
           FROM standing_sketches ORDER BY id"""
    ).fetchall()
    items = []
    for row in rows:
        answer = (row["answer_md"] or "").strip()
        items.append(
            {
                "id": row["id"],
                "question": row["question"] or base_id(row["id"]),
                "answer_md": answer,
                "source_crystal_ids": json.loads(row["source_crystal_ids"] or "[]"),
                "cite_json": json.loads(row["cite_json"] or "[]"),
                "refreshed_at": row["refreshed_at"] or "",
                "stale": int(row["stale"] or 0) == 1,
                "fingerprint": row["source_fingerprint"] or "",
                "ready": bool(answer),
            }
        )
    return items


def pack_for_core(con: sqlite3.Connection, *, project: str = "") -> list[dict]:
    """Thin core read: only ``standing_sketches``. Prefer project overlay when present.

    Stale rows are omitted — refresh belongs on write hooks, never as a silent
    crystallizations scan inside core. Serving stale answer_md would be an
    unsourced-looking wall relative to the current crystal state.
    """
    ensure_schema(con)
    wanted = _project_key(project)
    rows = con.execute(
        """SELECT id, question, answer_md, stale FROM standing_sketches
           WHERE IFNULL(answer_md,'') != '' AND IFNULL(stale, 1) = 0
           ORDER BY id"""
    ).fetchall()
    baselines: dict[str, dict] = {}
    overlays: dict[str, dict] = {}
    for row in rows:
        answer = (row["answer_md"] or "").strip()
        if not answer:
            continue
        question = row["question"] or base_id(row["id"])
        item = {"name": question, "text": answer}
        rid = row["id"]
        if _PROJECT_SEP in rid:
            key = rid.split(_PROJECT_SEP, 1)[1]
            overlays[(base_id(rid), key)] = item
        else:
            baselines[rid] = item
    sections: list[dict] = []
    seen_q: set[str] = set()
    for spec in BUILTIN_SPECS:
        chosen = None
        if wanted and spec.project_filter:
            chosen = overlays.get((spec.id, wanted))
        if chosen is None:
            chosen = baselines.get(spec.id)
        if chosen is None:
            continue
        sections.append(chosen)
        seen_q.add(chosen["name"])
    # Any non-builtin ready rows (future curated) keep insertion order after builtins.
    for row in rows:
        if _PROJECT_SEP in row["id"]:
            continue
        if row["id"] in {s.id for s in BUILTIN_SPECS}:
            continue
        question = row["question"] or row["id"]
        if question in seen_q:
            continue
        answer = (row["answer_md"] or "").strip()
        if answer:
            sections.append({"name": question, "text": answer})
    return sections
