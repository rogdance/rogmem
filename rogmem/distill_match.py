"""Program Match for Distiller integrate: name/alias only.

No GraphRAG, no multi-hop. One or more library hits return
``library_candidates`` (all N) for ``_llm_integrate``. Draft v5.3
mid-稿 is produced separately; this module only matches names/aliases.
Zero hits return {} so the engine takes Match=0 identity=new.
≥2 hits stay on Integrate — the model, not Match, decides same/new/maybe.
"""

from __future__ import annotations

from rogmem.names import (
    _one_substitution,
    _one_transposition,
    name_key,
    names_correspond,
    pinyin_key,
    spoken_text,
    syllable,
)


def match_integrate_parts(text: str, library: list | None = None) -> dict:
    """Return integrate candidate kwargs when at least one library crystal matches."""
    hits = [
        hit
        for hit in _name_alias_hits(text or "", library or [])
        if str(hit.get("name") or "").strip()
    ]
    if not hits:
        return {}
    return {"library_candidates": hits}


def pick_matched_draft_crystal(draft, hit) -> dict | None:
    """Pick the first Draft v5 crystal that overlaps any given Match hit.

    Requires non-empty facts. Empty-facts stubs are not a mid-稿.
    ``hit`` may be one library item or a sequence of them.
    Production N-loop uses ``list_draft_crystals`` (all mid-稿s), not this.
    """
    hits = hit if isinstance(hit, (list, tuple)) else [hit]
    labeled = []
    for item in hits:
        if not isinstance(item, dict):
            continue
        labels = _item_labels(item)
        if labels:
            labeled.append(labels)
    if not labeled:
        return None
    for crystal in _draft_crystals(draft):
        if not _nonempty_facts(crystal):
            continue
        crystal_labels = _item_labels(crystal)
        if any(_labels_overlap(crystal_labels, labels) for labels in labeled):
            return crystal
    return None


def list_draft_crystals(draft) -> list[dict]:
    """All Draft v5 mid-稿s with non-empty facts. Empty-facts stubs are dropped."""
    return [item for item in _draft_crystals(draft) if _nonempty_facts(item)]


def _draft_crystals(draft) -> list[dict]:
    if isinstance(draft, list):
        return [item for item in draft if isinstance(item, dict)]
    if not isinstance(draft, dict):
        return []
    raw = draft.get("crystals")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    inner = draft.get("result")
    if isinstance(inner, dict) and isinstance(inner.get("crystals"), list):
        return [item for item in inner["crystals"] if isinstance(item, dict)]
    if str(draft.get("name") or "").strip():
        return [draft]
    return []


def _nonempty_facts(crystal: dict) -> bool:
    facts = crystal.get("facts")
    return isinstance(facts, list) and any(isinstance(item, dict) and item for item in facts)


def _item_labels(item: dict) -> list[str]:
    labels = [str(item.get("name") or "").strip()]
    aliases = item.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    labels.extend(str(alias).strip() for alias in aliases)
    return [label for label in labels if label]


def _labels_overlap(left: list[str], right: list[str]) -> bool:
    for first in left:
        for second in right:
            if first == second:
                return True
            if names_correspond(first, second):
                return True
            key = name_key(first)
            if len(key) >= 2 and key == name_key(second):
                return True
    return False


def _name_alias_hits(text: str, library: list) -> list[dict]:
    scan = _BodyScan(text) if text else None
    hits = []
    for item in library:
        if not isinstance(item, dict):
            continue
        if not str(item.get("id") or "").strip():
            continue
        if any(_label_in_text(label, text, scan) for label in _item_labels(item)):
            hits.append(item)
    return hits


def page_identity(query: str, name: str, aliases: list[str] | None = None) -> tuple[int, int] | None:
    """The query is a name this page is opened by.

    A longer question that merely contains the name is a different question.
    That one belongs to full-text search. The first number is smaller for the
    page's own title than for another published name. The second is the length
    of the matched name.
    """
    text = (query or "").strip()
    title = str(name or "").strip()
    labels: list[str] = []
    for raw in [title, *(aliases or [])]:
        label = str(raw or "").strip()
        if label and label not in labels and label not in {"尚无来源", "暂无来源"}:
            labels.append(label)
    if not text or not labels:
        return None
    matched: list[tuple[int, int]] = []
    for label in labels:
        if not names_correspond(text, label):
            continue
        matched.append((0 if label == title else 1, len(name_key(label))))
    if not matched:
        return None
    return min(matched, key=lambda item: (item[0], -item[1]))


class _BodyScan:
    """One body, prepared once, so every label reads it without re-transliterating it.

    The old walk asked ``names_correspond`` about every window of the body once per label.
    Both sides of that question transliterate through pypinyin, so the cost was
    ``body length × labels × 2`` pinyin calls and one real body took seconds. Character keys
    and character syllables are the same answer, asked once for the whole body.
    """

    __slots__ = ("text", "keys", "syllables", "stream", "_whole")

    def __init__(self, text: str) -> None:
        self.text = text
        self.keys = [name_key(char) for char in text]
        self.syllables = [syllable(char) for char in text]
        self.stream = "".join(self.syllables)
        self._whole = None

    def whole_spoken(self) -> str:
        """Phrase-level pinyin, asked at most once, and only when the syllables do not settle it."""
        if self._whole is None:
            self._whole = spoken_text(self.text)
        return self._whole


def _label_in_text(label: str, text: str, scan: "_BodyScan | None" = None) -> bool:
    raw = (label or "").strip()
    if len(raw) < 2 or not text:
        return False
    if raw in text:
        return True
    key = name_key(raw)
    if len(key) >= 2 and key in name_key(text):
        return True
    width = len(raw)
    if width > len(text):
        return False
    scan = scan if scan is not None else _BodyScan(text)
    spoken = pinyin_key(raw)
    if spoken and len(spoken) >= 4:
        if spoken in scan.stream:
            return True
        if spoken in scan.whole_spoken():
            # Phrase-level pinyin can part company with character-level. Confirm on the windows.
            for index in range(len(text) - width + 1):
                if names_correspond(raw, text[index : index + width]):
                    return True
    keys = scan.keys
    for index in range(len(text) - width + 1):
        window = "".join(keys[index : index + width])
        if len(window) != len(key):
            continue
        if _one_substitution(key, window) or _one_transposition(key, window):
            return True
    return False
