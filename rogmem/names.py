"""Name correspondence: case, separators, pinyin, and a single typo."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_SEPARATORS = re.compile(r"[\s_\-·.．,，、/]+")

# A name, a label and one window of a body are all short. A whole body is not, and
# caching those would hold megabytes for a value that is read once. Long text skips
# the cache and pays the parse, which is what it did before there was one.
_CACHE_CHARS = 64


def _fold(text: str) -> str:
    return _SEPARATORS.sub("", unicodedata.normalize("NFKC", text).casefold())


@lru_cache(maxsize=65536)
def _cached_key(text: str) -> str:
    return _fold(text)


def name_key(name: str) -> str:
    text = name or ""
    if len(text) <= _CACHE_CHARS:
        return _cached_key(text)
    return _fold(text)


@lru_cache(maxsize=65536)
def syllable(char: str) -> str:
    """The spoken form of one character, or "" when it has none.

    A body is checked against every label it might carry, so the same character is
    transliterated again and again. One character is a small alphabet: cache it and the
    per-label cost stops depending on how long the body is.
    """
    from pypinyin import Style, lazy_pinyin

    if not char:
        return ""
    parts = lazy_pinyin(char, style=Style.NORMAL, errors="ignore")
    return _fold("".join(parts))


@lru_cache(maxsize=65536)
def _cached_pinyin(text: str) -> str:
    key = _cached_key(text)
    if len(key) < 2:
        return ""
    from pypinyin import Style, lazy_pinyin

    return _fold("".join(lazy_pinyin(key, style=Style.NORMAL, errors="ignore")))


def spoken_text(text: str) -> str:
    """The spoken form of a string, cached when it is short."""
    if len(text) <= _CACHE_CHARS:
        return _cached_pinyin(text)
    from pypinyin import Style, lazy_pinyin

    key = name_key(text)
    if len(key) < 2:
        return ""
    return _fold("".join(lazy_pinyin(key, style=Style.NORMAL, errors="ignore")))


def pinyin_key(name: str) -> str:
    return spoken_text(name or "")


def _one_substitution(left: str, right: str) -> bool:
    if len(left) != len(right) or len(left) < 2:
        return False
    return sum(a != b for a, b in zip(left, right)) == 1


def _one_transposition(left: str, right: str) -> bool:
    if len(left) != len(right) or len(left) < 2:
        return False
    diffs = [index for index, (a, b) in enumerate(zip(left, right)) if a != b]
    if len(diffs) != 2 or diffs[1] != diffs[0] + 1:
        return False
    left_at, right_at = diffs
    return left[left_at] == right[right_at] and left[right_at] == right[left_at]


def names_correspond(left: str, right: str) -> bool:
    """True when two labels are the same name under the design's spelling rules."""
    first = name_key(left)
    second = name_key(right)
    if len(first) < 2 or len(second) < 2:
        return False
    if first == second:
        return True
    spoken_left = pinyin_key(left)
    spoken_right = pinyin_key(right)
    if spoken_left and spoken_right and spoken_left == spoken_right and len(spoken_left) >= 4:
        return True
    return _one_substitution(first, second) or _one_transposition(first, second)


_LATIN_TOKEN = re.compile(r"[a-z0-9]+", re.I)
_HAN = re.compile(r"[\u3400-\u9fff]")


def _script_bridge_keys(name: str) -> set[str]:
    """Keys that bridge a Chinese personal name to its Latin spellings.

    Used only for owner true-name / alias checks. General ``names_correspond``
    still refuses bare place-style pairs like 北城 / Beicheng.
    """
    text = unicodedata.normalize("NFKC", name or "").strip()
    if not text:
        return set()
    keys: set[str] = set()
    folded = name_key(text)
    if len(folded) >= 2:
        keys.add(folded)
    spoken = pinyin_key(text)
    if len(spoken) >= 4:
        keys.add(spoken)
    tokens = _LATIN_TOKEN.findall(text.casefold())
    if len(tokens) >= 2 and all(token.isascii() for token in tokens):
        joined = name_key("".join(tokens))
        reversed_join = name_key("".join(reversed(tokens)))
        if len(joined) >= 4:
            keys.add(joined)
        if len(reversed_join) >= 4:
            keys.add(reversed_join)
    han = "".join(_HAN.findall(text))
    if len(han) >= 2:
        from pypinyin import Style, lazy_pinyin

        parts = lazy_pinyin(han, style=Style.NORMAL, errors="ignore")
        if parts:
            surname_first = name_key("".join(parts))
            given_first = name_key("".join(parts[1:] + parts[:1]))
            if len(surname_first) >= 4:
                keys.add(surname_first)
            if len(given_first) >= 4:
                keys.add(given_first)
    return {key for key in keys if len(key) >= 2}


def owner_names_match(left: str, right: str) -> bool:
    """Owner true-name / alias match. Allows Latin ↔ Chinese pinyin bridges."""
    if names_correspond(left, right):
        return True
    left_keys = _script_bridge_keys(left)
    right_keys = _script_bridge_keys(right)
    return bool(left_keys and right_keys and left_keys & right_keys)
