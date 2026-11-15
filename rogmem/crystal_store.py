"""CrystalStore: thin envelope writes, revision/lease, lifecycle (v3 §3.2 / §7 / §11.1).

New writes go through the thin envelope. Distiller chapter and field writers
call ``apply_ops`` / ``put``; there is no thick JSON fallback
(``summary_json=?`` / ``chapters_json=?``).
``DISTILLER_STORE_WRITE_ENABLED`` is on. Kill the flag to fail-closed:
put / apply_ops / transition raise. Unresolved conflicts persist on the
existing column either way.

On-ship wipe of leftover thick blobs is allowed (not a thick→thin migration).
Read paths are thin-only. ``project_thick`` is a deprecated fail-closed
tombstone — do not claim the store is zero-thick; leftover blobs still
need ``scripts/wipe_thick_blobs.sql``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

CRYSTAL_STATUSES = frozenset({"draft", "deferred", "active", "merged", "rejected"})
LEGACY_STATUS_MAP = {"已合并": "merged"}
EVENT_KINDS = frozenset(
    {
        "draft_created",
        "match",
        "integrate",
        "gate_drop",
        "apply_op",
        "defer",
        "promote_new",
        "merge",
        "reconcile",
        "reject",
        "lease_conflict",
    }
)
PLACEHOLDER = frozenset({"", "暂无来源", "尚无来源", "暂无信息"})
LEASE_TTL = timedelta(minutes=5)
# Production on. Kill to False to fail-closed put / apply_ops / transition.
# _write_chapter / _write_field call apply_ops; no thick JSON bypass.
# On-ship wipe leftover thick blobs: UPDATE crystallizations SET summary_json='{}', chapters_json='{}';
# That is a wipe, not a thick→thin migration. Read paths no longer project those blobs.
DISTILLER_STORE_WRITE_ENABLED = True
# v3 §7.3: maybe → deferred automatic digestion is bounded. Hitting the
# cap stays deferred and observable; it does not accept or mint another page.
DEFERRED_RETRY_LIMIT = 3
# Typed envelope slots. keys.* and any other simple identifier sink into ThinCrystal.keys.
# This is apply dispatch, not a closed field-name allowlist.
ENVELOPE_FIELDS = frozenset({"summary", "aliases", "owner_relation"})

StorageKind = Literal["thin", "thick"]
CrystalStatus = Literal["draft", "deferred", "active", "merged", "rejected"]


class RevisionConflict(Exception):
    """apply_ops / transition saw a stale revision."""


class LeaseConflict(Exception):
    """Another holder already leases this crystal."""


class InvalidTransition(Exception):
    """Status change is not in the v3 machine."""


class IntegrateWriteNotLive(RuntimeError):
    """Distiller production writes are not routed through CrystalStore yet."""


class ThickReadDeprecated(RuntimeError):
    """Knife B: thick-read projection is retired. Live reads are thin-only."""


def parse_valid_until(value) -> str | None:
    """Accept YYYY-MM-DD only. Refuse invented / unparseable dates."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none"}:
        return None
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return text


@dataclass
class Fact:
    claim: str
    evidence: str = ""
    anchor: str = ""
    epistemics: str = "asserted"
    valid_until: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "claim": self.claim,
            "evidence": self.evidence,
            "anchor": self.anchor,
            "epistemics": self.epistemics or "asserted",
        }
        if self.valid_until:
            out["valid_until"] = self.valid_until
        return out


@dataclass
class Inference:
    claim: str
    basis: str = ""
    anchor: str = ""
    epistemics: str = "inferred"

    def to_dict(self) -> dict[str, str]:
        return {
            "claim": self.claim,
            "basis": self.basis,
            "anchor": self.anchor,
            "epistemics": self.epistemics or "inferred",
        }


@dataclass
class ThinCrystal:
    id: str
    type: str
    name: str
    status: str
    revision: int
    summary: str = ""
    aliases: list[str] = field(default_factory=list)
    owner_relation: str = "暂无信息"
    body_md: str = ""
    sketch_md: str = ""
    facts: list[Fact] = field(default_factory=list)
    inferences: list[Inference] = field(default_factory=list)
    keys: dict[str, Any] = field(default_factory=dict)
    storage: StorageKind = "thin"
    merged_into: str | None = None
    maybe_with: str | None = None
    possible_duplicate_of: str | None = None
    unresolved_conflicts: list[dict] = field(default_factory=list)
    deferred_retries: int = 0

    def to_envelope(self) -> dict[str, Any]:
        return {
            "aliases": list(self.aliases),
            "summary": self.summary,
            "owner_relation": self.owner_relation,
            "facts": [item.to_dict() for item in self.facts],
            "inferences": [item.to_dict() for item in self.inferences],
            "keys": dict(self.keys),
        }


class CrystalStorePort(Protocol):
    def get(self, crystal_id: str) -> ThinCrystal | None: ...
    def put(self, crystal: ThinCrystal) -> ThinCrystal: ...
    def apply_ops(
        self,
        crystal_id: str,
        expected_revision: int,
        ops: list[dict],
        *,
        holder: str,
        commit: bool = True,
    ) -> ThinCrystal: ...
    def persist_unresolved(
        self,
        crystal_id: str,
        unresolved: list[dict],
        *,
        holder: str | None = None,
        commit: bool = True,
    ) -> ThinCrystal: ...
    def transition(
        self,
        crystal_id: str,
        status: str,
        *,
        expected_revision: int | None = None,
        extra: dict | None = None,
    ) -> ThinCrystal: ...


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


def parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw)


def _part_clocks(
    previous_body: str | None,
    previous_sketch: str | None,
    body_at: str | None,
    sketch_at: str | None,
    crystal: ThinCrystal,
) -> tuple[str | None, str | None]:
    """Move a part's clock only when that part's stored text changes.

    One ``utcnow_iso`` covers every part this write actually changes.
    A name, status, or envelope write leaves both clocks where they were.
    """
    body_changed = (previous_body or "") != (crystal.body_md or "")
    sketch_changed = (previous_sketch or "") != (crystal.sketch_md or "")
    if not body_changed and not sketch_changed:
        return body_at, sketch_at
    stamp = utcnow_iso()
    if body_changed:
        body_at = stamp
    if sketch_changed:
        sketch_at = stamp
    return body_at, sketch_at


def canonicalize_status(raw: str | None) -> str:
    value = (raw or "").strip()
    return LEGACY_STATUS_MAP.get(value, value)


def _row_get(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


def _parse_json(raw: Any, fallback: Any) -> Any:
    if raw in (None, ""):
        return fallback
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def _split_aliases(raw: str) -> list[str]:
    parts = []
    for piece in (raw or "").replace("，", ",").replace("、", ",").split(","):
        name = piece.strip()
        if name and name not in PLACEHOLDER:
            parts.append(name)
    return parts


def _fact_from_dict(item: dict) -> Fact:
    return Fact(
        claim=str(item.get("claim") or "").strip(),
        evidence=str(item.get("evidence") or "").strip(),
        anchor=str(item.get("anchor") or "").strip(),
        epistemics=str(item.get("epistemics") or "asserted").strip() or "asserted",
        valid_until=parse_valid_until(item.get("valid_until")),
    )


def _inference_from_dict(item: dict) -> Inference:
    return Inference(
        claim=str(item.get("claim") or "").strip(),
        basis=str(item.get("basis") or "").strip(),
        anchor=str(item.get("anchor") or "").strip(),
        epistemics=str(item.get("epistemics") or "inferred").strip() or "inferred",
    )


def project_thick(row: sqlite3.Row | dict) -> ThinCrystal:
    """Deprecated. Not a live read path.

    Historical ``summary_json`` / ``chapters_json`` blobs stay in the DB until
    ``scripts/wipe_thick_blobs.sql``. Callers must use ``hydrate`` / thin
    columns. This tombstone raises so leftover calls fail closed.
    """
    raise ThickReadDeprecated(
        "project_thick is retired; read paths are thin-only. "
        "On-ship leftover thick blobs still need wipe_thick_blobs.sql."
    )


def hydrate(row: sqlite3.Row | dict) -> ThinCrystal:
    """Thin-only projection. Never reads chapters_json / summary_json."""
    envelope = _parse_json(_row_get(row, "envelope_json"), {})
    if not isinstance(envelope, dict):
        envelope = {}
    facts_raw = _parse_json(_row_get(row, "facts_json"), envelope.get("facts") or [])
    inferences_raw = _parse_json(_row_get(row, "inferences_json"), envelope.get("inferences") or [])
    return ThinCrystal(
        id=str(_row_get(row, "id") or ""),
        type=str(_row_get(row, "type") or envelope.get("type") or ""),
        name=str(_row_get(row, "name") or envelope.get("name") or ""),
        status=canonicalize_status(_row_get(row, "status")),
        revision=int(_row_get(row, "revision") or 0),
        summary=str(envelope.get("summary") or ""),
        aliases=list(envelope.get("aliases") or []),
        owner_relation=str(envelope.get("owner_relation") or "暂无信息"),
        body_md=str(_row_get(row, "body_md") or envelope.get("body_md") or ""),
        sketch_md=str(_row_get(row, "sketch_md") or ""),
        facts=[_fact_from_dict(item) for item in facts_raw if isinstance(item, dict)],
        inferences=[_inference_from_dict(item) for item in inferences_raw if isinstance(item, dict)],
        keys=dict(envelope.get("keys") or {}),
        storage="thin",
        merged_into=_row_get(row, "merged_into") or envelope.get("merged_into"),
        maybe_with=_row_get(row, "maybe_with") or envelope.get("maybe_with"),
        possible_duplicate_of=_row_get(row, "possible_duplicate_of") or envelope.get("possible_duplicate_of"),
        unresolved_conflicts=_parse_json(_row_get(row, "unresolved_conflicts"), []) or [],
        deferred_retries=int(_row_get(row, "deferred_retries") or 0),
    )


def migrate_crystal_store(con: sqlite3.Connection) -> None:
    """Additive columns + audit / lease tables. Safe on existing thick rows."""
    columns = {row[1] for row in con.execute("PRAGMA table_info(crystallizations)")}
    for name, spec in (
        ("envelope_json", "TEXT"),
        ("body_md", "TEXT"),
        ("facts_json", "TEXT"),
        ("inferences_json", "TEXT"),
        ("merged_into", "TEXT"),
        ("maybe_with", "TEXT"),
        ("possible_duplicate_of", "TEXT"),
        ("unresolved_conflicts", "TEXT"),
        ("deferred_retries", "INTEGER NOT NULL DEFAULT 0"),
        ("lease_holder", "TEXT"),
        ("lease_until", "TEXT"),
        ("sketch_md", "TEXT"),
        ("body_updated_at", "TEXT"),
        ("sketch_updated_at", "TEXT"),
    ):
        if name not in columns:
            con.execute(f"ALTER TABLE crystallizations ADD COLUMN {name} {spec}")
    con.execute(
        """CREATE TABLE IF NOT EXISTS crystal_events(
             id INTEGER PRIMARY KEY,
             crystal_id TEXT NOT NULL,
             kind TEXT NOT NULL,
             payload_json TEXT NOT NULL,
             at TEXT NOT NULL
           )"""
    )
    indexes = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    if "crystal_events_crystal_at" not in indexes:
        con.execute("CREATE INDEX crystal_events_crystal_at ON crystal_events(crystal_id, at)")
    con.execute("UPDATE crystallizations SET type='工程' WHERE type='项目'")
    con.execute(
        """CREATE TABLE IF NOT EXISTS crystal_leases(
             crystal_id TEXT PRIMARY KEY,
             holder TEXT NOT NULL,
             until TEXT NOT NULL,
             purpose TEXT NOT NULL DEFAULT 'integrate'
           )"""
    )


def assert_distiller_write_not_flipped() -> None:
    if DISTILLER_STORE_WRITE_ENABLED:
        return
    raise IntegrateWriteNotLive(
        "CrystalStore thin write is off; Distiller production writes fail closed"
    )


def require_thin_write() -> None:
    """Kill switch: thin envelope mutation is impossible while the flag is False."""
    assert_distiller_write_not_flipped()


def _is_structural_path(path: str) -> bool:
    raw = (path or "").strip()
    if raw in {"facts", "facts[]", "inferences", "inferences[]", "body_md", ""}:
        return True
    if raw.startswith("facts") or raw.startswith("inferences"):
        return True
    return raw.startswith("body_md##")


def _envelope_key(path: str) -> str | None:
    """Open path: keys.* / envelope stable keys / simple key names. No field allowlist."""
    raw = (path or "").strip()
    if not raw:
        return None
    if raw.startswith("keys."):
        rest = raw[5:].strip()
        return rest or None
    if _is_structural_path(raw):
        return None
    if raw in ENVELOPE_FIELDS:
        return raw
    if "." in raw or "[" in raw:
        return None
    return raw


def chapter_update_ops(chapter: str, value: str) -> list[dict]:
    """Single-section body write. ``_write_chapter`` must go through apply_ops with this shape."""
    heading = (chapter or "").strip()
    return [{"op": "update", "path": f"body_md##{heading}", "value": value}]


# Thick summary slots that hydrate into typed envelope fields, not keys.*.
_FIELD_ENVELOPE_PATH = {
    "概述": "summary",
    "别名": "aliases",
    "与记忆主人的关系": "owner_relation",
}


def field_update_ops(field: str, value: str) -> list[dict]:
    """Single-field envelope write. ``_write_field`` must go through apply_ops with this shape."""
    key = (field or "").strip()
    path = _FIELD_ENVELOPE_PATH.get(key, f"keys.{key}" if key else "keys")
    return [{"op": "update", "path": path, "value": value}]


def apply_op_supported(op: dict) -> bool:
    """True when `_apply_one` can execute this op. Gate and apply share this surface."""
    kind = str(op.get("op") or "").strip()
    path = str(op.get("path") or "").strip()
    if kind == "keep":
        return True
    if kind == "add" and path in {"facts", "facts[]", "inferences", "inferences[]", ""}:
        return True
    if kind in {"add", "update", "delete"} and (
        path.startswith("facts") or path.startswith("inferences")
    ):
        return True
    if kind in {"add", "update"} and (path == "body_md" or path.startswith("body_md##")):
        return True
    if kind in {"add", "update", "delete"} and _envelope_key(path):
        return True
    return False


_LEGAL_TRANSITIONS = {
    "draft": frozenset({"draft", "deferred", "active", "merged", "rejected"}),
    "deferred": frozenset({"deferred", "active", "merged", "rejected"}),
    "active": frozenset({"active", "merged", "rejected"}),
    "merged": frozenset({"merged"}),
    "rejected": frozenset({"rejected"}),
}


class CrystalStore:
    """SQLite CrystalStore. Thin writes never touch chapters_json / summary_json."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        migrate_crystal_store(con)

    def get(self, crystal_id: str) -> ThinCrystal | None:
        row = self.con.execute("SELECT * FROM crystallizations WHERE id=?", (crystal_id,)).fetchone()
        if row is None:
            return None
        return hydrate(row)

    def put(self, crystal: ThinCrystal, *, holder: str | None = None, commit: bool = True) -> ThinCrystal:
        """Insert or replace the thin envelope. Never writes thick chapter trees."""
        require_thin_write()
        self._require_lease_if_held(crystal.id, holder)
        if crystal.status not in CRYSTAL_STATUSES:
            raise InvalidTransition(f"illegal status {crystal.status!r}")
        row = self.con.execute("SELECT * FROM crystallizations WHERE id=?", (crystal.id,)).fetchone()
        envelope = json.dumps(crystal.to_envelope(), ensure_ascii=False)
        facts = json.dumps([item.to_dict() for item in crystal.facts], ensure_ascii=False)
        inferences = json.dumps([item.to_dict() for item in crystal.inferences], ensure_ascii=False)
        conflicts = json.dumps(crystal.unresolved_conflicts, ensure_ascii=False)
        if row is None:
            body_at, sketch_at = _part_clocks(None, None, None, None, crystal)
            self.con.execute(
                """INSERT INTO crystallizations(
                     id, type, name, status, summary_json, chapters_json, revision,
                     envelope_json, body_md, sketch_md, facts_json, inferences_json,
                     merged_into, maybe_with, possible_duplicate_of, unresolved_conflicts,
                     deferred_retries, body_updated_at, sketch_updated_at
                   ) VALUES(?,?,?,?, '{}', '{}', ?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    crystal.id,
                    crystal.type,
                    crystal.name,
                    crystal.status,
                    crystal.revision,
                    envelope,
                    crystal.body_md,
                    crystal.sketch_md,
                    facts,
                    inferences,
                    crystal.merged_into,
                    crystal.maybe_with,
                    crystal.possible_duplicate_of,
                    conflicts,
                    crystal.deferred_retries,
                    body_at,
                    sketch_at,
                ),
            )
            self.record_event(crystal.id, "draft_created" if crystal.status == "draft" else "promote_new", {
                "status": crystal.status,
                "storage": "thin",
            })
        else:
            if int(row["revision"]) != int(crystal.revision):
                raise RevisionConflict(
                    f"{crystal.id}: expected revision {row['revision']}, got {crystal.revision}"
                )
            next_rev = int(row["revision"]) + 1
            body_at, sketch_at = _part_clocks(
                _row_get(row, "body_md"),
                _row_get(row, "sketch_md"),
                _row_get(row, "body_updated_at"),
                _row_get(row, "sketch_updated_at"),
                crystal,
            )
            self.con.execute(
                """UPDATE crystallizations SET
                     type=?, name=?, status=?,
                     envelope_json=?, body_md=?, sketch_md=?, facts_json=?, inferences_json=?,
                     merged_into=?, maybe_with=?, possible_duplicate_of=?,
                     unresolved_conflicts=?, deferred_retries=?,
                     body_updated_at=?, sketch_updated_at=?,
                     revision=?
                   WHERE id=?""",
                (
                    crystal.type,
                    crystal.name,
                    crystal.status,
                    envelope,
                    crystal.body_md,
                    crystal.sketch_md,
                    facts,
                    inferences,
                    crystal.merged_into,
                    crystal.maybe_with,
                    crystal.possible_duplicate_of,
                    conflicts,
                    crystal.deferred_retries,
                    body_at,
                    sketch_at,
                    next_rev,
                    crystal.id,
                ),
            )
        from rogmem.crystal_index import sync_crystal
        from rogmem.wikilink import sync_wikilinks

        sync_wikilinks(self.con, crystal.id, crystal.body_md, active=crystal.status == "active")
        sync_crystal(self.con, crystal.id, crystal.name, crystal.body_md, crystal.sketch_md, crystal.status)
        if commit:
            self.con.commit()
        loaded = self.get(crystal.id)
        assert loaded is not None
        return loaded

    def persist_unresolved(
        self,
        crystal_id: str,
        unresolved: list[dict],
        *,
        holder: str | None = None,
        commit: bool = True,
    ) -> ThinCrystal:
        """Write gate unresolved_conflicts onto the crystal row.

        This is not a thin envelope mutation. Allowed even when the write
        switch is killed to False so uncertain points are not memory-only.
        Never touches summary_json / chapters_json / envelope_json / body_md.
        """
        self._require_lease_if_held(crystal_id, holder)
        crystal = self.get(crystal_id)
        if crystal is None:
            raise KeyError(crystal_id)
        payload = [dict(item) for item in unresolved if isinstance(item, dict)]
        self.con.execute(
            "UPDATE crystallizations SET unresolved_conflicts=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), crystal_id),
        )
        self.record_event(crystal_id, "integrate", {"unresolved": True, "n": len(payload)})
        if commit:
            self.con.commit()
        loaded = self.get(crystal_id)
        assert loaded is not None
        return loaded

    def apply_ops(
        self,
        crystal_id: str,
        expected_revision: int,
        ops: list[dict],
        *,
        holder: str,
        commit: bool = True,
    ) -> ThinCrystal:
        require_thin_write()
        crystal = self._locked(crystal_id, expected_revision)
        self.require_lease(crystal_id, holder)
        working = crystal
        for op in ops:
            working = _apply_one(working, op)
        working.revision = expected_revision
        try:
            result = self.put(working, holder=holder, commit=False)
            for op in ops:
                self.record_event(crystal_id, "apply_op", {"op": op.get("op"), "path": op.get("path")})
            if commit:
                self.con.commit()
        except Exception:
            self.con.rollback()
            raise
        return result

    def transition(
        self,
        crystal_id: str,
        status: str,
        *,
        expected_revision: int | None = None,
        extra: dict | None = None,
        holder: str | None = None,
        commit: bool = True,
    ) -> ThinCrystal:
        require_thin_write()
        self._require_lease_if_held(crystal_id, holder)
        crystal = self.get(crystal_id)
        if crystal is None:
            raise KeyError(crystal_id)
        if expected_revision is not None and crystal.revision != expected_revision:
            raise RevisionConflict(
                f"{crystal_id}: expected revision {crystal.revision}, got {expected_revision}"
            )
        dest = canonicalize_status(status)
        if dest not in CRYSTAL_STATUSES:
            raise InvalidTransition(f"illegal status {status!r}")
        allowed = _LEGAL_TRANSITIONS.get(crystal.status, frozenset())
        if dest not in allowed:
            raise InvalidTransition(f"{crystal.status} → {dest}")
        extra = extra or {}
        crystal.status = dest
        if dest == "merged":
            crystal.merged_into = extra.get("merged_into") or crystal.merged_into
        if dest == "deferred":
            crystal.maybe_with = extra.get("maybe_with") or crystal.maybe_with
            crystal.deferred_retries = int(extra.get("deferred_retries", crystal.deferred_retries))
        if extra.get("possible_duplicate_of"):
            crystal.possible_duplicate_of = extra["possible_duplicate_of"]
        if extra.get("unresolved_conflicts") is not None:
            crystal.unresolved_conflicts = list(extra["unresolved_conflicts"])
        kind = {
            "active": "promote_new",
            "merged": "merge",
            "deferred": "defer",
            "rejected": "reject",
        }.get(dest, "integrate")
        self.record_event(crystal_id, kind, {"status": dest, **{k: extra[k] for k in extra if k != "unresolved_conflicts"}})
        return self.put(crystal, holder=holder, commit=commit)

    def acquire_lease(
        self,
        crystal_id: str,
        holder: str,
        *,
        ttl: timedelta = LEASE_TTL,
        purpose: str = "integrate",
        now: datetime | None = None,
        commit: bool = True,
    ) -> datetime:
        moment = now or utcnow()
        until = moment + ttl
        row = self.con.execute(
            "SELECT holder, until FROM crystal_leases WHERE crystal_id=?",
            (crystal_id,),
        ).fetchone()
        if row is not None:
            existing_until = parse_ts(row["until"])
            if existing_until and existing_until > moment and row["holder"] != holder:
                self.record_event(
                    crystal_id,
                    "lease_conflict",
                    {"holder": holder, "blocked_by": row["holder"], "until": row["until"]},
                )
                if commit:
                    self.con.commit()
                raise LeaseConflict(f"{crystal_id} leased by {row['holder']} until {row['until']}")
        self.con.execute(
            """INSERT INTO crystal_leases(crystal_id, holder, until, purpose)
               VALUES(?,?,?,?)
               ON CONFLICT(crystal_id) DO UPDATE SET holder=excluded.holder,
                 until=excluded.until, purpose=excluded.purpose""",
            (crystal_id, holder, until.isoformat(), purpose),
        )
        self.con.execute(
            "UPDATE crystallizations SET lease_holder=?, lease_until=? WHERE id=?",
            (holder, until.isoformat(), crystal_id),
        )
        if commit:
            self.con.commit()
        return until

    def release_lease(self, crystal_id: str, holder: str, *, commit: bool = True) -> None:
        row = self.con.execute(
            "SELECT holder FROM crystal_leases WHERE crystal_id=?",
            (crystal_id,),
        ).fetchone()
        if row is None:
            return
        if row["holder"] != holder:
            raise LeaseConflict(f"{crystal_id} leased by {row['holder']}")
        self.con.execute("DELETE FROM crystal_leases WHERE crystal_id=?", (crystal_id,))
        self.con.execute(
            "UPDATE crystallizations SET lease_holder=NULL, lease_until=NULL WHERE id=?",
            (crystal_id,),
        )
        if commit:
            self.con.commit()

    def release_orphaned(self) -> int:
        """This process is the only worker. Crystal leases from a previous process are stale."""
        cur = self.con.execute("DELETE FROM crystal_leases")
        self.con.execute(
            "UPDATE crystallizations SET lease_holder=NULL, lease_until=NULL "
            "WHERE lease_holder IS NOT NULL OR lease_until IS NOT NULL"
        )
        self.con.commit()
        return int(cur.rowcount)

    def _has_lease(self, crystal_id: str, *, now: datetime | None = None) -> bool:
        moment = now or utcnow()
        row = self.con.execute(
            "SELECT holder, until FROM crystal_leases WHERE crystal_id=?",
            (crystal_id,),
        ).fetchone()
        if row is None:
            return False
        until = parse_ts(row["until"])
        return until is not None and until > moment

    def _require_lease_if_held(self, crystal_id: str, holder: str | None, *, now: datetime | None = None) -> None:
        """If a live lease exists, put/transition must present the same holder."""
        if not self._has_lease(crystal_id, now=now):
            return
        if not holder:
            raise LeaseConflict(f"{crystal_id} is leased; holder is required")
        self.require_lease(crystal_id, holder, now=now)

    def require_lease(self, crystal_id: str, holder: str, *, now: datetime | None = None) -> None:
        moment = now or utcnow()
        row = self.con.execute(
            "SELECT holder, until FROM crystal_leases WHERE crystal_id=?",
            (crystal_id,),
        ).fetchone()
        if row is None:
            raise LeaseConflict(f"{crystal_id} has no integrate lease")
        until = parse_ts(row["until"])
        if until is None or until <= moment or row["holder"] != holder:
            raise LeaseConflict(f"{crystal_id} lease is not held by {holder}")

    def begin_distiller_write(
        self,
        *,
        draft_id: str,
        matched_id: str | None,
        holder: str,
    ) -> list[str]:
        """Acquire leases that must sit in front of a Distiller write (v3 §7.4).

        Production Distiller is not wired here. Callers that mutate crystals
        through this store must hold these leases first.
        """
        claimed = [draft_id]
        self.acquire_lease(draft_id, holder, purpose="integrate")
        if matched_id and matched_id != draft_id:
            self.acquire_lease(matched_id, holder, purpose="integrate")
            claimed.append(matched_id)
        return claimed

    def record_event(self, crystal_id: str, kind: str, payload: dict | None = None) -> None:
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown crystal_events kind {kind!r}")
        self.con.execute(
            "INSERT INTO crystal_events(crystal_id, kind, payload_json, at) VALUES(?,?,?,?)",
            (crystal_id, kind, json.dumps(payload or {}, ensure_ascii=False), utcnow_iso()),
        )

    def events(self, crystal_id: str) -> list[dict]:
        rows = self.con.execute(
            "SELECT kind, payload_json, at FROM crystal_events WHERE crystal_id=? ORDER BY id",
            (crystal_id,),
        ).fetchall()
        return [
            {"kind": row["kind"], "payload": _parse_json(row["payload_json"], {}), "at": row["at"]}
            for row in rows
        ]

    def _locked(self, crystal_id: str, expected_revision: int) -> ThinCrystal:
        crystal = self.get(crystal_id)
        if crystal is None:
            raise KeyError(crystal_id)
        if crystal.revision != expected_revision:
            raise RevisionConflict(
                f"{crystal_id}: expected revision {crystal.revision}, got {expected_revision}"
            )
        return crystal


def _apply_body_section(body: str, heading: str, value: str) -> str:
    marker = f"## {heading}"
    block = f"{marker}\n\n{value}".strip()
    if marker not in (body or ""):
        return ((body or "").rstrip() + ("\n\n" if body else "") + block).strip()
    start = body.find(marker)
    rest = body[start + len(marker) :]
    nxt = rest.find("\n## ")
    if nxt >= 0:
        return (body[:start] + block + rest[nxt:]).strip()
    return (body[:start] + block).strip()


def _apply_one(crystal: ThinCrystal, op: dict) -> ThinCrystal:
    kind = str(op.get("op") or "").strip()
    path = str(op.get("path") or "").strip()
    if not apply_op_supported(op):
        raise ValueError(f"illegal op {kind!r} path={path!r}")
    if kind == "keep":
        return crystal
    if kind == "add" and path in {"facts", "facts[]", ""}:
        payload = op.get("fact") or op.get("value") or {}
        if isinstance(payload, dict):
            fact = _fact_from_dict(payload)
            if fact.claim:
                crystal.facts.append(fact)
        return crystal
    if kind == "add" and path in {"inferences", "inferences[]"}:
        payload = op.get("inference") or op.get("value") or {}
        if isinstance(payload, dict):
            item = _inference_from_dict(payload)
            if item.claim:
                crystal.inferences.append(item)
        return crystal
    if kind in {"add", "update"} and (path == "body_md" or path.startswith("body_md##")):
        value = str(op.get("value") or "")
        if path == "body_md":
            crystal.body_md = value
        else:
            heading = path.split("##", 1)[1]
            crystal.body_md = _apply_body_section(crystal.body_md, heading, value)
        return crystal
    if kind in {"add", "update"} and path.startswith("facts"):
        payload = op.get("fact") if isinstance(op.get("fact"), dict) else None
        if payload is None and isinstance(op.get("value"), dict):
            payload = op["value"]
        if payload is None:
            payload = {
                "claim": str(op.get("value") or op.get("claim") or ""),
                "evidence": str(op.get("evidence") or ""),
            }
        replacement = _fact_from_dict(payload)
        if not replacement.claim:
            raise ValueError(f"illegal op {kind!r} path={path!r}: empty claim")
        claim = str((op.get("fact") or {}).get("claim") or op.get("claim") or "").strip()
        for index, fact in enumerate(crystal.facts):
            indexed = path.startswith("facts.") and path.split(".", 1)[1].split(".", 1)[0] == str(index)
            if fact.claim == claim or indexed or path.startswith(f"facts[{index}]"):
                crystal.facts[index] = replacement
                return crystal
        crystal.facts.append(replacement)
        return crystal
    if kind == "delete" and path.startswith("facts"):
        claim = str((op.get("fact") or {}).get("claim") or op.get("claim") or "").strip()
        crystal.facts = [fact for fact in crystal.facts if fact.claim != claim]
        return crystal
    if kind == "delete" and path.startswith("inferences"):
        claim = str((op.get("inference") or {}).get("claim") or op.get("claim") or "").strip()
        crystal.inferences = [item for item in crystal.inferences if item.claim != claim]
        return crystal
    key = _envelope_key(path)
    if key and kind in {"add", "update"}:
        value = op.get("value")
        if key == "summary":
            crystal.summary = str(value or "")
        elif key == "aliases":
            if isinstance(value, list):
                crystal.aliases = [str(item) for item in value]
            else:
                crystal.aliases = _split_aliases(str(value or ""))
        elif key == "owner_relation":
            crystal.owner_relation = str(value or "暂无信息")
        else:
            crystal.keys[key] = value
        return crystal
    if key and kind == "delete":
        if key == "summary":
            crystal.summary = ""
        elif key == "aliases":
            crystal.aliases = []
        elif key == "owner_relation":
            crystal.owner_relation = "暂无信息"
        else:
            crystal.keys.pop(key, None)
        return crystal
    raise ValueError(f"illegal op {kind!r} path={path!r}")
