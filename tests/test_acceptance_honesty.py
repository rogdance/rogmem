"""Acceptance-layer honesty: contract IDs must not claim delivery or vendor LLM."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "tests" / "live_check.py"
FEATURES = ROOT / "docs" / "superpowers" / "specs" / "2026-10-24-rogmem-features.md"
LAYERS = ROOT / "docs" / "superpowers" / "specs" / "2026-10-25-rogmem-acceptance-layers.md"
BASELINE = ROOT / "tests" / "eval_baseline.json"
TRIVIAL_PERFECT = {"召回": 1, "先后": 1, "更新": 1, "矛盾": 1}


def _live_text() -> str:
    return LIVE.read_text(encoding="utf-8")


def _check_ids(text: str) -> list[str]:
    ids = []
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id != "check":
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            ids.append(node.args[0].value)
    return ids


def test_live_check_source_has_no_f35_contract_pass_id():
    text = _live_text()
    assert 'check("F35"' not in text
    assert 'check("F37"' not in text
    assert 'check("F39"' not in text
    assert not re.search(r'check\(\s*["\']F(?:35|37|39)["\']', text)
    ids = _check_ids(text)
    assert "F35" not in ids
    assert "F37" not in ids
    assert "F39" not in ids
    assert "C-F35-history" in ids
    assert "C-F37-sweep" in ids
    assert any(item.startswith("C-F39-") for item in ids)


def test_f108_contract_id_is_not_claiming_vendor():
    text = _live_text()
    ids = _check_ids(text)
    assert "F108" not in ids
    assert 'check("F108"' not in text
    assert any(item.startswith("C-F108") for item in ids)
    assert "C-F108-cite" in ids
    cite_at = text.find('check("C-F108-cite"')
    window = text[max(0, cite_at - 200) : cite_at + 240]
    assert "MiniMax" not in window
    assert "极空间" not in window


def test_i_f108_skip_is_not_pass_without_live_flag():
    text = _live_text()
    ids = _check_ids(text)
    assert "I-F108" in ids
    assert "ROGMEM_LIVE_LLM" in text
    assert re.search(r'SKIP I-F108|skip\(\s*["\']I-F108["\']', text)
    gated = False
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id != "check":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant) or node.args[0].value != "I-F108":
            continue
        parent = node
        for ancestor in ast.walk(tree):
            if not isinstance(ancestor, (ast.If, ast.FunctionDef)):
                continue
            if any(child is parent or parent in ast.walk(child) for child in ast.iter_child_nodes(ancestor)):
                src = ast.get_source_segment(text, ancestor) or ""
                if "ROGMEM_LIVE_LLM" in src:
                    gated = True
        if gated:
            break
    assert gated


def test_f113_keeps_delivery_rows_unpassed():
    text = _live_text()
    assert "F113" in _check_ids(text)
    features = FEATURES.read_text(encoding="utf-8")
    for fid in ("F35", "F39"):
        row = next(line for line in features.splitlines() if line.startswith(f"| {fid} |"))
        assert "未通过" in row
        assert not re.search(r"\|\s*通过\s*\|", row) and not row.rstrip().endswith("| 通过")
    sweep = next(line for line in features.splitlines() if line.startswith("| F37 |"))
    assert "通过" in sweep
    assert "到点触发" in sweep
    assert "未通过" not in sweep


def test_acceptance_layers_doc_defines_l0_l1_l2():
    text = LAYERS.read_text(encoding="utf-8")
    assert "L0" in text and "L1" in text and "L2" in text
    assert "C-F35-history" in text
    assert "I-F108" in text
    assert "F35" in text and "未通过" in text


def test_features_notes_layers_and_f108_honesty():
    text = FEATURES.read_text(encoding="utf-8")
    assert "L0" in text and "L1" in text and "L2" in text
    assert "C-F108-cite" in text or "I-F108" in text
    row = next(line for line in text.splitlines() if "| F108 |" in line)
    assert "通过" in row
    assert "未通过" not in row
    assert "我在周生生任职，做金饰零售。" in row
    assert "skip" in row
    assert "极空间真实模型" not in row or "不得" in row


def test_baseline_not_floor_equals_perfect():
    from tests.eval_set import FLOOR, ITEMS, SCALE_MAX, judge

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert FLOOR["召回"] >= 1
    assert SCALE_MAX >= 2
    assert FLOOR != TRIVIAL_PERFECT
    assert not (SCALE_MAX == 1 and all(value == 1 for value in FLOOR.values()))
    for dim, cases in ITEMS.items():
        assert len(cases) >= 2, dim
        assert int(baseline[dim]) >= FLOOR[dim]
        assert int(baseline[dim]) <= SCALE_MAX
    judge({"召回": 2, "先后": 2, "更新": 2, "矛盾": 2}, baseline)
    try:
        judge({"召回": 0, "先后": 2, "更新": 2, "矛盾": 2}, baseline)
    except AssertionError as exc:
        assert "below baseline" in str(exc)
    else:
        raise AssertionError("judge must fail when scores drop below baseline")
    try:
        judge(baseline, {"召回": 0, "先后": 0, "更新": 0, "矛盾": 0})
    except AssertionError as exc:
        assert "FLOOR" in str(exc)
    else:
        raise AssertionError("judge must fail when baseline is below FLOOR")


def test_eval_gate_scores_meet_baseline():
    from tests.eval_set import load_baseline, score_all, judge

    scores = score_all()
    judge(scores, load_baseline())
