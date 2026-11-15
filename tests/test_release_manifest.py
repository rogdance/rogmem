"""Release metadata manifest schema v1 — strict parsing contract.

This is the shared release contract between the publisher side (release
skill) and the consumer side (about page probe). Non-conforming inputs
must raise ManifestError with a meaningful `kind`, never silently drop
or accept.
"""

from __future__ import annotations

import json

import pytest

from rogmem.release_manifest import (
    MAX_RESPONSE_BYTES,
    Manifest,
    ManifestError,
    parse_manifest,
)


def _sample() -> dict:
    return {
        "schema": "rogmem-releases/1",
        "generated_at": "2026-11-12T04:30:00Z",
        "channels": {
            "stable": {
                "latest": "V1-2611121012-PUB",
                "released_at": "2026-11-12T02:14:10Z",
            }
        },
        "releases": [
            {
                "tag": "V1-2611121012-PUB",
                "channel": "stable",
                "released_at": "2026-11-12T02:14:10Z",
            },
            {
                "tag": "V1-2611120929-PUB",
                "channel": "stable",
                "released_at": "2026-11-12T01:31:22Z",
            },
        ],
    }


def _bytes(obj: dict) -> bytes:
    return json.dumps(obj).encode("utf-8")


def test_minimal_valid_manifest_parses():
    manifest = parse_manifest(_bytes(_sample()))
    assert isinstance(manifest, Manifest)
    assert manifest.schema == "rogmem-releases/1"
    assert manifest.generated_at == "2026-11-12T04:30:00Z"
    assert manifest.stable_latest == "V1-2611121012-PUB"
    assert manifest.stable_released_at == "2026-11-12T02:14:10Z"
    assert [r.tag for r in manifest.releases] == [
        "V1-2611121012-PUB",
        "V1-2611120929-PUB",
    ]


def test_unknown_schema_is_rejected_with_kind_schema():
    bad = _sample() | {"schema": "rogmem-releases/999"}
    with pytest.raises(ManifestError) as info:
        parse_manifest(_bytes(bad))
    assert info.value.kind == "schema"


def test_missing_schema_is_rejected():
    bad = {k: v for k, v in _sample().items() if k != "schema"}
    with pytest.raises(ManifestError) as info:
        parse_manifest(_bytes(bad))
    assert info.value.kind == "schema"


def test_missing_channels_stable_latest_is_rejected():
    bad = _sample()
    del bad["channels"]["stable"]["latest"]
    with pytest.raises(ManifestError) as info:
        parse_manifest(_bytes(bad))
    assert info.value.kind == "schema"


def test_missing_channels_is_rejected():
    bad = {k: v for k, v in _sample().items() if k != "channels"}
    with pytest.raises(ManifestError) as info:
        parse_manifest(_bytes(bad))
    assert info.value.kind == "schema"


def test_releases_must_be_present_and_non_empty():
    for bad_releases in (None, [], "not a list", {}):
        bad = _sample() | {"releases": bad_releases}
        with pytest.raises(ManifestError) as info:
            parse_manifest(_bytes(bad))
        assert info.value.kind == "schema"


def test_releases_list_longer_than_500_is_rejected():
    bad = _sample()
    bad["releases"] = [
        {"tag": "V1-2611120000-PUB", "channel": "stable", "released_at": "2026-11-12T00:00:00Z"}
    ] * 501
    with pytest.raises(ManifestError) as info:
        parse_manifest(_bytes(bad))
    assert info.value.kind == "schema"


def test_generated_at_must_be_utc_z_suffix():
    for bad_time in ("2026-11-12T04:30:00+08:00", "2026-11-12 04:30:00", "not-a-time", ""):
        bad = _sample() | {"generated_at": bad_time}
        with pytest.raises(ManifestError) as info:
            parse_manifest(_bytes(bad))
        assert info.value.kind == "schema"


def test_generated_at_rejects_nonexistent_calendar_dates():
    """Reviewer blocker: a regex-only check lets '2026-13-40T99:99:99Z' through."""
    for bad_time in (
        "2026-13-40T99:99:99Z",
        "2026-02-30T00:00:00Z",
        "0000-01-01T00:00:00Z",
        "2026-11-12T24:00:00Z",
    ):
        bad = _sample() | {"generated_at": bad_time}
        with pytest.raises(ManifestError) as info:
            parse_manifest(_bytes(bad))
        assert info.value.kind == "schema"


def test_stable_latest_must_be_present_in_releases_and_be_the_highest_pub():
    """Producer and reader must agree on what 'latest' means."""
    disagreeing = _sample()
    disagreeing["channels"]["stable"]["latest"] = "V1-9999999999-PUB"
    with pytest.raises(ManifestError) as info:
        parse_manifest(_bytes(disagreeing))
    assert info.value.kind == "schema"

    not_highest = _sample()
    not_highest["channels"]["stable"]["latest"] = "V1-2611120929-PUB"
    with pytest.raises(ManifestError) as info:
        parse_manifest(_bytes(not_highest))
    assert info.value.kind == "schema"


def test_latest_tag_must_match_release_pattern():
    for bad_tag in ("v1.0.0", "V1-123-PUB", "V1-2611121012-DEV", "", "V1-2611121012-PRE"):
        bad = _sample()
        bad["channels"]["stable"]["latest"] = bad_tag
        with pytest.raises(ManifestError) as info:
            parse_manifest(_bytes(bad))
        assert info.value.kind == "schema"


def test_release_entry_tag_may_be_pre_or_pub_but_not_other():
    good = _sample()
    good["releases"].append(
        {"tag": "V1-2611120856-PRE", "channel": "prerelease", "released_at": "2026-11-12T00:58:05Z"}
    )
    manifest = parse_manifest(_bytes(good))
    assert any(r.tag == "V1-2611120856-PRE" for r in manifest.releases)

    bad = _sample()
    bad["releases"].append(
        {"tag": "unparsable", "channel": "stable", "released_at": "2026-11-12T00:00:00Z"}
    )
    with pytest.raises(ManifestError) as info:
        parse_manifest(_bytes(bad))
    assert info.value.kind == "schema"


def test_release_entry_requires_tag_channel_and_time():
    for drop in ("tag", "channel", "released_at"):
        bad = _sample()
        del bad["releases"][0][drop]
        with pytest.raises(ManifestError) as info:
            parse_manifest(_bytes(bad))
        assert info.value.kind == "schema"


def test_unknown_optional_fields_are_ignored_not_errors():
    good = _sample()
    good["extra_top_level"] = {"anything": 1}
    good["releases"][0]["notes"] = "ignored"
    good["channels"]["stable"]["extra"] = True
    manifest = parse_manifest(_bytes(good))
    assert manifest.stable_latest == "V1-2611121012-PUB"


def test_oversize_bytes_are_rejected_as_too_large():
    payload = _bytes(_sample())
    padded = payload + b" " * (MAX_RESPONSE_BYTES + 1)
    with pytest.raises(ManifestError) as info:
        parse_manifest(padded)
    assert info.value.kind == "too_large"


def test_non_json_bytes_are_rejected_as_parse():
    for junk in (b"<html>", b"", b"not json {", b"[1, 2, 3"):
        with pytest.raises(ManifestError) as info:
            parse_manifest(junk)
        assert info.value.kind == "parse"


def test_top_level_must_be_object():
    for junk in (b"[]", b'"string"', b"42", b"null"):
        with pytest.raises(ManifestError) as info:
            parse_manifest(junk)
        assert info.value.kind == "schema"


def test_manifest_error_str_is_non_empty_and_safe():
    error = ManifestError("schema", "channels.stable.latest 字段非法")
    assert error.kind == "schema"
    assert "字段" in str(error)
    assert "token" not in str(error).lower()


def test_prerelease_channel_is_optional_and_tolerated():
    good = _sample()
    good["channels"]["prerelease"] = {
        "latest": "V1-2611120856-PRE",
        "released_at": "2026-11-12T00:58:05Z",
    }
    good["releases"].append(
        {
            "tag": "V1-2611120856-PRE",
            "channel": "prerelease",
            "released_at": "2026-11-12T00:58:05Z",
        }
    )
    manifest = parse_manifest(_bytes(good))
    assert manifest.stable_latest == "V1-2611121012-PUB"
    assert manifest.prerelease_latest == "V1-2611120856-PRE"
