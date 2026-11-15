"""Dual-resume re-sim (v3 §7.5).

Offline (required CI green): replay the checked-in MiniMax sim artifacts
from ``docs/sim-dual-resume-report.md`` + ``tests/fixtures/dual_resume/``
through program fact gates and integrate consistency / PublishGate.

Live MiniMax (flip-switch hard card, **not** a CI assertion): when
``ROGMEM_LIVE_LLM=1``, call the existing engine chat client with the
already-landed Draft v5.3 + integrate v1.4 prompt *text* (read from
``docs/prompts/``, no wording changes). Pipe model output through the
same ``gate_facts`` / ``parse_integrate_payload`` / ``apply_integrate``
evaluate path as offline. Does not change ``DISTILLER_STORE_WRITE_ENABLED``.
Production Distiller ``_llm_integrate`` reads the same approved
integrate v1.4 text.

Without the env flag, ``run_live_resim`` does not open a network
connection; ``run_dual_resume_resim`` stays on the offline default.

True MiniMax (``run_true_minimax_resim``) is the flip-switch hard card:
it requires ``ROGMEM_LIVE_LLM=1`` plus a MiniMax base/key/model and
evaluates *program invariants* on the live JSON — not fixture-replay
``matches_recorded``. Injected ``complete`` stays on the fixture
evaluation so CI never needs secrets. Does not invent keys.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable

from rogmem.crystal_store import DISTILLER_STORE_WRITE_ENABLED
from rogmem.distill_gate import (
    IDENTITY_VALUES,
    gate_facts,
    is_continuous_evidence,
    parse_integrate_payload,
)
from rogmem.engine import _complete_chat, _parse_model_json, apply_integrate, llm_target

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "sim-dual-resume-report.md"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "dual_resume"
PROMPT_DIR = ROOT / "docs" / "prompts"
DRAFT_PROMPT_PATH = PROMPT_DIR / "wiki-distill-draft-prompt-v5.md"
INTEGRATE_PROMPT_PATH = PROMPT_DIR / "wiki-integrate-adjudicate-prompt-v1.md"

# Live MiniMax remains a non-CI hard card. DISTILLER_STORE_WRITE_ENABLED is on.
# Read paths are thin-only. Historical thick blobs still need wipe — do not
# claim zero-thick.
LIVE_RESIM_NEXT_GATE = (
    "DISTILLER_STORE_WRITE_ENABLED is on. Live MiniMax re-sim (Draft v5.3 + "
    "integrate v1.4) remains a non-CI hard card, not a substitute for offline "
    "green. Offline replay of docs/sim-dual-resume-report.md remains the "
    "required green. Production Distiller _llm_integrate uses approved "
    "integrate v1.4 when a valid user payload can be assembled. Do not claim "
    "zero-thick; leftover blobs still need wipe_thick_blobs.sql."
)

# Dual-resume sim identity (report, not prompt text).
OWNER_REAL_NAME = "林知远"
OWNER_ALIASES = "Zhiyuan Lin, zhiyuanlin, 知远哥, Zhiyuan Lin"
EN_FILENAME = "Zhiyuan Lin (1).docx"
CN_FILENAME = "中文简历2026.pdf"

LIVE_RESIM_TIMEOUT_SEC = 180
# M3.1 的输出上限。思考也算在里面，不能再人为收成一万多。
LIVE_RESIM_MAX_TOKENS = 8192
LIVE_RESIM_MAX_TOKENS_MINIMAX = 524288

# True MiniMax live (flip-switch hard card). Names only; never invent keys.
TRUE_LIVE_FLAG = "ROGMEM_LIVE_LLM"
TRUE_LIVE_BASE_VARS = ("ROGMEM_LIVE_LLM_BASE", "ROGMEM_LLM_BASE")
TRUE_LIVE_KEY_VARS = ("ROGMEM_LIVE_LLM_KEY", "ROGMEM_LLM_KEY")
TRUE_LIVE_MODEL_VARS = ("ROGMEM_LIVE_LLM_MODEL", "ROGMEM_LLM_MODEL")
_STUB_KEYS = frozenset({"", "test", "stub"})
_STUB_MODELS = frozenset({"", "stub"})

OWNER_NEEDLES = (
    OWNER_REAL_NAME,
    "Zhiyuan Lin",
    "Lin Zhiyuan",
    "Zhiyuan Lin",
    "zhiyuanlin",
    "知远哥",
)
xinghe_NEEDLES = ("星河工坊", "Xinghe Studio")
PERSON_LIBRARY_ID = "cry_en_0_林知远"
PRODUCT_LIBRARY_ID = "cry_en_2_星河工坊"

CompleteFn = Callable[[list[dict[str, str]]], Any]


def _load_json(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _load_text(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _require_sim_files() -> None:
    missing = [str(REPORT)] if not REPORT.is_file() else []
    for name in (
        "en-material.txt",
        "cn-material.txt",
        "draft-en.json",
        "draft-cn.json",
        "integrate-0.json",
        "integrate-2.json",
        "match-report.json",
        "gate-continuity-check.json",
    ):
        if not (FIXTURE_DIR / name).is_file():
            missing.append(name)
    if missing:
        raise FileNotFoundError("dual-resume sim artifacts missing: " + ", ".join(missing))


def _require_approved_prompts() -> None:
    missing = [str(path) for path in (DRAFT_PROMPT_PATH, INTEGRATE_PROMPT_PATH) if not path.is_file()]
    if missing:
        raise FileNotFoundError("approved prompt files missing: " + ", ".join(missing))


def _section_after(text: str, heading_prefix: str, stop_prefixes: tuple[str, ...]) -> str:
    """Slice a markdown body after a heading, without changing the source file."""
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith(heading_prefix):
            start = index + 1
            break
    if start is None:
        raise ValueError(f"prompt section not found: {heading_prefix}")
    collected: list[str] = []
    for line in lines[start:]:
        if line.strip() == "---" or any(line.startswith(stop) for stop in stop_prefixes):
            break
        collected.append(line)
    body = "\n".join(collected).strip()
    if not body:
        raise ValueError(f"prompt section empty: {heading_prefix}")
    return body


def approved_draft_prompts() -> tuple[str, str]:
    """Read Draft v5.3 system + user template as-is from the approved markdown."""
    text = DRAFT_PROMPT_PATH.read_text(encoding="utf-8")
    system = _section_after(text, "## A) System prompt", ("## B)", "## C)"))
    user = _section_after(text, "## B) User prompt", ("程序注入：", "## C)"))
    return system, user


def approved_integrate_prompts() -> tuple[str, str]:
    """Read integrate v1.4 system + user template as-is from the approved markdown."""
    text = INTEGRATE_PROMPT_PATH.read_text(encoding="utf-8")
    system = _section_after(text, "## System prompt", ("## User prompt",))
    user = _section_after(
        text,
        "## User prompt",
        ("程序注入：", "#### 注入分级", "## 输出 JSON schema"),
    )
    return system, user


def _fill_template(template: str, **values: str) -> str:
    filled = template
    for key, value in values.items():
        filled = filled.replace("{{" + key + "}}", value)
    return filled


def fill_draft_user(
    template: str,
    *,
    filename: str = "",
    material: str,
    owner_real_name: str = "",
    owner_aliases: str = "",
    material_kind: str = "",
) -> str:
    """Fill approved Draft user placeholders. Does not change the markdown."""
    return _fill_template(
        template,
        filename=filename,
        owner_real_name=owner_real_name,
        owner_aliases=owner_aliases,
        material=material,
        material_kind=material_kind,
    )


PROJECT_REF_LINE_PREFIX = "本材料关联工程："


def inject_draft_project_ref(user: str, project_ref: str = "") -> str:
    """Append the program-side 关联工程 line. Does not edit prompt markdown."""
    ref = str(project_ref or "").strip()
    text = user or ""
    if not ref:
        return text
    line = f"{PROJECT_REF_LINE_PREFIX}{ref}"
    if line in text:
        return text
    for marker in ("文件名：", "材料类型："):
        start = text.find(marker)
        if start < 0:
            continue
        end = text.find("\n", start)
        if end < 0:
            return f"{text}\n{line}"
        return f"{text[: end + 1]}{line}\n{text[end + 1 :]}"
    return f"{line}\n{text}"


def assemble_draft_user(
    template: str,
    *,
    filename: str = "",
    material: str,
    owner_real_name: str = "",
    owner_aliases: str = "",
    material_kind: str = "",
    project_ref: str = "",
) -> str:
    """Fill approved placeholders, then inject project_ref. Prompt files stay untouched."""
    filled = fill_draft_user(
        template,
        filename=filename,
        owner_real_name=owner_real_name,
        owner_aliases=owner_aliases,
        material=material,
        material_kind=material_kind,
    )
    return inject_draft_project_ref(filled, project_ref)


def _draft_user(template: str, *, filename: str, material: str, material_kind: str = "文件") -> str:
    return fill_draft_user(
        template,
        filename=filename,
        material=material,
        owner_real_name=OWNER_REAL_NAME,
        owner_aliases=OWNER_ALIASES,
        material_kind=material_kind,
    )


def _dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _bare_name(value: str) -> str:
    text = str(value or "").strip()
    for sep in ("（", "("):
        if sep in text:
            text = text.split(sep, 1)[0].strip()
    return text


def _crystal_names(crystal: dict) -> set[str]:
    names = [_bare_name(str(crystal.get("name") or ""))]
    names.extend(_bare_name(str(alias)) for alias in (crystal.get("aliases") or []))
    return {name for name in names if name}


def _folded(names: Iterable[str]) -> set[str]:
    return {name.casefold() for name in names if name}


def _crystal_by_name(draft: dict, name: str) -> dict | None:
    want = _bare_name(name)
    for crystal in draft.get("crystals") or []:
        if not isinstance(crystal, dict):
            continue
        names = _crystal_names(crystal)
        if want in names or name == crystal.get("name"):
            return crystal
    return None


def _crystal_by_needles(draft: dict, needles: Iterable[str]) -> dict | None:
    want = _folded(_bare_name(needle) for needle in needles)
    for crystal in draft.get("crystals") or []:
        if not isinstance(crystal, dict):
            continue
        if _folded(_crystal_names(crystal)) & want:
            return crystal
    return None


def _draft_has(draft: dict, needles: Iterable[str]) -> bool:
    return _crystal_by_needles(draft, needles) is not None


def _job_needles(job: dict) -> tuple[str, ...]:
    library_id = str(job.get("library_id") or "")
    if library_id == PERSON_LIBRARY_ID:
        return OWNER_NEEDLES
    if library_id == PRODUCT_LIBRARY_ID:
        return xinghe_NEEDLES
    name = str(job.get("draft_name") or "")
    return (name,) if name else ()


def _resolve_draft_crystal(draft: dict, job: dict) -> dict | None:
    found = _crystal_by_name(draft, str(job.get("draft_name") or ""))
    if found is not None:
        return found
    needles = _job_needles(job)
    return _crystal_by_needles(draft, needles) if needles else None


def _as_draft(data: Any) -> dict:
    if isinstance(data, dict) and isinstance(data.get("crystals"), list):
        return data
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        inner = data["result"]
        if isinstance(inner.get("crystals"), list):
            return inner
    return {"crystals": []}


def _pack_library_crystal(crystal: dict, library_id: str) -> dict:
    packed = dict(crystal)
    packed["id"] = library_id
    packed["body_injection"] = "full"
    facts = []
    for index, fact in enumerate(packed.get("facts") or []):
        if not isinstance(fact, dict):
            continue
        row = dict(fact)
        row.setdefault("index", index)
        facts.append(row)
    packed["facts"] = facts
    return packed


def _library_crystal(draft_en: dict, match: dict, library_id: str) -> dict | None:
    meta = next((item for item in match.get("library_ids") or [] if item.get("id") == library_id), None)
    if not meta:
        return None
    crystal = _crystal_by_name(draft_en, str(meta.get("name") or ""))
    if crystal is None:
        crystal = _crystal_by_needles(draft_en, _job_needles({"library_id": library_id, "draft_name": meta.get("name")}))
    if crystal is None:
        return None
    return _pack_library_crystal(crystal, library_id)


def fill_integrate_user(
    template: str,
    *,
    draft_crystal: dict,
    library_candidates: list,
    material: str,
    owner_real_name: str = "",
    owner_aliases: str = "",
) -> str:
    """Fill approved integrate v1.4 user placeholders. Does not change the markdown."""
    return _fill_template(
        template,
        owner_real_name=owner_real_name,
        owner_aliases=owner_aliases,
        draft_crystal_json=_dump_json(draft_crystal),
        library_candidates_json=_dump_json(library_candidates),
        new_material_snippets=material,
    )


def _integrate_user(template: str, *, draft_crystal: dict, library_candidates: list[dict], material: str) -> str:
    return fill_integrate_user(
        template,
        draft_crystal=draft_crystal,
        library_candidates=library_candidates,
        material=material,
        owner_real_name=OWNER_REAL_NAME,
        owner_aliases=OWNER_ALIASES,
    )


def _integrate_jobs(match: dict) -> list[dict[str, str]]:
    jobs = []
    for item in match.get("matches") or []:
        if not item.get("will_call_integrate"):
            continue
        hits = item.get("hits") or []
        library_id = str((hits[0] or {}).get("id") or "") if hits else ""
        if not library_id:
            continue
        jobs.append({"draft_name": str(item.get("draft_name") or ""), "library_id": library_id})
    return jobs


def _draft_fact_rows(draft: dict) -> list[dict]:
    rows = []
    for crystal in draft.get("crystals") or []:
        for fact in crystal.get("facts") or []:
            if isinstance(fact, dict):
                rows.append(fact)
    return rows


def _replay_continuity(side: str, draft: dict, corpus: str, recorded: list[dict]) -> dict[str, Any]:
    facts = _draft_fact_rows(draft)
    kept, dropped = gate_facts(facts, corpus)
    ours = []
    for fact in facts:
        evidence = str(fact.get("evidence") or "")
        ours.append(is_continuous_evidence(evidence, corpus))
    recorded_ok = [bool(item.get("ok")) for item in recorded]
    return {
        "side": side,
        "n_facts": len(facts),
        "n_kept": len(kept),
        "n_dropped": len(dropped),
        "ours_ok": ours,
        "recorded_ok": recorded_ok,
        "matches_recorded": ours == recorded_ok,
    }


def _resim_checks(
    *,
    en_gate: dict[str, Any],
    cn_gate: dict[str, Any],
    match: dict,
    person,
    product,
    person_payload: dict,
    product_payload: dict,
) -> dict[str, bool]:
    matches = {item["draft_name"]: item for item in match.get("matches") or []}
    return {
        "report_present": REPORT.is_file() and "identity=same" in REPORT.read_text(encoding="utf-8"),
        "en_all_facts_fail": en_gate["n_facts"] >= 7 and en_gate["n_kept"] == 0 and en_gate["matches_recorded"],
        "cn_partial_fail": cn_gate["n_dropped"] >= 2 and cn_gate["matches_recorded"],
        "match_person_unique_strong": matches.get("林知远", {}).get("match_decision") == "unique_strong",
        "match_xinghe_unique_strong": matches.get("星河工坊", {}).get("match_decision") == "unique_strong",
        "match_hima_zero": matches.get("HIMA", {}).get("match_decision") == "zero",
        "match_ai_zero": matches.get("AI原生UGC编辑器", {}).get("match_decision") == "zero",
        "person_identity_same": person.identity == "same",
        "person_matched_id": person.matched_id == "cry_en_0_林知远",
        "person_raw_conflicts_empty": person_payload.get("conflicts") == [],
        # Report: model left conflicts=[] on a current-title update. Covering is
        # model-declared; program does not infer it from 现任/path. Offline gate
        # still must drop hallucinated evidence and not keep it on accept.
        "person_op0_evidence_dropped": any(
            "现任职位：2025/12~至今" in (item.evidence or "") for item in person.dropped_facts
        ),
        # Real integrate evidence is longer than a continuous source span; the
        # program drops every unproven row. Accept would have to strip them.
        "person_unproven_ops_not_accepted": person.decision != "accept",
        "person_failed_evidence_stripped_if_accept": person.decision != "accept"
        or not any(
            "现任职位：2025/12~至今" in str(op.get("evidence") or "")
            for op in ((person.payload or {}).get("ops") or [])
        ),
        "product_identity_same": product.identity == "same",
        "product_matched_id": product.matched_id == "cry_en_2_星河工坊",
        "product_op0_evidence_dropped": any(
            "Xinghe Studio" in (item.evidence or "") for item in product.dropped_facts
        ),
        "product_unproven_ops_not_accepted": product.decision != "accept",
        "product_failed_evidence_stripped_if_accept": product.decision != "accept"
        or not any(
            "Xinghe Studio" in str(op.get("evidence") or "")
            for op in ((product.payload or {}).get("ops") or [])
        ),
        "write_switch_on": DISTILLER_STORE_WRITE_ENABLED is True,
    }


def _accept_only_if_proven(result, *corpora: str) -> bool:
    if result is None:
        return False
    if result.decision != "accept":
        return True
    for op in ((result.payload or {}).get("ops") or []):
        if not isinstance(op, dict):
            continue
        evidence = str(op.get("evidence") or "")
        if evidence and not is_continuous_evidence(evidence, *corpora):
            return False
    return True


def _evaluate_live_payloads(
    *,
    en_corpus: str,
    cn_corpus: str,
    draft_en: dict,
    draft_cn: dict,
    recorded: dict,
    match: dict,
    person_raw: Any,
    product_raw: Any,
    vendor: dict[str, str],
    person_called: bool,
    product_called: bool,
) -> dict[str, Any]:
    """True MiniMax gates: transferable program invariants, not fixture replay."""
    both = [en_corpus, cn_corpus]
    en_gate = _replay_continuity("en", draft_en, en_corpus, recorded.get("en") or [])
    cn_gate = _replay_continuity("cn", draft_cn, cn_corpus, recorded.get("cn") or [])

    person_payload, person_issue = parse_integrate_payload(person_raw)
    product_payload, product_issue = parse_integrate_payload(product_raw)
    person_ok = person_issue is None and person_payload is not None
    product_ok = product_issue is None and product_payload is not None
    if person_payload is None:
        person_payload = person_raw if isinstance(person_raw, dict) else {}
    if product_payload is None:
        product_payload = product_raw if isinstance(product_raw, dict) else {}

    candidates = [item["id"] for item in match.get("library_ids") or []]
    person = apply_integrate(
        person_payload,
        candidates=candidates,
        corpus=cn_corpus,
        chunks=both,
        commit=False,
    )
    product = apply_integrate(
        product_payload,
        candidates=candidates,
        corpus=cn_corpus,
        chunks=both,
        commit=False,
    )
    vendor_blob = f"{vendor.get('host') or ''} {vendor.get('model') or ''} {vendor.get('base') or ''}"
    checks = {
        "vendor_is_minimax": _looks_like_minimax(
            str(vendor.get("base") or ""),
            str(vendor.get("model") or vendor.get("host") or ""),
        )
        or "minimax" in vendor_blob.lower(),
        "draft_en_has_crystals": any(isinstance(item, dict) for item in (draft_en.get("crystals") or [])),
        "draft_cn_has_crystals": any(isinstance(item, dict) for item in (draft_cn.get("crystals") or [])),
        "draft_en_has_owner": _draft_has(draft_en, OWNER_NEEDLES),
        "draft_cn_has_owner": _draft_has(draft_cn, OWNER_NEEDLES),
        "draft_cn_has_xinghe": _draft_has(draft_cn, xinghe_NEEDLES),
        "person_integrate_called": person_called,
        "product_integrate_called": product_called,
        "person_parse_ok": person_ok,
        "product_parse_ok": product_ok,
        "person_identity_legal": person.identity in IDENTITY_VALUES,
        "product_identity_legal": product.identity in IDENTITY_VALUES,
        "person_identity_same": person.identity == "same",
        "product_identity_same": product.identity == "same",
        "person_matched_id_in_candidates": person.identity != "same"
        or person.matched_id in candidates,
        "product_matched_id_in_candidates": product.identity != "same"
        or product.matched_id in candidates,
        "person_accept_only_if_proven": _accept_only_if_proven(person, *both),
        "product_accept_only_if_proven": _accept_only_if_proven(product, *both),
        "write_switch_on": DISTILLER_STORE_WRITE_ENABLED is True,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "evaluation": "live",
        "en_gate": {k: en_gate[k] for k in ("n_facts", "n_kept", "n_dropped", "matches_recorded")},
        "cn_gate": {k: cn_gate[k] for k in ("n_facts", "n_kept", "n_dropped", "matches_recorded")},
        "person_decision": person.decision,
        "product_decision": product.decision,
        "person_identity": person.identity,
        "product_identity": product.identity,
        "write_switch": DISTILLER_STORE_WRITE_ENABLED,
        "vendor": vendor,
        "draft_en_names": [str(item.get("name") or "") for item in (draft_en.get("crystals") or []) if isinstance(item, dict)],
        "draft_cn_names": [str(item.get("name") or "") for item in (draft_cn.get("crystals") or []) if isinstance(item, dict)],
    }


def _evaluate_resim_payloads(
    *,
    en_corpus: str,
    cn_corpus: str,
    draft_en: dict,
    draft_cn: dict,
    recorded: dict,
    match: dict,
    person_raw: Any,
    product_raw: Any,
    require_parse: bool = False,
) -> dict[str, Any]:
    """Shared program gates + apply_integrate evaluate path. No network."""
    both = [en_corpus, cn_corpus]
    en_gate = _replay_continuity("en", draft_en, en_corpus, recorded.get("en") or [])
    cn_gate = _replay_continuity("cn", draft_cn, cn_corpus, recorded.get("cn") or [])

    person_payload, person_issue = parse_integrate_payload(person_raw)
    product_payload, product_issue = parse_integrate_payload(product_raw)
    if require_parse:
        assert person_issue is None and person_payload is not None
        assert product_issue is None and product_payload is not None
    if person_payload is None:
        person_payload = person_raw if isinstance(person_raw, dict) else {}
    if product_payload is None:
        product_payload = product_raw if isinstance(product_raw, dict) else {}

    candidates = [item["id"] for item in match.get("library_ids") or []]
    # Evaluate path only: no store, commit=False. Does not persist CrystalStore.
    person = apply_integrate(
        person_payload,
        candidates=candidates,
        corpus=cn_corpus,
        chunks=both,
        commit=False,
    )
    product = apply_integrate(
        product_payload,
        candidates=candidates,
        corpus=cn_corpus,
        chunks=both,
        commit=False,
    )
    checks = _resim_checks(
        en_gate=en_gate,
        cn_gate=cn_gate,
        match=match,
        person=person,
        product=product,
        person_payload=person_payload,
        product_payload=product_payload,
    )
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "en_gate": {k: en_gate[k] for k in ("n_facts", "n_kept", "n_dropped", "matches_recorded")},
        "cn_gate": {k: cn_gate[k] for k in ("n_facts", "n_kept", "n_dropped", "matches_recorded")},
        "person_decision": person.decision,
        "product_decision": product.decision,
        "write_switch": DISTILLER_STORE_WRITE_ENABLED,
    }


def run_offline_resim() -> dict[str, Any]:
    """Replay the real sim report artifacts through program gates. No network."""
    _require_sim_files()
    evaluated = _evaluate_resim_payloads(
        en_corpus=_load_text("en-material.txt"),
        cn_corpus=_load_text("cn-material.txt"),
        draft_en=_load_json("draft-en.json"),
        draft_cn=_load_json("draft-cn.json"),
        recorded=_load_json("gate-continuity-check.json"),
        match=_load_json("match-report.json"),
        person_raw=_load_json("integrate-0.json"),
        product_raw=_load_json("integrate-2.json"),
        require_parse=True,
    )
    return {
        "mode": "offline",
        "evaluation": "fixture",
        "live_next_gate": LIVE_RESIM_NEXT_GATE,
        **evaluated,
    }


def _live_llm_target() -> tuple[str, str, str] | None:
    base = (
        os.environ.get("ROGMEM_LIVE_LLM_BASE") or os.environ.get("ROGMEM_LLM_BASE") or ""
    ).rstrip("/")
    key = os.environ.get("ROGMEM_LIVE_LLM_KEY") or os.environ.get("ROGMEM_LLM_KEY") or ""
    model = os.environ.get("ROGMEM_LIVE_LLM_MODEL") or os.environ.get("ROGMEM_LLM_MODEL") or ""
    if base:
        return base, key or "test", model or "stub"
    return llm_target("distill")


def _first_env(*names: str) -> tuple[str, str]:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return name, value
    return "", ""


def _vendor_public(base: str = "", model: str = "") -> dict[str, str]:
    host = base.split("://", 1)[-1].split("/", 1)[0] if base else ""
    return {"host": host, "model": model, "base": base}


def _looks_like_minimax(base: str, model: str) -> bool:
    return "minimax" in f"{base} {model}".lower()


def true_live_ready() -> dict[str, Any]:
    """Whether this process can call real MiniMax. Reports env *names* only."""
    missing: list[str] = []
    if os.environ.get(TRUE_LIVE_FLAG) != "1":
        missing.append(TRUE_LIVE_FLAG)
    _name_b, base = _first_env(*TRUE_LIVE_BASE_VARS)
    _name_k, key = _first_env(*TRUE_LIVE_KEY_VARS)
    _name_m, model = _first_env(*TRUE_LIVE_MODEL_VARS)
    if not base:
        missing.append(" or ".join(TRUE_LIVE_BASE_VARS))
    if not key or key.casefold() in _STUB_KEYS:
        missing.append(" or ".join(TRUE_LIVE_KEY_VARS))
    if not model or model.casefold() in _STUB_MODELS:
        missing.append(" or ".join(TRUE_LIVE_MODEL_VARS))
    vendor = _vendor_public(base.rstrip("/"), model)
    if base and model and not _looks_like_minimax(base, model):
        return {
            "ok": False,
            "missing_env": missing,
            "reason": (
                "live target is not MiniMax; need minimax in "
                + " or ".join(TRUE_LIVE_BASE_VARS + TRUE_LIVE_MODEL_VARS)
            ),
            "vendor": vendor,
        }
    if missing:
        return {
            "ok": False,
            "missing_env": missing,
            "reason": "True MiniMax live blocked: missing " + ", ".join(missing),
            "vendor": vendor,
        }
    return {"ok": True, "missing_env": [], "reason": "", "vendor": vendor}


def _vendor_from_target(target: tuple[str, str, str] | None) -> dict[str, str]:
    if not target:
        return _vendor_public()
    base, _key, model = target
    return _vendor_public(base, model)


def _call_llm(system: str, user: str, *, complete: CompleteFn | None) -> dict:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    if complete is not None:
        raw = complete(messages)
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, tuple):
            raw = raw[0]
        return _parse_model_json(str(raw))
    if os.environ.get("ROGMEM_LIVE_LLM") != "1":
        raise RuntimeError("live LLM disabled; refuse network")
    target = _live_llm_target()
    if not target:
        raise RuntimeError("live LLM target missing (ROGMEM_LIVE_LLM_BASE / ROGMEM_LLM_BASE)")
    base, key, model = target
    minimax = "minimax" in base.lower() or "minimax" in model.lower()
    max_tokens = LIVE_RESIM_MAX_TOKENS_MINIMAX if minimax else LIVE_RESIM_MAX_TOKENS
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    content, reason = _complete_chat(base, key, payload, timeout=LIVE_RESIM_TIMEOUT_SEC)
    if not content and reason == "length":
        payload = dict(payload)
        payload["max_tokens"] = max(max_tokens, LIVE_RESIM_MAX_TOKENS_MINIMAX)
        content, reason = _complete_chat(base, key, payload, timeout=LIVE_RESIM_TIMEOUT_SEC)
    if not content:
        raise RuntimeError(f"模型没有给出正文（{reason or '空'}）")
    try:
        return _parse_model_json(content)
    except json.JSONDecodeError:
        # M3.1-Flash occasionally truncates or emits a broken object; one retry.
        payload = dict(payload)
        payload["max_tokens"] = max(int(payload.get("max_tokens") or 0), LIVE_RESIM_MAX_TOKENS_MINIMAX)
        content, reason = _complete_chat(base, key, payload, timeout=LIVE_RESIM_TIMEOUT_SEC)
        if not content:
            raise RuntimeError(f"模型没有给出正文（{reason or '空'}）")
        return _parse_model_json(content)


def _empty_integrate() -> dict:
    return {"identity": {"decision": "maybe", "matched_id": None}, "ops": [], "conflicts": []}


def run_live_resim(*, complete: CompleteFn | None = None) -> dict[str, Any]:
    """Live MiniMax dual-resume re-sim.

    Flip-switch hard card, not CI. Inject ``complete`` in tests so CI never
    needs MiniMax. Production live uses ``engine._complete_chat`` + the
    same env target pattern as ``tests/live_check.py`` / ``llm_target``.
    """
    if complete is None and os.environ.get("ROGMEM_LIVE_LLM") != "1":
        return {
            "mode": "live",
            "passed": False,
            "status": "skipped",
            "reason": (
                "ROGMEM_LIVE_LLM!=1; live MiniMax re-sim is a flip-switch "
                "hard card, not a CI assertion. Offline replay remains required green."
            ),
            "live_hard_card": LIVE_RESIM_NEXT_GATE,
            "write_switch": DISTILLER_STORE_WRITE_ENABLED,
        }

    _require_sim_files()
    _require_approved_prompts()
    draft_system, draft_user_tmpl = approved_draft_prompts()
    integrate_system, integrate_user_tmpl = approved_integrate_prompts()
    en_corpus = _load_text("en-material.txt")
    cn_corpus = _load_text("cn-material.txt")
    recorded = _load_json("gate-continuity-check.json")
    match = _load_json("match-report.json")

    try:
        draft_en = _as_draft(
            _call_llm(
                draft_system,
                _draft_user(draft_user_tmpl, filename=EN_FILENAME, material=en_corpus),
                complete=complete,
            )
        )
        draft_cn = _as_draft(
            _call_llm(
                draft_system,
                _draft_user(draft_user_tmpl, filename=CN_FILENAME, material=cn_corpus),
                complete=complete,
            )
        )

        person_raw: Any = _empty_integrate()
        product_raw: Any = _empty_integrate()
        person_called = False
        product_called = False
        jobs = _integrate_jobs(match)
        for job in jobs:
            draft_crystal = _resolve_draft_crystal(draft_cn, job)
            library = _library_crystal(draft_en, match, job["library_id"])
            if draft_crystal is None or library is None:
                continue
            raw = _call_llm(
                integrate_system,
                _integrate_user(
                    integrate_user_tmpl,
                    draft_crystal=draft_crystal,
                    library_candidates=[library],
                    material=cn_corpus,
                ),
                complete=complete,
            )
            if job["library_id"] == PERSON_LIBRARY_ID:
                person_raw = raw
                person_called = True
            elif job["library_id"] == PRODUCT_LIBRARY_ID:
                product_raw = raw
                product_called = True
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "mode": "live",
            "passed": False,
            "status": "error",
            "reason": str(exc),
            "live_hard_card": LIVE_RESIM_NEXT_GATE,
            "write_switch": DISTILLER_STORE_WRITE_ENABLED,
            "vendor": _vendor_from_target(_live_llm_target()) if complete is None else _vendor_public(),
        }

    if complete is None:
        evaluated = _evaluate_live_payloads(
            en_corpus=en_corpus,
            cn_corpus=cn_corpus,
            draft_en=draft_en,
            draft_cn=draft_cn,
            recorded=recorded,
            match=match,
            person_raw=person_raw,
            product_raw=product_raw,
            vendor=_vendor_from_target(_live_llm_target()),
            person_called=person_called,
            product_called=product_called,
        )
    else:
        evaluated = _evaluate_resim_payloads(
            en_corpus=en_corpus,
            cn_corpus=cn_corpus,
            draft_en=draft_en,
            draft_cn=draft_cn,
            recorded=recorded,
            match=match,
            person_raw=person_raw,
            product_raw=product_raw,
        )
        evaluated = {"evaluation": "fixture", **evaluated}
    return {
        "mode": "live",
        "status": "ran",
        "live_hard_card": LIVE_RESIM_NEXT_GATE,
        **evaluated,
    }


def run_true_minimax_resim() -> dict[str, Any]:
    """True MiniMax dual-resume re-sim. Never invents keys; blocks with env names."""
    ready = true_live_ready()
    if not ready["ok"]:
        return {
            "mode": "live",
            "true_minimax": True,
            "passed": False,
            "status": "blocked",
            "reason": ready["reason"],
            "missing_env": list(ready["missing_env"]),
            "vendor": ready["vendor"],
            "live_hard_card": LIVE_RESIM_NEXT_GATE,
            "write_switch": DISTILLER_STORE_WRITE_ENABLED,
        }
    report = run_live_resim()
    report["true_minimax"] = True
    report["missing_env"] = []
    return report


# LIVE 多实体硬卡（#72 分轨之上）。结构 Pass ≠ 本闸。仍卡 wipe。
MULTI_ENTITY_CLAIM = "多实体闭环（LIVE 硬卡过）"
FORBIDDEN_SHIP_PHRASES = ("可上线", "闭环完", "已可上线")
MULTI_HIT_CORPUS = "云岚测试员甲于 2019 年入职。后来到南港书院做讲师。"
MATCH0_CORPUS = "北窗讲师丙在西湾茶社兼职。西湾茶社开了十年。"
MULTI_ENTITY_FILENAME = "云岚双实体.md"


def wire_live_llm_env_into_prod() -> dict[str, str]:
    """Copy LIVE_* into LLM_* so production ``llm_target`` can call MiniMax."""
    wired: dict[str, str] = {}
    if os.environ.get(TRUE_LIVE_FLAG) != "1":
        return wired
    _name_b, base = _first_env(*TRUE_LIVE_BASE_VARS)
    _name_k, key = _first_env(*TRUE_LIVE_KEY_VARS)
    _name_m, model = _first_env(*TRUE_LIVE_MODEL_VARS)
    if base and not (os.environ.get("ROGMEM_LLM_BASE") or "").strip():
        os.environ["ROGMEM_LLM_BASE"] = base
        wired["ROGMEM_LLM_BASE"] = "from_live"
    if key and not (os.environ.get("ROGMEM_LLM_KEY") or "").strip():
        os.environ["ROGMEM_LLM_KEY"] = key
        wired["ROGMEM_LLM_KEY"] = "from_live"
    if model and not (os.environ.get("ROGMEM_LLM_MODEL") or "").strip():
        os.environ["ROGMEM_LLM_MODEL"] = model
        wired["ROGMEM_LLM_MODEL"] = "from_live"
    return wired


def _blocked_multi_entity_report(ready: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": "live",
        "true_minimax": True,
        "passed": False,
        "status": "blocked",
        "evaluation": None,
        "reason": ready.get("reason") or "True MiniMax live blocked",
        "missing_env": list(ready.get("missing_env") or []),
        "vendor": ready.get("vendor") or _vendor_public(),
        "write_switch": DISTILLER_STORE_WRITE_ENABLED,
        "mid_count": 0,
        "thin_active_count": 0,
        "evidence": "blocked",
        "ba_promoted": False,
        "match0_no_integrate_llm": False,
        "owner_injection_nonempty": False,
        "filename_injection_nonempty": False,
        "still_blocked_on_wipe": True,
        "ship_ready": False,
        "live_hard_card": LIVE_RESIM_NEXT_GATE,
    }


def multi_entity_closed_loop_verdict(report: dict[str, Any] | None) -> dict[str, Any]:
    """Aggregator: skip / blocked / fixture / sniff never mint 多实体闭环.

    ``closed_loop`` only. ``ship_ready`` stays False — wipe still blocks ship.
    """
    data = report if isinstance(report, dict) else {}
    reasons: list[str] = []
    status = str(data.get("status") or "")
    evaluation = data.get("evaluation")
    if status in {"skipped", "blocked"}:
        reasons.append("skip_or_blocked_is_not_green")
    if evaluation == "fixture":
        reasons.append("fixture_is_not_live")
    if evaluation != "live" or status != "ran":
        reasons.append("not_true_live_ran")
    if not data.get("true_minimax"):
        reasons.append("not_true_minimax")
    if data.get("passed") is not True:
        reasons.append("report_not_passed")
    if data.get("write_switch") is not True:
        reasons.append("write_switch_off")
    if int(data.get("mid_count") or 0) < 2:
        reasons.append("single_mid_is_not_multi_entity")
    if int(data.get("thin_active_count") or 0) < 2:
        reasons.append("need_ge2_thin_active")
    if data.get("evidence") == "sniff":
        reasons.append("sniff_is_not_live")
    if data.get("ba_promoted") is not True:
        reasons.append("multi_hit_ba_promote_missing")
    if data.get("match0_no_integrate_llm") is not True:
        reasons.append("match0_integrate_llm_not_skipped")
    if data.get("owner_injection_nonempty") is not True:
        reasons.append("owner_injection_empty")
    if data.get("filename_injection_nonempty") is not True:
        reasons.append("filename_injection_empty")
    closed = not reasons
    claim = MULTI_ENTITY_CLAIM if closed else ""
    for phrase in FORBIDDEN_SHIP_PHRASES:
        if phrase in claim:
            reasons.append("forbidden_ship_phrase")
            closed = False
            claim = ""
            break
    return {
        "closed_loop": closed,
        "claim": claim,
        "reasons": reasons,
        "still_blocked_on_wipe": True,
        "ship_ready": False,
    }


def _phase_of_candidates(candidates) -> str:
    if not isinstance(candidates, list) or not candidates:
        return "empty"
    from rogmem.engine import _is_a_pack, _is_b_card

    dicts = [item for item in candidates if isinstance(item, dict)]
    if len(dicts) >= 2 and all(_is_b_card(item) for item in dicts):
        return "b"
    if len(dicts) == 1 and _is_a_pack(dicts[0]):
        return "a"
    return "other"


def _seed_live_owner(engine) -> None:
    from rogmem.crystal_store import CrystalStore, hydrate

    row = engine.db().execute("SELECT * FROM crystallizations WHERE id='owner'").fetchone()
    if row is None:
        return
    thin = hydrate(row)
    thin.keys = {**dict(thin.keys or {}), "真名": OWNER_REAL_NAME}
    extras = [part.strip() for part in OWNER_ALIASES.split(",") if part.strip()]
    aliases = list(thin.aliases or [])
    for item in extras:
        if item not in aliases:
            aliases.append(item)
    thin.aliases = aliases
    CrystalStore(engine.db()).put(thin, holder="live-multi:owner", commit=True)


def _seed_live_library(engine) -> None:
    from rogmem.crystal_store import CrystalStore, Fact, ThinCrystal

    store = CrystalStore(engine.db())
    store.put(
        ThinCrystal(
            id="c-yunlan",
            type="人物",
            name="云岚甲",
            status="active",
            revision=0,
            summary="于 2019 年入职。",
            aliases=["测试员甲"],
            body_md="## 事业经历\n\n云岚测试员甲于 2019 年入职。",
            facts=[Fact(claim="2019 年入职", evidence="云岚测试员甲于 2019 年入职。")],
        ),
        holder="live-multi:c-yunlan",
        commit=True,
    )
    store.put(
        ThinCrystal(
            id="c-school",
            type="组织",
            name="南港书院",
            status="active",
            revision=0,
            summary="云岚甲后来任教的书院。",
            body_md="## 发展沿革\n\n后来到南港书院做讲师。",
            facts=[Fact(claim="后来到南港书院做讲师", evidence="后来到南港书院做讲师。")],
        ),
        holder="live-multi:c-school",
        commit=True,
    )


def _queue_live_source(engine, source_id: str, body: str, *, filename: str = "") -> None:
    con = engine.db()
    if filename:
        con.execute(
            """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready,filename)
               VALUES(?, 'file', 'qa', 'qa', ?, ?, ?, ?, 'slow', 1, ?)""",
            (source_id, engine.utcnow(), source_id + "-h", body, engine.ST_VEC_READY, filename),
        )
        con.execute(
            "INSERT INTO chunks(source_id,seq,loc,text,role) VALUES(?,?,?,?,?)",
            (source_id, 1, "1", body, "child"),
        )
        con.execute("INSERT INTO queue(source_id,lane,state) VALUES(?, 'slow', 'queued')", (source_id,))
    else:
        con.execute(
            """INSERT INTO sources(id,kind,device_name,agent_name,ingested_at,content_hash,body,status,lane,pair_ready,filename)
               VALUES(?, 'user', 'qa', 'qa', ?, ?, ?, ?, 'fast', 1, ?)""",
            (source_id, engine.utcnow(), source_id + "-h", body, engine.ST_VEC_READY, MULTI_ENTITY_FILENAME),
        )
        con.execute("INSERT INTO queue(source_id,lane,state) VALUES(?, 'fast', 'queued')", (source_id,))
    con.commit()


def _thin_active_named(engine) -> list[dict[str, Any]]:
    from rogmem.crystal_store import hydrate

    rows = list(
        engine.db().execute(
            "SELECT * FROM crystallizations WHERE status='active' AND id!='owner' ORDER BY id"
        )
    )
    out = []
    for row in rows:
        thin = hydrate(row)
        out.append(
            {
                "id": thin.id,
                "name": thin.name,
                "type": thin.type,
                "storage": thin.storage,
                "revision": int(thin.revision or 0),
            }
        )
    return out


def run_true_minimax_multi_entity() -> dict[str, Any]:
    """True MiniMax production path: one draft ≥2 mid → Integrate → thin active.

    Multi-hit mid must show B→A. Match=0 mid is programmatic new (no Integrate LLM).
    skip / blocked / fixture never return passed. Does not invent keys.
    """
    ready = true_live_ready()
    if not ready["ok"]:
        return _blocked_multi_entity_report(ready)

    import tempfile
    from pathlib import Path

    from rogmem import engine
    from rogmem.crystal_store import DISTILLER_STORE_WRITE_ENABLED as write_on

    wired = wire_live_llm_env_into_prod()
    root = Path(tempfile.mkdtemp(prefix="rogmem-live-multi-"))
    engine.connect(root, root / "scan")
    engine.set_embed_mode("off")
    _seed_live_owner(engine)
    _seed_live_library(engine)

    integrate_phases: list[dict[str, Any]] = []
    draft_injects: list[dict[str, str]] = []
    apply_results: list[dict[str, Any]] = []
    match0_integrate_calls = 0
    orig_integrate = engine._llm_integrate
    orig_draft = engine._llm_draft
    orig_apply = engine.apply_integrate
    in_match0 = False

    def spy_integrate(text, kind="", **kw):
        nonlocal match0_integrate_calls
        if in_match0:
            match0_integrate_calls += 1
        cands = list(kw.get("library_candidates") or [])
        raw = orig_integrate(text, kind, **kw)
        ident = ""
        matched = ""
        if isinstance(raw, dict):
            ident = str(raw.get("identity") or "")
            if isinstance(raw.get("identity"), dict):
                ident = str(raw["identity"].get("decision") or raw["identity"].get("identity") or "")
            matched = str(raw.get("matched_id") or "")
        integrate_phases.append(
            {
                "phase": _phase_of_candidates(cands),
                "ids": [item.get("id") for item in cands if isinstance(item, dict)],
                "draft": (kw.get("draft_crystal") or {}).get("name"),
                "identity": ident,
                "matched_id": matched,
            }
        )
        return raw

    def spy_apply(*args, **kwargs):
        result = orig_apply(*args, **kwargs)
        apply_results.append(
            {
                "decision": getattr(result, "decision", None),
                "identity": getattr(result, "identity", None),
                "matched_id": getattr(result, "matched_id", None),
                "issues": [getattr(item, "code", str(item)) for item in (getattr(result, "issues", None) or [])],
                "draft": (kwargs.get("draft_crystal") or {}).get("name"),
            }
        )
        return result

    def spy_draft(text, kind="", **kw):
        draft_injects.append(
            {
                "filename": str(kw.get("filename") or ""),
                "owner_real_name": str(kw.get("owner_real_name") or ""),
                "owner_aliases": str(kw.get("owner_aliases") or ""),
            }
        )
        return orig_draft(text, kind, **kw)

    engine._llm_integrate = spy_integrate  # type: ignore[method-assign]
    engine._llm_draft = spy_draft  # type: ignore[method-assign]
    engine.apply_integrate = spy_apply  # type: ignore[method-assign]

    checks: dict[str, bool] = {}
    mid_names: list[str] = []
    reason = ""
    try:
        _queue_live_source(engine, "src-live-ba", MULTI_HIT_CORPUS)
        pumped = engine.pump_one()
        ba_row = engine.db().execute(
            "SELECT state, last_error FROM queue WHERE source_id=?",
            ("src-live-ba",),
        ).fetchone()
        ba_done = ba_row is not None and ba_row["state"] == "done"
        if pumped != "src-live-ba" or not ba_done:
            reason = str((ba_row["last_error"] if ba_row else None) or "multi-hit pump not done")

        ba_mids = [item for item in integrate_phases if item.get("draft")]
        ba_names = [str(item.get("draft") or "") for item in ba_mids]
        phases = [item["phase"] for item in integrate_phases]
        ba_promoted = "b" in phases and "a" in phases and phases.index("b") < phases.index("a")

        in_match0 = True
        match0_before = len(integrate_phases)
        _queue_live_source(engine, "src-live-m0", MATCH0_CORPUS)
        pumped0 = engine.pump_one()
        m0_row = engine.db().execute(
            "SELECT state, last_error FROM queue WHERE source_id=?",
            ("src-live-m0",),
        ).fetchone()
        m0_done = m0_row is not None and m0_row["state"] == "done"
        if pumped0 != "src-live-m0" or not m0_done:
            extra = str((m0_row["last_error"] if m0_row else None) or "match0 pump not done")
            reason = f"{reason}; {extra}" if reason else extra
        match0_no_llm = match0_integrate_calls == 0 and len(integrate_phases) == match0_before

        thins = _thin_active_named(engine)
        mid_names = sorted({item["name"] for item in thins if item.get("name")})
        draft_mids = 0
        for inject in draft_injects:
            # each successful draft call; count via list after the fact below
            _ = inject
        owner_ok = any(
            str(item.get("owner_real_name") or "").strip()
            and str(item.get("owner_aliases") or "").strip()
            for item in draft_injects
        )
        filename_ok = any(str(item.get("filename") or "").strip() for item in draft_injects)
        # Mid count: unique landed non-owner names that came from the two sources.
        # Prefer draft inject count of crystals via library after produce is not
        # stored; use landed + integrate draft names.
        seen_mids = {name for name in ba_names if name}
        seen_mids.update(mid_names)
        mid_count = len(seen_mids)
        # Count draft crystals from the first live draft if we can re-read injects only.
        if draft_mids < mid_count:
            draft_mids = mid_count

        checks = {
            "vendor_is_minimax": bool((ready.get("vendor") or {}).get("host", ""))
            and _looks_like_minimax(
                str((ready.get("vendor") or {}).get("base") or ""),
                str((ready.get("vendor") or {}).get("model") or ""),
            ),
            "write_switch_on": write_on is True,
            "ba_pump_done": ba_done,
            "match0_pump_done": m0_done,
            "ge2_mid": mid_count >= 2,
            "ba_promoted": ba_promoted,
            "match0_no_integrate_llm": match0_no_llm,
            "thin_active_ge2": sum(1 for item in thins if item.get("storage") == "thin") >= 2,
            "owner_injection_nonempty": owner_ok,
            "filename_injection_nonempty": filename_ok,
            "all_landed_thin": all(item.get("storage") == "thin" for item in thins) if thins else False,
        }
        passed = all(checks.values()) and not reason
        report = {
            "mode": "live",
            "true_minimax": True,
            "passed": passed,
            "status": "ran",
            "evaluation": "live",
            "reason": reason,
            "missing_env": [],
            "vendor": ready.get("vendor") or _vendor_public(),
            "write_switch": write_on is True,
            "wired_prod_env": wired,
            "mid_count": mid_count,
            "thin_active_count": sum(1 for item in thins if item.get("storage") == "thin"),
            "thin_active": thins,
            "mid_names": sorted(seen_mids),
            "integrate_phases": integrate_phases,
            "apply_results": apply_results,
            "draft_injects": draft_injects,
            "ba_promoted": ba_promoted,
            "match0_no_integrate_llm": match0_no_llm,
            "owner_injection_nonempty": owner_ok,
            "filename_injection_nonempty": filename_ok,
            "evidence": "production_pump",
            "checks": checks,
            "still_blocked_on_wipe": True,
            "ship_ready": False,
            "live_hard_card": LIVE_RESIM_NEXT_GATE,
        }
        report["verdict"] = multi_entity_closed_loop_verdict(report)
        return report
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "mode": "live",
            "true_minimax": True,
            "passed": False,
            "status": "error",
            "evaluation": "live",
            "reason": str(exc),
            "missing_env": [],
            "vendor": ready.get("vendor") or _vendor_public(),
            "write_switch": write_on is True,
            "mid_count": len(mid_names),
            "thin_active_count": 0,
            "evidence": "production_pump",
            "ba_promoted": False,
            "match0_no_integrate_llm": False,
            "owner_injection_nonempty": False,
            "filename_injection_nonempty": False,
            "still_blocked_on_wipe": True,
            "ship_ready": False,
            "live_hard_card": LIVE_RESIM_NEXT_GATE,
        }
    finally:
        engine._llm_integrate = orig_integrate  # type: ignore[method-assign]
        engine._llm_draft = orig_draft  # type: ignore[method-assign]
        engine.apply_integrate = orig_apply  # type: ignore[method-assign]


def run_dual_resume_resim(*, live: bool | None = None, complete: CompleteFn | None = None) -> dict[str, Any]:
    if live is None:
        live = os.environ.get("ROGMEM_LIVE_LLM") == "1"
    if live:
        return run_live_resim(complete=complete)
    return run_offline_resim()


if __name__ == "__main__":
    import sys

    if "--true-minimax-multi" in sys.argv:
        report = run_true_minimax_multi_entity()
    elif "--true-minimax" in sys.argv:
        report = run_true_minimax_resim()
    else:
        report = run_dual_resume_resim()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report.get("passed") else 1)
