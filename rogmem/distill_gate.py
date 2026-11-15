"""Distiller merge-gate harness (v3 §6.5 / §6.7 / §6.8 / §8).

Program-only shape and evidence checks. Does not call a model.
``evaluate_integrate`` / ``run_merge_gate`` stay in memory.
Engine ``apply_integrate`` is the unified production path: evaluate,
then accept persist + apply + sidecar share ``_apply_then_sidecar``.
Distiller / ``distill_resim`` / the worker call that entry; they do
not stop at the in-memory gate.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal

from rogmem.crystal_store import PLACEHOLDER, apply_op_supported

# Ordered slice headings are the single source; alias frozensets derive from them.
CORE_MEMORY_SLICES = (
    ("身份速写", ("身份速写",)),
    ("沟通与决策风格", ("沟通与决策风格", "能力与风格", "沟通风格", "决策风格")),
    ("约定与待办", ("约定与待办", "约定待办")),
    ("兴趣与偏好", ("兴趣与偏好", "兴趣爱好")),
    ("当前关注与计划", ("当前关注与计划", "当前关注")),
)
CORE_MODULE_ALIASES = {name: frozenset(aliases) for name, aliases in CORE_MEMORY_SLICES}
# 偏好·风格·关注·约定 — same 合并触及这些时必须有对应 body_md## ops
CORE_TOUCH_MODULES = frozenset(
    {"沟通与决策风格", "约定与待办", "兴趣与偏好", "当前关注与计划"}
)

PAYLOAD_KIND_EXTRACT = "extract"
PAYLOAD_KIND_INTEGRATE = "integrate"
IDENTITY_VALUES = frozenset({"same", "new", "maybe"})
VERDICT_VALUES = frozenset({"keep", "supersede", "both_with_scope", "uncertain"})
OP_VALUES = frozenset({"add", "update", "delete", "keep"})
_WS = re.compile(r"\s+", re.UNICODE)

Decision = Literal["accept", "rejected", "deferred", "retry"]


@dataclass
class DroppedFact:
    claim: str
    reason: str
    evidence: str = ""


@dataclass
class GateIssue:
    code: str
    message: str


@dataclass
class GateResult:
    decision: Decision
    identity: str = ""
    matched_id: str | None = None
    payload: dict | None = None
    kept_facts: list[dict] = field(default_factory=list)
    dropped_facts: list[DroppedFact] = field(default_factory=list)
    inferences: list[dict] = field(default_factory=list)
    unresolved_conflicts: list[dict] = field(default_factory=list)
    issues: list[GateIssue] = field(default_factory=list)
    retry_used: bool = False

    @property
    def ok(self) -> bool:
        return self.decision == "accept"


def normalize_corpus(text: str) -> str:
    """v3 §8.2 whitespace ADR: fold unicode space, unify newlines, strip; keep case/punct."""
    unified = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return _WS.sub(" ", unified).strip()


def is_continuous_evidence(evidence: str, *corpora: str) -> bool:
    needle = normalize_corpus(evidence)
    if not needle:
        return False
    return any(needle and needle in normalize_corpus(body or "") for body in corpora)


def _limited_json_repair(raw: str) -> dict | None:
    """v3 §6.8: try a bounded repair (fence strip / first object) before defer."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None


def parse_integrate_payload(raw: str | dict) -> tuple[dict | None, GateIssue | None]:
    if isinstance(raw, dict):
        return _flatten_integrate(raw), None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        repaired = _limited_json_repair(raw)
        if repaired is None:
            return None, GateIssue("unparseable_json", f"整合 JSON 不可解析: {exc}")
        return _flatten_integrate(repaired), None
    if not isinstance(data, dict):
        repaired = _limited_json_repair(raw if isinstance(raw, str) else "")
        if repaired is None:
            return None, GateIssue("unparseable_json", "整合 JSON 必须是对象")
        return _flatten_integrate(repaired), None
    return _flatten_integrate(data), None


def stamp_integrate_payload(data: dict) -> dict:
    """Code stamp at the integrate-payload produce boundary. Not a field sniff."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    out["payload_kind"] = PAYLOAD_KIND_INTEGRATE
    return out


def _flatten_integrate(data: dict) -> dict:
    """Accept sim envelopes: result.identity.decision → identity, plus nested matched_id.

    This is the produce boundary for integrate payloads; it stamps
    ``payload_kind=integrate`` so Distiller routing does not sniff.
    """
    inner = data.get("result") if isinstance(data.get("result"), dict) else data
    if not isinstance(inner, dict):
        return stamp_integrate_payload(data)
    out = dict(inner)
    ident = out.get("identity")
    if isinstance(ident, dict):
        out["identity"] = ident.get("decision") or ident.get("identity") or ""
        if ident.get("matched_id") and not out.get("matched_id"):
            out["matched_id"] = ident["matched_id"]
    return stamp_integrate_payload(out)


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, list) else []


def _has_fake_evidence(item: dict) -> bool:
    if "evidence" not in item:
        return False
    return str(item.get("evidence") or "").strip() not in {"", *PLACEHOLDER}


def is_covering_op(op: dict) -> bool:
    """Covering is model-declared. Program does not guess from path or wording.

    The integrate model marks an op that needs a conflict with ``covering=True``.
    The program only checks the ops↔conflicts contract. No path allowlists,
    no 至今/Present/当前 wordlists, no claim/reason substring matching.
    """
    return op.get("covering") is True


def _slot(raw: Any) -> str:
    text = str(raw or "").strip()
    return text[5:] if text.startswith("keys.") else text


def _conflict_covers(op: dict, conflicts: Iterable[dict], *, index: int) -> bool:
    """Structural link only: same slot, op_id, or op_index."""
    op_path = _slot(op.get("path") or op.get("key") or "")
    op_id = str(op.get("id") or op.get("op_id") or "").strip()
    for item in conflicts:
        if not isinstance(item, dict):
            continue
        target = _slot(item.get("path") or item.get("key") or item.get("target") or "")
        if op_path and target and op_path == target:
            return True
        ref_id = str(item.get("op_id") or item.get("covers_op") or "").strip()
        if op_id and ref_id and op_id == ref_id:
            return True
        if item.get("op_index") is None:
            continue
        try:
            if int(item["op_index"]) == index:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _conflict_op_refs(ops: list, conflicts: Iterable[dict]) -> list[GateIssue]:
    """If a conflict names an op, that op must exist. Structure only."""
    issues: list[GateIssue] = []
    known_ids = {str(op.get("id") or op.get("op_id") or "").strip() for op in ops}
    known_ids.discard("")
    for item in conflicts:
        if not isinstance(item, dict):
            continue
        if item.get("op_index") is not None:
            try:
                idx = int(item["op_index"])
            except (TypeError, ValueError):
                issues.append(GateIssue("conflict_op_ref_mismatch", "conflict.op_index 对不上 ops"))
                continue
            if idx < 0 or idx >= len(ops):
                issues.append(GateIssue("conflict_op_ref_mismatch", "conflict.op_index 对不上 ops"))
        ref_id = str(item.get("op_id") or item.get("covers_op") or "").strip()
        if ref_id and ref_id not in known_ids:
            issues.append(GateIssue("conflict_op_ref_mismatch", "conflict 引用了不存在的 op"))
    return issues


def check_shape(payload: dict, candidates: Iterable[str]) -> list[GateIssue]:
    issues: list[GateIssue] = []
    identity = str(payload.get("identity") or "")
    if identity not in IDENTITY_VALUES:
        issues.append(GateIssue("illegal_identity", f"identity 必须是 same/new/maybe，收到 {identity!r}"))
    matched_id = payload.get("matched_id")
    candidate_ids = {str(item) for item in candidates}
    ops = _as_list(payload.get("ops"))
    conflicts = _as_list(payload.get("conflicts"))
    if identity == "same":
        if not matched_id:
            issues.append(GateIssue("matched_id_required", "identity=same 必须带 matched_id"))
        elif str(matched_id) not in candidate_ids:
            issues.append(GateIssue("matched_id_not_in_candidates", "matched_id 必须落在候选里"))
    elif matched_id not in {None, ""} and str(matched_id) not in candidate_ids:
        issues.append(GateIssue("matched_id_not_in_candidates", "matched_id 必须落在候选里"))
    if identity in {"new", "maybe"} and (ops or conflicts):
        issues.append(GateIssue("ops_conflicts_must_be_empty", "new/maybe 时 ops 与 conflicts 必须为空"))
    for op in ops:
        kind = str(op.get("op") or "")
        if kind not in OP_VALUES:
            issues.append(GateIssue("illegal_op", f"op 必须是 add/update/delete/keep，收到 {kind!r}"))
        elif not apply_op_supported(op):
            issues.append(
                GateIssue(
                    "unsupported_apply_path",
                    f"apply 不支持 {kind}/{op.get('path')}",
                )
            )
    for item in conflicts:
        verdict = str(item.get("verdict") or "")
        if verdict not in VERDICT_VALUES:
            issues.append(GateIssue("illegal_verdict", f"verdict 必须是四种裁决词之一，收到 {verdict!r}"))
        old_ep = str(item.get("old_epistemics") or item.get("epistemics_old") or "asserted")
        new_ep = str(item.get("new_epistemics") or item.get("epistemics_new") or "asserted")
        if new_ep == "inferred" and old_ep == "asserted" and verdict == "supersede":
            issues.append(GateIssue("inference_overwrites_fact", "推不得盖实：不得 supersede 旧实"))
    return issues


def _core_canonical(name: str) -> str | None:
    text = str(name or "").strip().lstrip("#").strip()
    if text.startswith("keys."):
        text = text[5:].strip()
    for canon, aliases in CORE_MODULE_ALIASES.items():
        if text in aliases:
            return canon
    return None


def _parse_md_headings(body_md: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    heading = None
    buf: list[str] = []
    for line in (body_md or "").splitlines():
        if line.startswith("## "):
            if heading is not None:
                sections[heading] = "\n".join(buf).strip()
            heading = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    if heading is not None:
        sections[heading] = "\n".join(buf).strip()
    return sections


def _cores_in_body(body_md: str) -> set[str]:
    found: set[str] = set()
    for heading, text in _parse_md_headings(body_md).items():
        canon = _core_canonical(heading)
        if canon and str(text or "").strip():
            found.add(canon)
    return found


def _cores_from_ops(ops: Iterable[dict]) -> set[str]:
    """Touched cores: keys / anchors / section paths. Not a 'section delivered' set."""
    found: set[str] = set()
    for op in ops:
        if not isinstance(op, dict):
            continue
        path = str(op.get("path") or "").strip()
        if path.startswith("body_md##"):
            canon = _core_canonical(path.split("##", 1)[1])
            if canon:
                found.add(canon)
        elif path == "body_md" and str(op.get("op") or "") in {"add", "update"}:
            found.update(_cores_in_body(str(op.get("value") or "")))
        else:
            canon = _core_canonical(path)
            if canon:
                found.add(canon)
        fact = op.get("fact") if isinstance(op.get("fact"), dict) else None
        if fact:
            canon = _core_canonical(fact.get("anchor") or "")
            if canon:
                found.add(canon)
    return found


def _cores_delivered_by_body_ops(ops: Iterable[dict]) -> set[str]:
    """Only body_md## / full body_md count as landing a core section."""
    found: set[str] = set()
    for op in ops:
        if not isinstance(op, dict):
            continue
        path = str(op.get("path") or "").strip()
        if path.startswith("body_md##"):
            canon = _core_canonical(path.split("##", 1)[1])
            if canon:
                found.add(canon)
        elif path == "body_md" and str(op.get("op") or "") in {"add", "update"}:
            found.update(_cores_in_body(str(op.get("value") or "")))
    return found


def check_core_module_complete(
    payload: dict,
    *,
    draft=None,
    existing_body_md: str = "",
) -> list[GateIssue]:
    """same 合并触及主人核心模块时，必须有对应 body_md## ops，不得只改 summary 挤没节。

    触及只看 payload/draft 的 fact.anchor 与 ops 路径，不把 draft 全文核心节当触及。
    """
    if not isinstance(payload, dict) or str(payload.get("identity") or "") != "same":
        return []
    ops = [item for item in _as_list(payload.get("ops")) if isinstance(item, dict)]
    if not ops:
        return []
    touched = set()
    for fact in _op_facts(payload):
        canon = _core_canonical(fact.get("anchor") or "")
        if canon:
            touched.add(canon)
    touched.update(_cores_from_ops(ops))
    if isinstance(draft, dict):
        for fact in _as_list(draft.get("facts")):
            if isinstance(fact, dict):
                canon = _core_canonical(fact.get("anchor") or "")
                if canon:
                    touched.add(canon)
    present_body = _cores_delivered_by_body_ops(ops)
    needed = touched & CORE_TOUCH_MODULES
    missing = sorted(needed - present_body)
    if missing:
        return [
            GateIssue(
                "core_module_ops_missing",
                "same 合并触及核心模块但缺少对应 body_md## ops：" + "、".join(missing),
            )
        ]
    existing_cores = _cores_in_body(existing_body_md)
    if existing_cores:
        for op in ops:
            if str(op.get("path") or "") != "body_md" or str(op.get("op") or "") not in {"add", "update"}:
                continue
            kept = _cores_in_body(str(op.get("value") or ""))
            dropped = sorted(existing_cores - kept)
            if dropped:
                return [
                    GateIssue(
                        "core_module_dropped",
                        "same 合并不得用整篇 body_md 挤掉核心节：" + "、".join(dropped),
                    )
                ]
    return []


def check_ops_conflicts(payload: dict) -> list[GateIssue]:
    identity = str(payload.get("identity") or "")
    if identity != "same":
        return []
    issues: list[GateIssue] = []
    ops = _as_list(payload.get("ops"))
    conflicts = _as_list(payload.get("conflicts"))
    for index, op in enumerate(ops):
        if not is_covering_op(op):
            continue
        if not _conflict_covers(op, conflicts, index=index):
            issues.append(
                GateIssue(
                    "covering_op_without_conflict",
                    f"覆盖类 op {op.get('op')}/{op.get('path')} 没有对应 conflict",
                )
            )
    issues.extend(_conflict_op_refs(ops, conflicts))
    return issues


def gate_facts(facts: Iterable[dict], *corpora: str) -> tuple[list[dict], list[DroppedFact]]:
    kept: list[dict] = []
    dropped: list[DroppedFact] = []
    for item in facts:
        if not isinstance(item, dict):
            dropped.append(DroppedFact(claim="", reason="malformed_fact"))
            continue
        if str(item.get("epistemics") or "asserted") == "inferred":
            dropped.append(
                DroppedFact(
                    claim=str(item.get("claim") or ""),
                    reason="inference_in_facts",
                    evidence=str(item.get("evidence") or ""),
                )
            )
            continue
        evidence = str(item.get("evidence") or "")
        if not is_continuous_evidence(evidence, *corpora):
            dropped.append(
                DroppedFact(
                    claim=str(item.get("claim") or ""),
                    reason="evidence_not_substring",
                    evidence=evidence,
                )
            )
            continue
        kept.append(item)
    return kept, dropped


def gate_inferences(items: Iterable[dict], facts: Iterable[dict]) -> tuple[list[dict], list[GateIssue]]:
    fact_claims = {str(item.get("claim") or "").strip() for item in facts if isinstance(item, dict)}
    kept: list[dict] = []
    issues: list[GateIssue] = []
    for item in items:
        if not isinstance(item, dict):
            issues.append(GateIssue("malformed_inference", "inference 必须是对象"))
            continue
        if _has_fake_evidence(item):
            issues.append(GateIssue("fake_inference_evidence", "推不得伪造 evidence 冒充过闸 facts"))
            continue
        claim = str(item.get("claim") or "").strip()
        if claim and claim in fact_claims:
            issues.append(GateIssue("inference_overwrites_fact", "推不得覆盖已有实"))
            continue
        cleaned = {
            "claim": claim,
            "basis": str(item.get("basis") or ""),
            "anchor": str(item.get("anchor") or ""),
            "epistemics": "inferred",
        }
        kept.append(cleaned)
    return kept, issues


def _conflict_field_text(item: dict, *keys: str) -> str:
    for key in keys:
        raw = item.get(key)
        if isinstance(raw, list):
            for part in raw:
                text = str(part).strip()
                if text and text not in PLACEHOLDER:
                    return str(part)
        elif isinstance(raw, str) and raw.strip() and raw.strip() not in PLACEHOLDER:
            return raw
    return ""


def _conflict_evidence_items(item: dict) -> list[str]:
    found = []
    for key in ("evidence", "evidence_new", "evidence_old", "old_evidence", "new_evidence"):
        raw = item.get(key)
        if isinstance(raw, str) and raw.strip() and raw.strip() not in PLACEHOLDER:
            found.append(raw)
        elif isinstance(raw, list):
            found.extend(str(part) for part in raw if str(part).strip())
    return found


_AUTHORIZING_VERDICTS = frozenset({"supersede", "both_with_scope"})


def _authorizing_evidence_ok(item: dict, *corpora: str) -> bool:
    """Covering-authorizing verdicts require continuous evidence_new."""
    text = _conflict_field_text(item, "evidence_new", "new_evidence")
    return is_continuous_evidence(text, *corpora)


def _clear_authorizing_new_evidence(item: dict) -> dict:
    """Downgrade copy: drop bad/empty new-side evidence so it is not a source."""
    cleaned = dict(item)
    cleaned["verdict"] = "uncertain"
    for key in ("evidence_new", "new_evidence"):
        cleaned.pop(key, None)
    return cleaned


def unresolved_from_conflicts(conflicts: Iterable[dict]) -> list[dict]:
    """Point-level uncertain stays on the crystal as unresolved_conflicts."""
    found = []
    for item in conflicts:
        if isinstance(item, dict) and str(item.get("verdict") or "") == "uncertain":
            found.append(dict(item))
    return found


def persist_unresolved(
    store,
    crystal_id: str,
    result: GateResult,
    *,
    holder: str | None = None,
    commit: bool = True,
):
    """Write accept-path unresolved_conflicts onto the crystal.

    Rejected / deferred / retry results do not write. Empty accept clears
    the column. This is not a thin envelope mutation and does not change
    ``DISTILLER_STORE_WRITE_ENABLED``. Engine ``apply_integrate`` is the
    unified distill path that calls this in the same try as accept apply
    (commit=False, then ``_apply_then_sidecar``); do not treat
    the function as the only seam.
    """
    if result.decision != "accept":
        return None
    target_id = crystal_id or result.matched_id
    if not target_id:
        return None
    return store.persist_unresolved(
        str(target_id),
        list(result.unresolved_conflicts or []),
        holder=holder,
        commit=commit,
    )


def _conflict_authorizes_cover(op: dict, conflicts: Iterable[dict], *, index: int) -> bool:
    """Covering writes apply only when a linked conflict still says supersede / both."""
    authorizing = [
        item
        for item in conflicts
        if isinstance(item, dict) and str(item.get("verdict") or "") in _AUTHORIZING_VERDICTS
    ]
    return _conflict_covers(op, authorizing, index=index)


def gate_conflicts(conflicts: Iterable[dict], *corpora: str) -> tuple[list[dict], list[DroppedFact]]:
    """v3 §6.7: conflict evidence uses the same continuous-substring gate as facts.

    Covering-authorizing verdicts (supersede / both_with_scope) must not pass
    without good evidence_new. Downgrade that point to uncertain so the
    ops↔conflicts contract still holds, but the write is no longer authorized.
    """
    kept: list[dict] = []
    dropped: list[DroppedFact] = []
    for item in conflicts:
        if not isinstance(item, dict):
            dropped.append(DroppedFact(claim="", reason="malformed_conflict"))
            continue
        verdict = str(item.get("verdict") or "")
        if verdict in _AUTHORIZING_VERDICTS and not _authorizing_evidence_ok(item, *corpora):
            kept.append(_clear_authorizing_new_evidence(item))
            continue
        evidences = _conflict_evidence_items(item)
        failed = [text for text in evidences if not is_continuous_evidence(text, *corpora)]
        if failed:
            dropped.append(
                DroppedFact(
                    claim=str(item.get("path") or item.get("claim") or ""),
                    reason="conflict_evidence_not_substring",
                    evidence=failed[0],
                )
            )
            continue
        kept.append(item)
    return kept, dropped


def _op_facts(payload: dict) -> list[dict]:
    facts = [item for item in _as_list(payload.get("facts")) if isinstance(item, dict)]
    for op in _as_list(payload.get("ops")):
        if str(op.get("op") or "") not in {"add", "update"}:
            continue
        fact = op.get("fact")
        if isinstance(fact, dict):
            facts.append(fact)
            continue
        value = op.get("value")
        if isinstance(value, dict) and (value.get("evidence") or value.get("claim")):
            facts.append(value)
            continue
        if op.get("evidence"):
            facts.append(
                {
                    "claim": str(op.get("claim") or (value if isinstance(value, str) else "") or ""),
                    "evidence": str(op.get("evidence") or ""),
                    "epistemics": str(op.get("epistemics") or "asserted"),
                }
            )
    return facts


def _sanitize_payload(
    payload: dict,
    kept_facts: list[dict],
    dropped: list[DroppedFact],
    inferences: list[dict],
    conflicts: list[dict],
) -> dict:
    """On accept, leave only rows that passed the evidence gate."""
    dropped_evidence = {item.evidence for item in dropped if item.evidence}
    kept_ids = {id(item) for item in kept_facts}
    cleaned_ops = []
    for index, op in enumerate(_as_list(payload.get("ops"))):
        fact = op.get("fact") if isinstance(op.get("fact"), dict) else None
        value = op.get("value") if isinstance(op.get("value"), dict) else None
        evidence = str(
            op.get("evidence")
            or (fact or {}).get("evidence")
            or (value or {}).get("evidence")
            or ""
        )
        if evidence and evidence in dropped_evidence:
            continue
        if fact is not None and id(fact) not in kept_ids and evidence:
            continue
        if is_covering_op(op) and not _conflict_authorizes_cover(op, conflicts, index=index):
            continue
        cleaned_ops.append(op)
    out = dict(payload)
    out["facts"] = list(kept_facts)
    out["inferences"] = list(inferences)
    out["conflicts"] = list(conflicts)
    out["ops"] = cleaned_ops
    out["unresolved_conflicts"] = unresolved_from_conflicts(conflicts)
    return out


def evaluate_integrate(
    raw: str | dict,
    *,
    candidates: Iterable[str],
    corpus: str = "",
    chunks: Iterable[str] | None = None,
    attempt: int = 1,
) -> GateResult:
    """One pass of the merge gate. attempt=1 may return retry for ops↔conflicts."""
    payload, parse_issue = parse_integrate_payload(raw)
    if parse_issue or payload is None:
        return GateResult(
            decision="deferred",
            issues=[parse_issue] if parse_issue else [GateIssue("unparseable_json", "空载荷")],
            retry_used=attempt > 1,
        )

    issues = check_shape(payload, candidates)
    identity = str(payload.get("identity") or "")
    matched_id = payload.get("matched_id") or None
    if matched_id is not None:
        matched_id = str(matched_id)

    corpora = [corpus, *list(chunks or [])]
    kept, dropped = gate_facts(_op_facts(payload), *corpora)
    kept_conflicts, dropped_conflicts = gate_conflicts(_as_list(payload.get("conflicts")), *corpora)
    dropped.extend(dropped_conflicts)
    if dropped_conflicts:
        issues.extend(
            GateIssue("conflict_evidence_not_substring", f"conflict evidence 不是连续子串: {item.claim}")
            for item in dropped_conflicts
        )
    inferences, inference_issues = gate_inferences(_as_list(payload.get("inferences")), kept)
    issues.extend(inference_issues)

    gated = dict(payload)
    gated["conflicts"] = kept_conflicts
    consistency = check_ops_conflicts(gated)
    if consistency:
        if attempt <= 1:
            return GateResult(
                decision="retry",
                identity=identity,
                matched_id=matched_id,
                payload=payload,
                kept_facts=kept,
                dropped_facts=dropped,
                inferences=inferences,
                issues=issues + consistency,
            )
        return GateResult(
            decision="deferred",
            identity=identity,
            matched_id=matched_id,
            payload=payload,
            kept_facts=kept,
            dropped_facts=dropped,
            inferences=inferences,
            issues=issues + consistency,
            retry_used=True,
        )

    blocking = [
        item
        for item in issues
        if item.code not in {"evidence_not_substring", "conflict_evidence_not_substring"}
    ]
    if blocking:
        fatal = {item.code for item in blocking}
        if fatal & {"unparseable_json"}:
            decision: Decision = "deferred"
        elif fatal & {"fake_inference_evidence", "inference_overwrites_fact"}:
            decision = "rejected"
        else:
            decision = "rejected"
        return GateResult(
            decision=decision,
            identity=identity,
            matched_id=matched_id,
            payload=payload,
            kept_facts=kept,
            dropped_facts=dropped,
            inferences=inferences,
            issues=issues,
            retry_used=attempt > 1,
        )

    fact_inputs = _op_facts(payload)
    if fact_inputs and not kept:
        return GateResult(
            decision="rejected",
            identity=identity,
            matched_id=matched_id,
            payload=payload,
            kept_facts=[],
            dropped_facts=dropped,
            inferences=inferences,
            issues=issues + [GateIssue("all_facts_dropped", "全 facts 灭闸，不升空壳 active")],
            retry_used=attempt > 1,
        )

    sanitized = _sanitize_payload(payload, kept, dropped, inferences, kept_conflicts)
    unresolved = list(sanitized.get("unresolved_conflicts") or [])
    return GateResult(
        decision="accept",
        identity=identity,
        matched_id=matched_id,
        payload=sanitized,
        kept_facts=kept,
        dropped_facts=dropped,
        inferences=inferences,
        unresolved_conflicts=unresolved,
        issues=issues,
        retry_used=attempt > 1,
    )


def run_merge_gate(
    raw: str | dict,
    *,
    candidates: Iterable[str],
    corpus: str = "",
    chunks: Iterable[str] | None = None,
    retry_payload: str | dict | Callable[[dict, GateResult], str | dict] | None = None,
) -> GateResult:
    """v3 §6.8: covering-op inconsistency auto-retries once, then deferred."""
    first = evaluate_integrate(raw, candidates=candidates, corpus=corpus, chunks=chunks, attempt=1)
    if first.decision != "retry":
        return first
    if retry_payload is None:
        first.decision = "deferred"
        return first
    if callable(retry_payload):
        second_raw = retry_payload(first.payload or {}, first)
    else:
        second_raw = retry_payload
    second = evaluate_integrate(
        second_raw,
        candidates=candidates,
        corpus=corpus,
        chunks=chunks,
        attempt=2,
    )
    second.retry_used = True
    return second
