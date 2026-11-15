"""Release manifest generator — pure build without touching gh or the network.

The ``build_manifest`` function is the dry-run heart of
``scripts/generate_releases_manifest.py``: its output is exactly the
JSON the operator would publish. We check round-trip, filtering, and
that no credential or secret-looking string is ever written.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.generate_releases_manifest import build_manifest  # noqa: E402
from rogmem.release_manifest import parse_manifest  # noqa: E402


def _entries():
    return [
        {"tag": "V1-2611121012-PUB", "released_at": "2026-11-12T02:14:10Z"},
        {"tag": "V1-2611120929-PUB", "released_at": "2026-11-12T01:31:22Z"},
        {"tag": "V1-2611120856-PRE", "released_at": "2026-11-12T00:58:05Z"},
        {"tag": "random-tag", "released_at": "2026-11-12T00:00:00Z"},
    ]


def test_build_manifest_emits_schema_v1_minimum_fields_only():
    manifest = build_manifest(_entries())
    assert manifest["schema"] == "rogmem-releases/1"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", manifest["generated_at"])
    assert set(manifest["channels"]["stable"].keys()) == {"latest", "released_at"}
    assert manifest["channels"]["stable"]["latest"] == "V1-2611121012-PUB"
    assert "prerelease" not in manifest["channels"]
    assert all(
        set(entry.keys()) == {"tag", "channel", "released_at"}
        for entry in manifest["releases"]
    )


def test_prerelease_only_present_when_opted_in():
    manifest = build_manifest(_entries(), include_prerelease=True)
    assert manifest["channels"]["prerelease"]["latest"] == "V1-2611120856-PRE"
    tags = [e["tag"] for e in manifest["releases"]]
    assert "V1-2611120856-PRE" in tags


def test_generator_output_round_trips_through_the_reader_parser():
    manifest = build_manifest(_entries(), include_prerelease=True)
    parsed = parse_manifest(json.dumps(manifest).encode("utf-8"))
    assert parsed.stable_latest == "V1-2611121012-PUB"
    assert parsed.prerelease_latest == "V1-2611120856-PRE"


def test_generator_refuses_empty_or_all_non_matching_input():
    with pytest.raises(RuntimeError):
        build_manifest([{"tag": "bad", "released_at": "2026-11-12T00:00:00Z"}])
    with pytest.raises(RuntimeError):
        build_manifest([])


def test_generator_output_does_not_contain_any_secret_shape():
    manifest = build_manifest(_entries(), include_prerelease=True)
    text = json.dumps(manifest)
    for needle in ("ghp_", "github_pat_", "Authorization", "Bearer ", "x-access-token"):
        assert needle not in text


def test_times_are_normalized_to_utc_z():
    local = [
        {"tag": "V1-2611121012-PUB", "released_at": "2026-11-12T10:14:10+08:00"},
    ]
    manifest = build_manifest(local)
    assert manifest["channels"]["stable"]["released_at"] == "2026-11-12T02:14:10Z"


def test_truncation_never_drops_the_latest_tag():
    """Reviewer blocker: truncating to 500 entries must keep latest."""
    # Build >500 older entries plus one newest entry.
    older = [
        {
            "tag": f"V1-26{str(i).zfill(8)}-PUB",
            "released_at": f"2026-11-{(i % 30) + 1:02d}T00:00:00Z",
        }
        for i in range(600)
    ]
    newest = {"tag": "V1-2699999999-PUB", "released_at": "2099-12-31T23:59:59Z"}
    manifest = build_manifest([newest] + older)
    assert len(manifest["releases"]) <= 500
    tags = [entry["tag"] for entry in manifest["releases"]]
    assert "V1-2699999999-PUB" in tags, "截断必须保留 latest 条目，但丢了"
    assert manifest["channels"]["stable"]["latest"] == "V1-2699999999-PUB"
    # Self-check: round-trip must pass.
    parse_manifest(json.dumps(manifest).encode("utf-8"))
