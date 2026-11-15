"""Release parser: tag format, comparator, and about-page view invariants.

A failure must never display as '已是最新' or near-'OK'. A historical
successful tag must survive subsequent failures, and the current
failure must be surfaced as a secondary detail — not as the primary
text.
"""

from __future__ import annotations

import pytest

from rogmem.release_parser import (
    LOCAL_TAG,
    RELEASE_TAG,
    error_text,
    present,
    relation,
    select_latest_from_tags,
)


# ---- regex ---------------------------------------------------------------


@pytest.mark.parametrize(
    "tag,should_match",
    [
        ("V1-2611121012-PUB", True),
        ("V2-2601010000-PUB", True),
        ("V10-2611121012-PUB", True),
        ("V1-2611121012-PRE", False),
        ("V1-2611121012", False),
        ("V1-未打包", False),
        ("v1-2611121012-PUB", False),
        ("V0-2611121012-PUB", False),
        ("V1-123-PUB", False),
        ("", False),
    ],
)
def test_release_tag_regex(tag, should_match):
    assert bool(RELEASE_TAG.fullmatch(tag)) is should_match


@pytest.mark.parametrize(
    "tag,should_match",
    [
        ("V1-2611121012-PUB", True),
        ("V1-2611121012-PRE", True),
        ("V1-2611121012", True),
        ("V1-未打包", False),
        ("V0-2611121012", False),
        ("V1-2611121012-DEV", True),
    ],
)
def test_local_tag_regex(tag, should_match):
    assert bool(LOCAL_TAG.fullmatch(tag)) is should_match


# ---- select_latest_from_tags --------------------------------------------


def test_select_latest_picks_highest_pub_tag():
    assert select_latest_from_tags(
        [
            "V1-2610010000-PUB",
            "V1-2611081002-PUB",
            "V2-2601010000-PUB",
            "V1-2611081002-PRE",
            "v1.0.0",
        ]
    ) == "V2-2601010000-PUB"


def test_select_latest_returns_empty_when_nothing_qualifies():
    assert select_latest_from_tags([]) == ""
    assert select_latest_from_tags(["bad", "V1-2611081002-PRE", ""]) == ""


# ---- relation -----------------------------------------------------------


def test_relation_identical_build_is_latest():
    assert relation("V1-2611081002-PUB", "V1-2611081002-PUB") == "已是最新"
    assert relation("V1-2611081002-PRE", "V1-2611081002-PUB") == "已是最新"
    assert relation("V1-2611081002-DEV", "V1-2611081002-PUB") == "已是最新"
    assert relation("V1-2611081002", "V1-2611081002-PUB") == "已是最新"


def test_relation_newer_remote_is_update_available():
    assert relation("V1-2610010000-PUB", "V1-2611081002-PUB") == "有新版本"


def test_relation_newer_local_is_local_ahead():
    assert relation("V1-2611990000-PRE", "V1-2611081002-PUB") == "本地较新"
    assert relation("V2-2601010000-PUB", "V1-2611081002-PUB") == "本地较新"


def test_relation_unparseable_sides_yield_empty():
    assert relation("V1-未打包", "V1-2611081002-PUB") == ""
    assert relation("V1-2611081002-PUB", "") == ""
    assert relation("", "") == ""


# ---- present ------------------------------------------------------------


def test_present_never_checked_defaults_to_unchecked():
    view = present("V1-未打包")
    assert view["display"] == "未核对"
    assert view["relation"] == ""
    assert view["error_kind"] == "never_checked"
    assert view["latest"] == ""


def test_present_config_missing_is_distinct_from_failure():
    view = present("V1-未打包", error_kind="config_missing")
    assert view["display"] == "尚未配置发布核对源"
    assert view["relation"] == ""
    assert view["latest"] == ""
    assert view["error_kind"] == "config_missing"


def test_present_config_invalid_is_distinct_from_failure():
    view = present("V1-未打包", error_kind="config_invalid")
    assert view["display"] == "发布核对源地址非法"
    assert view["error_kind"] == "config_invalid"


def test_present_first_failure_without_history_shows_request_failed():
    view = present(
        "V1-未打包",
        error_kind="not_found",
        last_fail_at="2026-11-12T04:30:00+00:00",
    )
    assert view["display"].startswith("请求失败，")
    assert "核对源不存在" in view["display"]
    assert view["relation"] == ""
    assert view["latest"] == ""
    assert view["error_kind"] == "not_found"


def test_present_failure_after_success_never_calls_cached_tag_current_latest():
    """Reviewer blocker: a failing current probe MUST NOT claim '已是最新'.

    The historical tag can be preserved and shown, but it must be
    marked as historical (freshness != fresh), and the UI-facing
    relation that implies 'live' latest must be empty.
    """
    view = present(
        "V1-2611081002-PUB",
        latest="V1-2611081002-PUB",
        last_success_at="2026-11-12T04:30:00+00:00",
        last_fail_at="2026-11-12T05:00:00+00:00",
        error_kind="timeout",
    )
    assert view["freshness"] == "stale", f"失败后 freshness 必须是 stale：{view!r}"
    assert view["relation"] == "", "失败态不得宣称'已是最新/有新版本/本地较新'"
    assert "已是最新" not in view["display"], f"失败态主文案不得出现'已是最新'：{view['display']!r}"
    assert "V1-2611081002-PUB" in view["display"], "历史成功 tag 仍须可见"
    assert view["last_success_tag"] == "V1-2611081002-PUB"
    assert view["fail_detail"] == "核对超时"
    assert view["last_fail_at"] == "2026-11-12T05:00:00+00:00"
    assert view["error_kind"] == "timeout"


def test_present_fresh_success_exposes_live_relation_and_freshness():
    view = present(
        "V1-2610010000-PUB",
        latest="V1-2611081002-PUB",
        last_success_at="2026-11-12T04:30:00+00:00",
        error_kind="ok",
    )
    assert view["freshness"] == "fresh"
    assert view["relation"] == "有新版本"
    assert view["display"] == "V1-2611081002-PUB"
    assert view["fail_detail"] == ""


def test_present_not_modified_keeps_freshness_fresh_without_fail_detail():
    view = present(
        "V1-2611081002-PUB",
        latest="V1-2611081002-PUB",
        last_success_at="2026-11-12T05:00:00+00:00",
        error_kind="not_modified",
    )
    assert view["freshness"] == "fresh"
    assert view["relation"] == "已是最新"
    assert view["fail_detail"] == ""


def test_present_never_displays_latest_when_only_error_present():
    for kind in ("not_found", "timeout", "network", "rate_limited", "server_error",
                 "parse", "schema", "too_large", "unknown"):
        view = present("V1-未打包", error_kind=kind, last_fail_at="2026-11-12T05:00:00+00:00")
        assert view["display"] != "已是最新"
        assert "已是最新" not in view["display"]
        assert view["relation"] == ""
        assert view["latest"] == ""
        assert view["freshness"] == "none"


def test_error_text_lookup_table():
    assert error_text("not_found") == "核对源不存在"
    assert error_text("timeout") == "核对超时"
    assert error_text("ok") == ""
    assert error_text("", fallback="x") == "x"
