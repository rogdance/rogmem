"""The retained core-memory benchmark. Failures name the capability, not a hidden fixture."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.rogmem_core.run import execute


def test_core_benchmark_covers_the_opening_and_the_change(tmp_path):
    report = execute(tmp_path)
    assert report["count"] >= 100
    failed = [item for item in report["cases"] if not item["ok"]]
    assert failed == []
