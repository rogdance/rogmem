"""Distiller merge-gate harness: shape, evidence, 实/推, ops↔conflicts (v3 §6.7 / §8)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from rogmem.crystal_store import DISTILLER_STORE_WRITE_ENABLED, CrystalStore, migrate_crystal_store
from rogmem.distill_gate import (
    evaluate_integrate,
    is_continuous_evidence,
    is_covering_op,
    normalize_corpus,
    persist_unresolved,
    run_merge_gate,
)

CORPUS = "云岚测试员甲于 2019 年在北窗实验室任职。后来到南港书院做讲师。"
CANDIDATES = ["c-old"]


def test_normalize_folds_whitespace_keeps_case_and_punct():
    assert normalize_corpus("  甲\n乙\t丙  ") == "甲 乙 丙"
    assert normalize_corpus("GitHub，OpenAI") == "GitHub，OpenAI"
    assert is_continuous_evidence("甲\n乙", "甲 乙 丙")
    assert not is_continuous_evidence("甲丙", "甲 乙 丙")


def test_shape_new_requires_empty_ops_and_conflicts():
    result = evaluate_integrate(
        {
            "identity": "new",
            "ops": [{"op": "add", "path": "facts"}],
            "conflicts": [],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert result.decision == "rejected"
    assert any(item.code == "ops_conflicts_must_be_empty" for item in result.issues)


def test_matched_id_must_be_in_candidates():
    result = evaluate_integrate(
        {"identity": "same", "matched_id": "ghost", "ops": [], "conflicts": []},
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert result.decision == "rejected"
    assert any(item.code == "matched_id_not_in_candidates" for item in result.issues)


def test_two_candidates_reuse_same_new_maybe_shape():
    """≥2 library ids use the existing identity gate — no extra ≥2 rule."""
    candidates = ["c-lab", "c-school"]
    same = evaluate_integrate(
        {"identity": "same", "matched_id": "c-school", "ops": [], "conflicts": []},
        candidates=candidates,
        corpus=CORPUS,
    )
    assert same.decision == "accept"
    assert same.matched_id == "c-school"
    ghost = evaluate_integrate(
        {"identity": "same", "matched_id": "ghost", "ops": [], "conflicts": []},
        candidates=candidates,
        corpus=CORPUS,
    )
    assert ghost.decision == "rejected"
    assert any(item.code == "matched_id_not_in_candidates" for item in ghost.issues)
    for identity in ("new", "maybe"):
        ok = evaluate_integrate(
            {"identity": identity, "ops": [], "conflicts": []},
            candidates=candidates,
            corpus=CORPUS,
        )
        assert ok.decision == "accept"
        assert ok.identity == identity
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_illegal_enum_rejected():
    result = evaluate_integrate(
        {
            "identity": "merge",
            "matched_id": "c-old",
            "ops": [{"op": "patch"}],
            "conflicts": [{"verdict": "overwrite", "path": "x"}],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    codes = {item.code for item in result.issues}
    assert "illegal_identity" in codes
    assert "illegal_op" in codes
    assert "illegal_verdict" in codes


def test_unparseable_json_defers_after_failed_repair():
    result = evaluate_integrate("这不是 JSON", candidates=CANDIDATES, corpus=CORPUS)
    assert result.decision == "deferred"
    assert result.issues[0].code == "unparseable_json"


def test_fenced_json_is_repaired():
    raw = '```json\n{"identity":"maybe","ops":[],"conflicts":[]}\n```'
    result = evaluate_integrate(raw, candidates=CANDIDATES, corpus=CORPUS)
    assert result.decision == "accept"
    assert result.identity == "maybe"


def test_evidence_drop_is_per_fact_not_whole_doc():
    result = evaluate_integrate(
        {
            "identity": "new",
            "ops": [],
            "conflicts": [],
            "facts": [
                {
                    "claim": "2019 入职",
                    "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
                },
                {"claim": "编造", "evidence": "正文里没有这句"},
            ],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert result.decision == "accept"
    assert len(result.kept_facts) == 1
    assert len(result.dropped_facts) == 1
    assert result.dropped_facts[0].reason == "evidence_not_substring"


def test_all_facts_fail_rejected():
    result = evaluate_integrate(
        {
            "identity": "new",
            "ops": [],
            "conflicts": [],
            "facts": [{"claim": "无", "evidence": "对不上的句子"}],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert result.decision == "rejected"
    assert any(item.code == "all_facts_dropped" for item in result.issues)


def test_all_facts_dropped_on_same_still_rejected_at_gate():
    from rogmem import engine
    from rogmem.distill_gate import GateResult

    result = evaluate_integrate(
        {
            "identity": "same",
            "matched_id": "c-old",
            "ops": [],
            "conflicts": [],
            "facts": [{"claim": "无", "evidence": "对不上的句子"}],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert result.decision == "rejected"
    assert result.identity == "same"
    assert any(item.code == "all_facts_dropped" for item in result.issues)
    assert engine._batch_mid_skippable(result)
    accepted = GateResult(decision="accept", identity="same", matched_id="c-old")
    engine._raise_if_new_land_unwritten([accepted, result])


def test_path_and_core_module_misses_do_not_sink_the_file():
    from rogmem import engine
    from rogmem.distill_gate import GateIssue, GateResult

    missed = GateResult(
        decision="rejected",
        identity="same",
        matched_id="owner",
        issues=[
            GateIssue("unsupported_apply_path", "apply 不支持 update/chapters.事业经历"),
            GateIssue("core_module_ops_missing", "缺少正文节"),
            GateIssue("conflict_evidence_not_substring", "引文对不上"),
        ],
    )
    assert engine._batch_mid_skippable(missed)
    kept = GateResult(decision="accept", identity="new", matched_id="page-1")
    engine._raise_if_new_land_unwritten([kept, missed])


def test_inferences_skip_substring_and_reject_fake_evidence():
    ok = evaluate_integrate(
        {
            "identity": "new",
            "ops": [],
            "conflicts": [],
            "facts": [
                {
                    "claim": "2019 入职",
                    "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
                }
            ],
            "inferences": [{"claim": "两份材料是同一人", "basis": "姓名对得上"}],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert ok.decision == "accept"
    assert ok.inferences[0]["epistemics"] == "inferred"
    fake = evaluate_integrate(
        {
            "identity": "new",
            "ops": [],
            "conflicts": [],
            "facts": [
                {
                    "claim": "2019 入职",
                    "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
                }
            ],
            "inferences": [{"claim": "猜的", "evidence": "伪造出处"}],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert fake.decision == "rejected"
    assert any(item.code == "fake_inference_evidence" for item in fake.issues)


def test_inference_must_not_overwrite_fact_claim_or_supersede_asserted():
    overlap = evaluate_integrate(
        {
            "identity": "new",
            "ops": [],
            "conflicts": [],
            "facts": [
                {
                    "claim": "在北窗实验室任职",
                    "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
                }
            ],
            "inferences": [{"claim": "在北窗实验室任职", "basis": "重复一句"}],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert overlap.decision == "rejected"
    assert any(item.code == "inference_overwrites_fact" for item in overlap.issues)
    supersede = evaluate_integrate(
        {
            "identity": "same",
            "matched_id": "c-old",
            "ops": [{"op": "keep", "path": "facts.0"}],
            "conflicts": [
                {
                    "path": "org_title",
                    "verdict": "supersede",
                    "old_epistemics": "asserted",
                    "new_epistemics": "inferred",
                }
            ],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert supersede.decision == "rejected"
    assert any(item.code == "inference_overwrites_fact" for item in supersede.issues)


def test_covering_op_without_conflict_retries_once_then_defers():
    payload = {
        "identity": "same",
        "matched_id": "c-old",
        "ops": [
            {
                "op": "update",
                "path": "org_title",
                "covering": True,
                "old": "北窗实验室 研究员",
                "claim": "当前是南港书院讲师",
                "fact": {
                    "claim": "当前是南港书院讲师",
                    "evidence": "后来到南港书院做讲师。",
                },
            }
        ],
        "conflicts": [],
    }
    first = evaluate_integrate(payload, candidates=CANDIDATES, corpus=CORPUS, attempt=1)
    assert first.decision == "retry"
    still_bad = run_merge_gate(payload, candidates=CANDIDATES, corpus=CORPUS, retry_payload=payload)
    assert still_bad.decision == "deferred"
    assert still_bad.retry_used is True
    fixed = {
        **payload,
        "conflicts": [
            {
                "path": "org_title",
                "verdict": "supersede",
                "reason": "当前职发生了变化",
                "old_epistemics": "asserted",
                "new_epistemics": "asserted",
                "evidence_new": "后来到南港书院做讲师。",
            }
        ],
    }
    recovered = run_merge_gate(payload, candidates=CANDIDATES, corpus=CORPUS, retry_payload=fixed)
    assert recovered.decision == "accept"
    assert recovered.retry_used is True
    assert recovered.payload is not None
    assert recovered.payload["conflicts"][0]["verdict"] == "supersede"


def test_narrative_update_is_not_covering_and_accepts_without_conflict():
    """Covering is model-declared only — no path/keyword heuristics."""
    assert is_covering_op({"op": "update", "path": "body_md", "value": "补一段经历"}) is False
    assert is_covering_op({
        "op": "update",
        "path": "facts.0",
        "claim": "2019 年在北窗实验室任职",
        "old": "2019 年在北窗实验室任职。",
    }) is False
    assert is_covering_op({"op": "delete", "path": "facts.1", "claim": "写过一篇讲义"}) is False
    assert is_covering_op({"op": "add", "path": "facts", "claim": "后来到南港书院做讲师"}) is False
    assert is_covering_op({"op": "update", "path": "org_title", "old": "北窗实验室 研究员"}) is False
    assert is_covering_op({"op": "update", "path": "keys.gender", "old": "男"}) is False
    assert is_covering_op({
        "op": "update",
        "path": "facts.0",
        "claim": "至今兼任顾问",
        "old": "北窗实验室 研究员",
    }) is False
    assert is_covering_op({
        "op": "update",
        "path": "facts.0",
        "covering": True,
        "old": "旧句",
    }) is True
    assert is_covering_op({
        "op": "update",
        "path": "org_title",
        "covering": False,
        "old": "北窗实验室 研究员",
    }) is False
    result = evaluate_integrate(
        {
            "identity": "same",
            "matched_id": "c-old",
            "ops": [
                {
                    "op": "update",
                    "path": "body_md",
                    "value": "云岚测试员甲于 2019 年在北窗实验室任职。后来到南港书院做讲师。",
                    "fact": {
                        "claim": "2019 入职",
                        "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
                    },
                }
            ],
            "conflicts": [],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert result.decision == "accept"
    assert not any(item.code == "covering_op_without_conflict" for item in result.issues)


def test_ops_conflicts_contract_is_structural_not_lexical():
    """Claim/reason wording does not link an op to a conflict."""
    covering = {
        "op": "update",
        "path": "org_title",
        "covering": True,
        "old": "北窗实验室 研究员",
        "claim": "当前是南港书院讲师",
        "fact": {
            "claim": "当前是南港书院讲师",
            "evidence": "后来到南港书院做讲师。",
        },
    }
    lexical_only = evaluate_integrate(
        {
            "identity": "same",
            "matched_id": "c-old",
            "ops": [covering],
            "conflicts": [
                {
                    "verdict": "supersede",
                    "reason": "当前是南港书院讲师，所以要改",
                    "old_epistemics": "asserted",
                    "new_epistemics": "asserted",
                }
            ],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert lexical_only.decision == "retry"
    assert any(item.code == "covering_op_without_conflict" for item in lexical_only.issues)

    by_index = evaluate_integrate(
        {
            "identity": "same",
            "matched_id": "c-old",
            "ops": [covering],
            "conflicts": [
                {
                    "op_index": 0,
                    "verdict": "supersede",
                    "old_epistemics": "asserted",
                    "new_epistemics": "asserted",
                    "evidence_new": "后来到南港书院做讲师。",
                }
            ],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert by_index.decision == "accept"
    assert by_index.payload is not None
    assert by_index.payload["conflicts"][0]["verdict"] == "supersede"

    dangling = evaluate_integrate(
        {
            "identity": "same",
            "matched_id": "c-old",
            "ops": [{"op": "keep", "path": "facts.0"}],
            "conflicts": [
                {
                    "op_index": 9,
                    "verdict": "keep",
                    "old_epistemics": "asserted",
                    "new_epistemics": "asserted",
                }
            ],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert dangling.decision != "accept"
    assert any(item.code == "conflict_op_ref_mismatch" for item in dangling.issues)


def test_placeholder_to_real_value_is_not_covering():
    result = evaluate_integrate(
        {
            "identity": "same",
            "matched_id": "c-old",
            "ops": [
                {
                    "op": "update",
                    "path": "org_title",
                    "old": "暂无信息",
                    "claim": "北窗实验室 研究员",
                    "fact": {
                        "claim": "2019 入职",
                        "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
                    },
                }
            ],
            "conflicts": [],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert result.decision == "accept"


def test_maybe_accepts_with_empty_ops():
    result = evaluate_integrate(
        {"identity": "maybe", "ops": [], "conflicts": []},
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert result.decision == "accept"
    assert result.identity == "maybe"


def test_conflict_evidence_must_be_continuous_substring():
    narrative = evaluate_integrate(
        {
            "identity": "same",
            "matched_id": "c-old",
            "ops": [{"op": "keep", "path": "facts.0"}],
            "conflicts": [
                {
                    "path": "facts.0",
                    "verdict": "keep",
                    "evidence": "编造的冲突证据",
                }
            ],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert narrative.decision == "accept"
    assert narrative.payload is not None
    assert narrative.payload["conflicts"] == []
    assert any(item.reason == "conflict_evidence_not_substring" for item in narrative.dropped_facts)


def test_accept_sanitizes_dropped_evidence_from_payload():
    result = evaluate_integrate(
        {
            "identity": "new",
            "ops": [],
            "conflicts": [],
            "facts": [
                {
                    "claim": "2019 入职",
                    "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
                },
                {"claim": "编造", "evidence": "正文里没有这句"},
            ],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert result.decision == "accept"
    assert result.payload is not None
    assert result.payload["facts"] == result.kept_facts
    assert [item["claim"] for item in result.payload["facts"]] == ["2019 入职"]
    assert all(item.get("evidence") != "正文里没有这句" for item in result.payload["facts"])

    with_op = evaluate_integrate(
        {
            "identity": "same",
            "matched_id": "c-old",
            "ops": [
                {
                    "op": "add",
                    "path": "facts",
                    "fact": {"claim": "编造", "evidence": "正文里没有这句"},
                },
                {
                    "op": "add",
                    "path": "facts",
                    "fact": {
                        "claim": "2019 入职",
                        "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
                    },
                },
            ],
            "conflicts": [],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert with_op.decision == "accept"
    assert with_op.payload is not None
    assert len(with_op.payload["ops"]) == 1
    assert with_op.payload["ops"][0]["fact"]["claim"] == "2019 入职"


def test_gate_rejects_unsupported_apply_path():
    result = evaluate_integrate(
        {
            "identity": "same",
            "matched_id": "c-old",
            "ops": [{"op": "update", "path": "chapters.wiki_score", "value": 1}],
            "conflicts": [],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert result.decision == "rejected"
    assert any(item.code == "unsupported_apply_path" for item in result.issues)


def test_gate_accepts_open_key_path_without_field_allowlist():
    result = evaluate_integrate(
        {
            "identity": "same",
            "matched_id": "c-old",
            "ops": [{"op": "update", "path": "keys.foo", "value": "新槽位"}],
            "conflicts": [],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert result.decision == "accept"
    assert not any(item.code == "unsupported_apply_path" for item in result.issues)


def test_supersede_without_good_evidence_new_is_not_applied():
    covering = {
        "op": "update",
        "path": "org_title",
        "covering": True,
        "old": "北窗实验室 研究员",
        "claim": "当前是南港书院讲师",
        "fact": {
            "claim": "当前是南港书院讲师",
            "evidence": "后来到南港书院做讲师。",
        },
    }

    def _run(conflict: dict):
        return evaluate_integrate(
            {
                "identity": "same",
                "matched_id": "c-old",
                "ops": [covering],
                "conflicts": [conflict],
            },
            candidates=CANDIDATES,
            corpus=CORPUS,
        )

    empty = _run(
        {
            "path": "org_title",
            "verdict": "supersede",
            "evidence_new": "",
            "old_epistemics": "asserted",
            "new_epistemics": "asserted",
        }
    )
    missing = _run(
        {
            "path": "org_title",
            "verdict": "supersede",
            "old_epistemics": "asserted",
            "new_epistemics": "asserted",
        }
    )
    not_substring = _run(
        {
            "path": "org_title",
            "verdict": "supersede",
            "evidence_new": "正文里没有这句冲突出处",
            "old_epistemics": "asserted",
            "new_epistemics": "asserted",
        }
    )
    for result in (empty, missing, not_substring):
        assert result.decision == "accept"
        assert result.payload is not None
        assert all(item.get("verdict") != "supersede" for item in result.payload["conflicts"])
        assert all(item.get("verdict") == "uncertain" for item in result.payload["conflicts"])
        assert not any(op.get("covering") is True for op in result.payload["ops"])
        _assert_uncertain_collected_and_new_evidence_cleared(result)

    good = _run(
        {
            "path": "org_title",
            "verdict": "supersede",
            "evidence_new": "后来到南港书院做讲师。",
            "old_epistemics": "asserted",
            "new_epistemics": "asserted",
        }
    )
    assert good.decision == "accept"
    assert good.payload is not None
    assert good.payload["conflicts"][0]["verdict"] == "supersede"
    assert any(op.get("covering") is True for op in good.payload["ops"])


def test_both_with_scope_without_good_evidence_new_is_not_applied():
    covering = {
        "op": "update",
        "path": "org_title",
        "covering": True,
        "old": "北窗实验室 研究员",
        "claim": "当前是南港书院讲师",
        "fact": {
            "claim": "当前是南港书院讲师",
            "evidence": "后来到南港书院做讲师。",
        },
    }

    def _run(conflict: dict):
        return evaluate_integrate(
            {
                "identity": "same",
                "matched_id": "c-old",
                "ops": [covering],
                "conflicts": [conflict],
            },
            candidates=CANDIDATES,
            corpus=CORPUS,
        )

    empty = _run(
        {
            "path": "org_title",
            "verdict": "both_with_scope",
            "evidence_new": "",
            "old_epistemics": "asserted",
            "new_epistemics": "asserted",
        }
    )
    missing = _run(
        {
            "path": "org_title",
            "verdict": "both_with_scope",
            "old_epistemics": "asserted",
            "new_epistemics": "asserted",
        }
    )
    not_substring = _run(
        {
            "path": "org_title",
            "verdict": "both_with_scope",
            "evidence_new": "正文里没有这句冲突出处",
            "old_epistemics": "asserted",
            "new_epistemics": "asserted",
        }
    )
    for result in (empty, missing, not_substring):
        assert result.decision == "accept"
        assert result.payload is not None
        assert all(item.get("verdict") != "both_with_scope" for item in result.payload["conflicts"])
        assert all(item.get("verdict") == "uncertain" for item in result.payload["conflicts"])
        assert not any(op.get("covering") is True for op in result.payload["ops"])
        _assert_uncertain_collected_and_new_evidence_cleared(result)

    good = _run(
        {
            "path": "org_title",
            "verdict": "both_with_scope",
            "evidence_new": "后来到南港书院做讲师。",
            "old_epistemics": "asserted",
            "new_epistemics": "asserted",
        }
    )
    assert good.decision == "accept"
    assert good.payload is not None
    assert good.payload["conflicts"][0]["verdict"] == "both_with_scope"
    assert any(op.get("covering") is True for op in good.payload["ops"])


BAD_NEW_EVIDENCE = "正文里没有这句冲突出处"


def _assert_uncertain_collected_and_new_evidence_cleared(result) -> None:
    unresolved = result.unresolved_conflicts
    assert unresolved, "accept 上的 uncertain 必须进 unresolved_conflicts"
    assert all(item.get("verdict") == "uncertain" for item in unresolved)
    payload_unresolved = (result.payload or {}).get("unresolved_conflicts")
    assert payload_unresolved == unresolved
    for item in [*unresolved, *((result.payload or {}).get("conflicts") or [])]:
        assert item.get("evidence_new") in (None, "", [])
        assert item.get("new_evidence") in (None, "", [])
        assert BAD_NEW_EVIDENCE not in str(item.get("evidence_new") or "")
        assert BAD_NEW_EVIDENCE not in str(item.get("new_evidence") or "")


@pytest.mark.ws_ur
def test_WS_UR_gate_accept_persist_unresolved_uses_gate_payload(tmp_path):
    """WS-UR: evaluate_integrate 的 unresolved 经 persist_unresolved 落到 crystal 列。"""
    assert DISTILLER_STORE_WRITE_ENABLED is True
    con = sqlite3.connect(tmp_path / "gate-ur.sqlite")
    con.row_factory = sqlite3.Row
    root = Path(__file__).resolve().parents[1]
    con.executescript((root / "rogmem" / "schema.sql").read_text(encoding="utf-8"))
    migrate_crystal_store(con)
    con.execute(
        "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,revision) VALUES(?,?,?,'active','{}','{}',0)",
        ("c-old", "人物", "云岚测试员甲"),
    )
    con.commit()
    result = evaluate_integrate(
        {
            "identity": "same",
            "matched_id": "c-old",
            "ops": [
                {
                    "op": "update",
                    "path": "org_title",
                    "covering": True,
                    "old": "北窗实验室 研究员",
                    "claim": "当前是南港书院讲师",
                    "fact": {
                        "claim": "当前是南港书院讲师",
                        "evidence": "后来到南港书院做讲师。",
                    },
                }
            ],
            "conflicts": [
                {
                    "path": "org_title",
                    "verdict": "supersede",
                    "evidence_new": BAD_NEW_EVIDENCE,
                    "old_epistemics": "asserted",
                    "new_epistemics": "asserted",
                }
            ],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert result.decision == "accept"
    loaded = persist_unresolved(CrystalStore(con), "c-old", result)
    assert loaded is not None
    assert loaded.unresolved_conflicts == result.unresolved_conflicts
    raw = json.loads(
        con.execute("SELECT unresolved_conflicts FROM crystallizations WHERE id='c-old'").fetchone()[0]
    )
    assert raw == result.unresolved_conflicts
    assert DISTILLER_STORE_WRITE_ENABLED is True


def test_w_58_02_accept_uncertain_lands_in_unresolved_conflicts():
    """W-58-02: 整单 accept，降为 uncertain 的点必须进 unresolved_conflicts。"""
    result = evaluate_integrate(
        {
            "identity": "same",
            "matched_id": "c-old",
            "ops": [
                {
                    "op": "update",
                    "path": "org_title",
                    "covering": True,
                    "old": "北窗实验室 研究员",
                    "claim": "当前是南港书院讲师",
                    "fact": {
                        "claim": "当前是南港书院讲师",
                        "evidence": "后来到南港书院做讲师。",
                    },
                }
            ],
            "conflicts": [
                {
                    "path": "org_title",
                    "verdict": "supersede",
                    "evidence_new": BAD_NEW_EVIDENCE,
                    "old_epistemics": "asserted",
                    "new_epistemics": "asserted",
                }
            ],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert result.decision == "accept"
    assert result.identity == "same"
    assert result.matched_id == "c-old"
    assert {item.get("path") for item in result.unresolved_conflicts} == {"org_title"}
    assert all(item.get("verdict") == "uncertain" for item in result.unresolved_conflicts)
    assert result.payload is not None
    assert result.payload["unresolved_conflicts"] == result.unresolved_conflicts
    assert not any(op.get("covering") is True for op in result.payload["ops"])


def test_w_58_03_downgrade_clears_bad_evidence_new():
    """W-58-03: covering 降 uncertain 时清掉坏/空 evidence_new，不得当合格出处。"""
    covering = {
        "op": "update",
        "path": "org_title",
        "covering": True,
        "old": "北窗实验室 研究员",
        "claim": "当前是南港书院讲师",
        "fact": {
            "claim": "当前是南港书院讲师",
            "evidence": "后来到南港书院做讲师。",
        },
    }

    def _run(conflict: dict):
        return evaluate_integrate(
            {
                "identity": "same",
                "matched_id": "c-old",
                "ops": [covering],
                "conflicts": [conflict],
            },
            candidates=CANDIDATES,
            corpus=CORPUS,
        )

    dirty = _run(
        {
            "path": "org_title",
            "verdict": "supersede",
            "evidence_new": BAD_NEW_EVIDENCE,
            "new_evidence": BAD_NEW_EVIDENCE,
            "old_epistemics": "asserted",
            "new_epistemics": "asserted",
        }
    )
    both_scope = _run(
        {
            "path": "org_title",
            "verdict": "both_with_scope",
            "evidence_new": BAD_NEW_EVIDENCE,
            "old_epistemics": "asserted",
            "new_epistemics": "asserted",
        }
    )
    empty = _run(
        {
            "path": "org_title",
            "verdict": "supersede",
            "evidence_new": "",
            "old_epistemics": "asserted",
            "new_epistemics": "asserted",
        }
    )
    for result in (dirty, both_scope, empty):
        assert result.decision == "accept"
        _assert_uncertain_collected_and_new_evidence_cleared(result)
        dumped = json.dumps(result.payload, ensure_ascii=False)
        assert BAD_NEW_EVIDENCE not in dumped


def test_w_58_04_model_uncertain_is_collected_without_deferring():
    """W-58-04: 模型自报 uncertain 也进 unresolved；整单仍 accept，不整条 deferred。"""
    result = evaluate_integrate(
        {
            "identity": "same",
            "matched_id": "c-old",
            "ops": [
                {
                    "op": "update",
                    "path": "body_md",
                    "value": "云岚测试员甲于 2019 年在北窗实验室任职。后来到南港书院做讲师。",
                    "fact": {
                        "claim": "2019 入职",
                        "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
                    },
                }
            ],
            "conflicts": [
                {
                    "path": "org_title",
                    "verdict": "uncertain",
                    "reason": "两处说法暂时无法裁断",
                }
            ],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert result.decision == "accept"
    assert result.identity == "same"
    assert len(result.unresolved_conflicts) == 1
    assert result.unresolved_conflicts[0]["path"] == "org_title"
    assert result.unresolved_conflicts[0]["verdict"] == "uncertain"
    assert result.payload is not None
    assert result.payload["unresolved_conflicts"] == result.unresolved_conflicts
    assert result.payload["ops"], "无冲突的正文 op 仍可过闸"
    good_supersede = evaluate_integrate(
        {
            "identity": "same",
            "matched_id": "c-old",
            "ops": [{"op": "keep", "path": "facts.0"}],
            "conflicts": [
                {
                    "path": "org_title",
                    "verdict": "supersede",
                    "evidence_new": "后来到南港书院做讲师。",
                    "old_epistemics": "asserted",
                    "new_epistemics": "asserted",
                }
            ],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert good_supersede.decision == "accept"
    assert good_supersede.unresolved_conflicts == []
    assert good_supersede.payload is not None
    assert good_supersede.payload["unresolved_conflicts"] == []
    assert good_supersede.payload["conflicts"][0]["evidence_new"] == "后来到南港书院做讲师。"


def test_empty_conflicts_still_accepts_when_otherwise_valid():
    result = evaluate_integrate(
        {
            "identity": "same",
            "matched_id": "c-old",
            "ops": [
                {
                    "op": "update",
                    "path": "body_md",
                    "value": "云岚测试员甲于 2019 年在北窗实验室任职。后来到南港书院做讲师。",
                    "fact": {
                        "claim": "2019 入职",
                        "evidence": "云岚测试员甲于 2019 年在北窗实验室任职。",
                    },
                }
            ],
            "conflicts": [],
        },
        candidates=CANDIDATES,
        corpus=CORPUS,
    )
    assert result.decision == "accept"
    assert result.payload is not None
    assert result.payload["conflicts"] == []
    assert len(result.payload["ops"]) == 1

