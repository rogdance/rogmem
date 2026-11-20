"""Extract a temporal window from a search query.

Wraps the Hindsight-ported rule engines in ``temporal_periods`` /
``chinese_temporal_periods``. Adds season phrases (春夏秋冬) and bare
``YYYY-MM`` that Hindsight left to dateparser — RogMem has no dateparser
dependency, so those gaps are closed here with the same window semantics.

Fails soft: a parser error or no match returns ``None`` (no temporal arm),
matching Hindsight ``extract_temporal_constraint``.
"""

from __future__ import annotations

import calendar
import logging
import re
import unicodedata
from datetime import datetime, timezone

from rogmem.temporal_periods import NO_TEMPORAL_CONSTRAINT, extract_period

logger = logging.getLogger(__name__)

DateRange = tuple[datetime, datetime]

_SEASON_MONTHS = {
    "春": (1, 3),
    "春天": (1, 3),
    "春季": (1, 3),
    "夏": (4, 6),
    "夏天": (4, 6),
    "夏季": (4, 6),
    "秋": (7, 9),
    "秋天": (7, 9),
    "秋季": (7, 9),
    "冬": (10, 12),
    "冬天": (10, 12),
    "冬季": (10, 12),
}

_YEAR_PREFIX = {
    "今年": 0,
    "本年": 0,
    "去年": -1,
    "上年": -1,
    "前年": -2,
    "明年": 1,
}

_SEASON_RE = re.compile(
    r"(?P<year_word>今年|本年|去年|上年|前年|明年)?"
    r"(?:(?P<year_num>\d{4})\s*年)?"
    r"(?P<season>春天|春季|夏天|夏季|秋天|秋季|冬天|冬季|春|夏|秋|冬)"
)

_ISO_MONTH_RE = re.compile(r"(?<!\d)(?P<year>\d{4})-(?P<month>0?[1-9]|1[0-2])(?!\d)")


def _aware(dt: datetime, reference: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=reference.tzinfo or timezone.utc)
    return dt


def _month_end(year: int, month: int) -> datetime:
    return datetime(year, month, calendar.monthrange(year, month)[1])


def _constraint(start: datetime, end: datetime, reference: datetime) -> DateRange:
    start = _aware(start.replace(hour=0, minute=0, second=0, microsecond=0), reference)
    end = _aware(end.replace(hour=23, minute=59, second=59, microsecond=999999), reference)
    return start, end


def _extract_season(query: str, reference: datetime) -> DateRange | None:
    match = _SEASON_RE.search(query)
    if not match:
        return None
    season = match.group("season")
    months = _SEASON_MONTHS.get(season)
    if not months:
        return None
    year = reference.year
    if match.group("year_num"):
        year = int(match.group("year_num"))
    elif match.group("year_word"):
        year = reference.year + _YEAR_PREFIX[match.group("year_word")]
    start_m, end_m = months
    return _constraint(datetime(year, start_m, 1), _month_end(year, end_m), reference)


def _extract_iso_month(query: str, reference: datetime) -> DateRange | None:
    match = _ISO_MONTH_RE.search(query)
    if not match:
        return None
    year = int(match.group("year"))
    month = int(match.group("month"))
    if not (reference.year - 120 <= year <= reference.year + 20):
        return None
    return _constraint(datetime(year, month, 1), _month_end(year, month), reference)


def extract_temporal_window(
    query: str,
    reference_date: datetime | None = None,
) -> DateRange | None:
    """Return ``(start, end)`` when the query names a time window, else ``None``.

    Never raises into the recall path — a bad parse degrades to no temporal arm.
    """
    text = unicodedata.normalize("NFKC", str(query or "")).strip()
    if not text:
        return None
    reference = reference_date or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    try:
        season = _extract_season(text, reference)
        if season is not None:
            return season
        iso_month = _extract_iso_month(text, reference)
        if iso_month is not None:
            return iso_month
        result = extract_period(text, reference)
    except Exception as exc:  # noqa: BLE001 — recall must not fail on parse
        logger.warning(
            "Temporal query analysis raised %s (treating as no temporal constraint): %s",
            type(exc).__name__,
            exc,
        )
        return None
    if result is None or result is NO_TEMPORAL_CONSTRAINT:
        return None
    start, end = result
    return _aware(start, reference), _aware(end, reference)


def strip_temporal_phrases(query: str) -> str:
    """Drop recognised temporal tokens so residual text can score relevance."""
    text = unicodedata.normalize("NFKC", str(query or ""))
    text = _SEASON_RE.sub(" ", text)
    text = _ISO_MONTH_RE.sub(" ", text)
    # Common relative phrases already handled by the rule engines.
    for phrase in (
        "大前年",
        "大后年",
        "上上周",
        "下下周",
        "上上个月",
        "下下个月",
        "上个月",
        "下个月",
        "上一年",
        "下一年",
        "上一年度",
        "下一年度",
        "上半年",
        "下半年",
        "第一季度",
        "第二季度",
        "第三季度",
        "第四季度",
        "去年",
        "前年",
        "明年",
        "后年",
        "今年",
        "本年",
        "上周",
        "本周",
        "这周",
        "下周",
        "上月",
        "本月",
        "这个月",
        "下月",
        "昨天",
        "今天",
        "明天",
        "前天",
        "后天",
        "last week",
        "last month",
        "last year",
        "yesterday",
        "today",
        "tomorrow",
    ):
        text = re.sub(re.escape(phrase), " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\d{4}\s*年\s*\d{1,2}\s*月(?:\s*到\s*\d{1,2}\s*月)?", " ", text)
    text = re.sub(r"\d{4}\s*年", " ", text)
    return re.sub(r"\s+", " ", text).strip()
