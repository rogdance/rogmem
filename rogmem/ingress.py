"""Material envelope → Ingress. Shape gate only.

This is the intake seam for host history and files. It is not the crystal
thin envelope, not Distiller, and not CrystalStore. Accepting a payload
never distills and never writes a crystal.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable

SCHEMA = "rogmem.material.v1"
KINDS = frozenset({"turn", "file", "summary"})
ROLES = frozenset({"user", "assistant"})
CRYSTAL_HINTS = frozenset({"body_md", "facts", "aliases", "owner_relation", "type", "name"})


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def envelope(
    *,
    kind: str,
    body: str,
    event_at: str = "",
    role: str = "",
    session_id: str = "",
    turn_id: str = "",
    seq: int = 0,
    program: str = "",
    device_name: str = "",
    filename: str = "",
    project: str = "",
    event_basis: str = "",
) -> dict[str, Any]:
    """Build a material envelope. Does not persist or distill."""
    text = str(body or "")
    digest = _hash(text)
    session = str(session_id or program or "material")
    sequence = int(seq or 0)
    built = {
        "schema": SCHEMA,
        "kind": str(kind or ""),
        "body": text,
        "event_at": str(event_at or ""),
        "event_basis": str(event_basis or ""),
        "role": str(role or ""),
        "session_id": session,
        "turn_id": str(turn_id or session),
        "seq": sequence,
        "program": str(program or ""),
        "device_name": str(device_name or ""),
        "filename": str(filename or ""),
        "content_hash": digest,
        "idempotency_key": f"{session}:{sequence}:{digest}",
    }
    bound = str(project or "").strip()
    if bound:
        built["project"] = bound
    return built


def envelopes_from_turns(turns: list[dict]) -> list[dict[str, Any]]:
    """Wrap installer/history turns as material envelopes. No upload."""
    out: list[dict[str, Any]] = []
    for item in turns or []:
        if not isinstance(item, dict):
            continue
        out.append(
            envelope(
                kind="turn",
                body=str(item.get("text") or item.get("body") or ""),
                event_at=str(item.get("event_at") or ""),
                event_basis=str(item.get("event_basis") or ""),
                role=str(item.get("role") or ""),
                session_id=str(item.get("session_id") or ""),
                turn_id=str(item.get("turn_id") or ""),
                seq=int(item.get("seq") or 0),
                program=str(item.get("program") or ""),
                device_name=str(item.get("device_name") or ""),
                project=str(item.get("project") or ""),
            )
        )
    return out


def turn_from_envelope(item: dict[str, Any]) -> dict[str, Any]:
    """Hub-facing turn shape. Ingress still does not ingest or distill."""
    built = {
        "session_id": str(item.get("session_id") or ""),
        "turn_id": str(item.get("turn_id") or item.get("session_id") or ""),
        "seq": int(item.get("seq") or 0),
        "role": str(item.get("role") or ""),
        "text": str(item.get("body") or ""),
        "event_at": str(item.get("event_at") or ""),
        "event_basis": str(item.get("event_basis") or ""),
        "program": str(item.get("program") or ""),
        "device_name": str(item.get("device_name") or ""),
    }
    project = str(item.get("project") or "").strip()
    if project:
        built["project"] = project
    return built


def validate(payload: Any) -> dict[str, Any]:
    """Program shape gate. Open semantic judgment is not this layer's job."""
    if not isinstance(payload, dict):
        return {"ok": False, "errors": ["not-object"], "envelope": None}
    errors: list[str] = []
    if payload.get("schema") != SCHEMA:
        errors.append("schema")
        if CRYSTAL_HINTS.intersection(payload):
            errors.append("crystal-envelope-refused")
        return {"ok": False, "errors": errors, "envelope": None}
    kind = str(payload.get("kind") or "")
    if kind not in KINDS:
        errors.append("kind")
    body = str(payload.get("body") or "")
    if not body.strip():
        errors.append("body")
    if kind == "turn":
        if str(payload.get("role") or "") not in ROLES:
            errors.append("role")
        if not str(payload.get("event_at") or "").strip():
            errors.append("event_at")
    if kind == "file" and not str(payload.get("filename") or "").strip():
        errors.append("filename")
    built = envelope(
        kind=kind,
        body=body,
        event_at=str(payload.get("event_at") or ""),
        event_basis=str(payload.get("event_basis") or ""),
        role=str(payload.get("role") or ""),
        session_id=str(payload.get("session_id") or ""),
        turn_id=str(payload.get("turn_id") or ""),
        seq=int(payload.get("seq") or 0),
        program=str(payload.get("program") or ""),
        device_name=str(payload.get("device_name") or ""),
        filename=str(payload.get("filename") or ""),
        project=str(payload.get("project") or ""),
    )
    if errors:
        return {"ok": False, "errors": errors, "envelope": None}
    return {"ok": True, "errors": [], "envelope": built}


def accept(
    payload: Any,
    *,
    dry_run: bool = True,
    post: Callable[[list[dict[str, Any]]], Any] | None = None,
) -> dict[str, Any]:
    """Ingress access point.

    Default dry-run only validates. A post callback may forward turns to the
    hub later. This function never distills and never writes a crystal.
    """
    items = payload if isinstance(payload, list) else [payload]
    accepted: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        checked = validate(item)
        if checked["ok"]:
            accepted.append(checked["envelope"])
        else:
            errors.append({"index": index, "errors": checked["errors"]})
    posted = 0
    result = None
    refused = ""
    if not items:
        refused = "empty"
    elif accepted and not dry_run:
        if post is None:
            refused = "no-target"
        else:
            turns = [turn_from_envelope(item) for item in accepted if item.get("kind") == "turn"]
            result = post(turns)
            if isinstance(result, dict) and result.get("ok") is False:
                refused = str(result.get("refused") or "post-failed")
                posted = 0
            else:
                posted = len(turns)
    return {
        "ok": not errors and not refused,
        "dry_run": bool(dry_run),
        "accepted": accepted,
        "errors": errors,
        "posted": posted,
        "result": result,
        "refused": refused,
        "distilled": False,
        "crystal_written": False,
    }
