"""Text splitting ported from WeKnora's chunker.

Three behaviors are kept: protected regions stay intact, consecutive chunks
overlap on a real boundary, and parent chunks hold context while only the
smaller children are meant to be embedded.

Defaults are character units (the existing Chunker unit): chunk=512, overlap=80.
Illegal overlap (zero, negative, or ≥ chunk size) is rejected, not clamped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CHUNK_SIZE = 512
CHUNK_OVERLAP = 80
PARENT_SIZE = 4096
CHILD_SIZE = CHUNK_SIZE
PARENT_OVERLAP = CHUNK_OVERLAP
ABSOLUTE_MAX = 7500

_SEPARATORS = ("\n\n", "\n", "。")
_PROTECTED = (
    re.compile(r"(?s)\$\$.*?\$\$"),
    re.compile(r"!\[[^\]\n]{0,200}\]\([^)\n]{1,500}\)"),
    re.compile(r"\[[^\]\n]{1,200}\]\([^)\n]{1,500}\)"),
    re.compile(r"(?m)[ ]*(?:\|[^|\n]*)+\|[\r\n]+\s*(?:\|\s*:?-{3,}:?\s*)+\|[\r\n]+"),
    re.compile(r"(?m)[ ]*(?:\|[^|\n]*)+\|[\r\n]+"),
    re.compile(r"(?s)```(?:\w+)?[\r\n].*?```"),
    re.compile(r"`[^`\r\n]+`"),
)


class ChunkerConfigError(ValueError):
    """Illegal chunk_size / overlap. Callers must not clamp and continue."""


@dataclass
class Chunk:
    content: str
    start: int
    end: int
    context_header: str = ""
    parent_index: int | None = None

    def embedding_content(self) -> str:
        body = self.content.strip()
        if not self.context_header:
            return body
        return self.context_header + "\n\n" + body


@dataclass
class _Unit:
    text: str
    start: int
    end: int
    protected: bool = False


def derived_overlap(chunk_size: int) -> int:
    """Legal overlap when the 80-char default does not fit this size."""
    if chunk_size <= 1:
        return 0
    guess = min(CHUNK_OVERLAP, max(chunk_size // 5, 1))
    if guess >= chunk_size:
        return max(chunk_size - 1, 0)
    return guess


def resolve_chunk_params(chunk_size: int | None = None, overlap: int | None = None) -> tuple[int, int]:
    size = CHUNK_SIZE if chunk_size is None else int(chunk_size)
    if size <= 0:
        raise ChunkerConfigError(f"chunk_size must be positive, got {chunk_size!r}")
    if overlap is None:
        ov = CHUNK_OVERLAP if CHUNK_OVERLAP < size else derived_overlap(size)
    else:
        ov = int(overlap)
    if ov <= 0 or ov >= size:
        raise ChunkerConfigError(
            f"overlap must be in (0, chunk_size), got overlap={overlap!r} chunk_size={size}"
        )
    return size, ov


def shared_boundary(left: str, right: str) -> str:
    """Longest suffix of left that is a prefix of right."""
    limit = min(len(left), len(right))
    for n in range(limit, 0, -1):
        if left.endswith(right[:n]):
            return right[:n]
    return ""


def uncovered_span(parent: Chunk, children: list[Chunk]) -> str:
    """Parent characters not covered by any child's [start, end) span."""
    text = parent.content
    n = len(text)
    covered = bytearray(n)
    for child in children:
        start = max(0, child.start - parent.start)
        end = min(n, child.end - parent.start)
        if start < end:
            covered[start:end] = b"\x01" * (end - start)
    return "".join(char for char, flag in zip(text, covered) if not flag)


def split_text(
    text: str,
    chunk_size: int | None = None,
    overlap: int | None = None,
    context_header: str = "",
) -> list[Chunk]:
    chunk_size, overlap = resolve_chunk_params(chunk_size, overlap)
    text = text.replace("\r\n", "\n")
    if not text.strip():
        return []
    units = _units(text, _protected_spans(text), chunk_size)
    chunks = _merge(units, chunk_size, overlap)
    for chunk in chunks:
        chunk.context_header = context_header
    return [chunk for chunk in chunks if chunk.content.strip()]


def split_headed(
    text: str,
    parent_size: int = PARENT_SIZE,
    child_size: int = CHILD_SIZE,
    context_header: str = "",
) -> tuple[list[Chunk], list[Chunk]]:
    """Split a headed document by heading, then with the same parent/child rules.

    Fewer than two real sections falls back to ``split_parent_child``. A section
    shorter than 40 characters is merged into the previous one so a heading
    fragment does not become its own tier.
    """
    sections = _heading_sections(text)
    if sections is None:
        return split_parent_child(text, parent_size, child_size, context_header)
    parents: list[Chunk] = []
    children: list[Chunk] = []
    for section, title in sections:
        extra = f"标题：{title}" if title else ""
        header = "\n".join(bit for bit in (context_header, extra) if bit)
        part_parents, part_children = split_parent_child(section, parent_size, child_size, header)
        shift = len(parents)
        parents.extend(part_parents)
        for child in part_children:
            if child.parent_index is not None:
                child.parent_index += shift
            children.append(child)
    return parents, children


def _heading_sections(text: str) -> list[tuple[str, str]] | None:
    masked = list(text)
    for start, end in _protected_spans(text):
        for index in range(start, end):
            if masked[index] != "\n":
                masked[index] = " "
    matches = list(re.finditer(r"(?m)^(#{1,3}[ \t]+[^\n]+)$", "".join(masked)))
    if len(matches) < 2:
        return None
    ranges: list[tuple[int, int, str]] = []
    if matches[0].start() > 0:
        ranges.append((0, matches[0].start(), ""))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        title = text[match.start() : match.end()].lstrip("#").strip()
        ranges.append((match.start(), end, title))
    merged: list[tuple[str, str]] = []
    for start, end, title in ranges:
        piece = text[start:end]
        if merged and len(piece.strip()) < 40:
            previous, previous_title = merged[-1]
            merged[-1] = (previous + piece, previous_title)
        else:
            merged.append((piece, title))
    if len(merged) < 2:
        return None
    return merged


def split_parent_child(
    text: str,
    parent_size: int = PARENT_SIZE,
    child_size: int = CHILD_SIZE,
    context_header: str = "",
) -> tuple[list[Chunk], list[Chunk]]:
    """Split into large parents and smaller children. A child that is the whole parent has no parent."""
    parents = split_text(text, parent_size, None, context_header)
    kept: list[Chunk] = []
    children: list[Chunk] = []
    for parent in parents:
        subs = split_text(parent.content, child_size)
        parent_index = None
        if len(subs) > 1 or (len(subs) == 1 and subs[0].content != parent.content):
            parent_index = len(kept)
            kept.append(parent)
        linked = [
            Chunk(
                content=sub.content,
                start=parent.start + sub.start,
                end=parent.start + sub.end,
                context_header=context_header,
                parent_index=parent_index,
            )
            for sub in subs
        ]
        children.extend(_fill_parent_holes(parent, linked, context_header, parent_index))
    return kept, children


def _fill_parent_holes(
    parent: Chunk,
    children: list[Chunk],
    context_header: str,
    parent_index: int | None,
) -> list[Chunk]:
    hole = uncovered_span(parent, children)
    if not hole:
        return children
    n = len(parent.content)
    covered = bytearray(n)
    for child in children:
        start = max(0, child.start - parent.start)
        end = min(n, child.end - parent.start)
        if start < end:
            covered[start:end] = b"\x01" * (end - start)
    filled = list(children)
    index = 0
    while index < n:
        if covered[index]:
            index += 1
            continue
        stop = index
        while stop < n and not covered[stop]:
            stop += 1
        piece = parent.content[index:stop]
        filled.append(
            Chunk(
                content=piece,
                start=parent.start + index,
                end=parent.start + stop,
                context_header=context_header,
                parent_index=parent_index,
            )
        )
        index = stop
    filled.sort(key=lambda item: (item.start, item.end))
    return filled


def _protected_spans(text: str) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    for pattern in _PROTECTED:
        for match in pattern.finditer(text):
            if match.end() > match.start():
                found.append((match.start(), match.end()))
    found.sort(key=lambda item: (item[0], -item[1]))
    kept: list[tuple[int, int]] = []
    last = 0
    for start, end in found:
        if start >= last:
            kept.append((start, end))
            last = end
    return kept


def _units(text: str, protected: list[tuple[int, int]], chunk_size: int) -> list[_Unit]:
    units: list[_Unit] = []
    cursor = 0
    for start, end in protected:
        if start > cursor:
            units.extend(_plain_units(text[cursor:start], cursor, chunk_size))
        piece = text[start:end]
        if len(piece) > ABSOLUTE_MAX:
            units.extend(_force_units(piece, start, ABSOLUTE_MAX, 0))
        else:
            units.append(_Unit(piece, start, end, protected=True))
        cursor = end
    if cursor < len(text):
        units.extend(_plain_units(text[cursor:], cursor, chunk_size))
    return units


def _plain_units(text: str, offset: int, chunk_size: int) -> list[_Unit]:
    parts = _split_separators(text, list(_SEPARATORS), chunk_size)
    units = []
    cursor = offset
    for part in parts:
        units.append(_Unit(part, cursor, cursor + len(part)))
        cursor += len(part)
    return units


def _force_units(text: str, offset: int, step: int = ABSOLUTE_MAX, overlap: int = 0) -> list[_Unit]:
    units = []
    cursor = 0
    if step <= 0:
        step = CHUNK_SIZE
    if overlap < 0 or overlap >= step:
        overlap = 0
    while cursor < len(text):
        end = min(len(text), cursor + step)
        if end < len(text):
            window = text[max(cursor, end - 200) : end]
            cut = max(window.rfind("\n"), window.rfind(" "))
            if cut >= 0:
                end = max(cursor, end - 200) + cut + 1
        if end <= cursor:
            end = min(len(text), cursor + max(step, 1))
        units.append(_Unit(text[cursor:end], offset + cursor, offset + end))
        if end >= len(text):
            break
        nxt = end - overlap
        if nxt <= cursor:
            nxt = cursor + 1
        cursor = nxt
    return units


def _split_separators(text: str, separators: list[str], chunk_size: int) -> list[str]:
    if text == "" or not separators:
        return [text] if text else []
    if chunk_size > 0 and len(text) <= chunk_size:
        return [text]
    for index, sep in enumerate(separators):
        if not sep or sep not in text:
            continue
        pattern = re.compile("(" + re.escape(sep) + ")")
        bits = pattern.split(text)
        if len(bits) <= 1:
            continue
        pieces = [bit for bit in bits if bit]
        remaining = separators[index + 1 :]
        out: list[str] = []
        for piece in pieces:
            if chunk_size > 0 and len(piece) > chunk_size and remaining:
                out.extend(_split_separators(piece, remaining, chunk_size))
            else:
                out.append(piece)
        return out
    return [text]


def _merge(units: list[_Unit], chunk_size: int, overlap: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    current: list[_Unit] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current and "".join(item.text for item in current).strip():
            chunks.append(_build(current, len(chunks)))
        current = []
        current_len = 0

    for unit in units:
        if (not unit.protected and len(unit.text) > chunk_size) or len(unit.text) > ABSOLUTE_MAX:
            if current:
                flush()
            step = ABSOLUTE_MAX if unit.protected else chunk_size
            forced = _force_units(unit.text, unit.start, step, 0 if unit.protected else overlap)
            if chunks and overlap > 0 and forced:
                forced[0] = _prepend_overlap(chunks[-1], forced[0], overlap)
            for piece in forced:
                chunks.append(_build([piece], len(chunks)))
            continue
        if current and current_len + len(unit.text) > chunk_size:
            flush()
            current = _overlap_from_chunk(chunks[-1], overlap) if chunks else []
            current_len = sum(len(item.text) for item in current)
        current.append(unit)
        current_len += len(unit.text)
    if current and "".join(item.text for item in current).strip():
        chunks.append(_build(current, len(chunks)))
    return chunks


def _build(units: list[_Unit], seq: int) -> Chunk:
    del seq
    return Chunk(
        content="".join(unit.text for unit in units),
        start=units[0].start,
        end=units[-1].end,
    )


def _unprotected_overlap_slice(text: str, overlap: int) -> tuple[int, str]:
    """Overlap suffix that does not leak a protected span into the next chunk."""
    if overlap <= 0 or not text:
        return 0, ""
    marked = bytearray(len(text))
    for start, end in _protected_spans(text):
        if start < end:
            marked[start:end] = b"\x01" * (end - start)
    end = len(text)
    while end > 0 and marked[end - 1]:
        end -= 1
    start = end
    while start > 0 and not marked[start - 1] and (end - start) < overlap:
        start -= 1
    if start >= end:
        return 0, ""
    cut = _semantic_cut(text[:end], start)
    if cut is None or cut < start or cut >= end or any(marked[cut:end]):
        cut = start
    suffix = text[cut:end]
    if not suffix:
        return 0, ""
    return cut, suffix


def _overlap_from_chunk(chunk: Chunk, overlap: int) -> list[_Unit]:
    cut, suffix = _unprotected_overlap_slice(chunk.content, overlap)
    if not suffix:
        return []
    start = chunk.start + cut
    return [_Unit(suffix, start, start + len(suffix))]


def _prepend_overlap(previous: Chunk, unit: _Unit, overlap: int) -> _Unit:
    if shared_boundary(previous.content, unit.text):
        return unit
    cut, prefix = _unprotected_overlap_slice(previous.content, overlap)
    if not prefix:
        return unit
    start = previous.start + cut
    return _Unit(prefix + unit.text, start, unit.end)


def _semantic_cut(text: str, min_end: int) -> int | None:
    """Return the exclusive end of the separator where the next chunk should start."""
    protected = _protected_spans(text)

    def inside(pos: int) -> bool:
        return any(start <= pos < end for start, end in protected)

    best: tuple[int, int, int] | None = None

    def consider(start: int, end: int, priority: int) -> None:
        nonlocal best
        if start < 0 or end <= start or end < min_end or end > len(text):
            return
        if inside(start) or not text[end:].strip():
            return
        if best is None or priority < best[0] or (priority == best[0] and start < best[1]):
            best = (priority, start, end)

    paragraph = [False] * len(text)
    index = 0
    while index < len(text) - 1:
        if text.startswith("\n\n", index):
            consider(index, index + 2, 1)
            paragraph[index] = paragraph[index + 1] = True
            index += 2
            continue
        index += 1
    for index, char in enumerate(text):
        if paragraph[index]:
            continue
        if char == "\n":
            consider(index, index + 1, 2)
    for index, char in enumerate(text):
        if char in "。？！":
            consider(index, index + 1, 3)
        elif char in ".?!" and index + 1 < len(text) and text[index + 1] == " ":
            consider(index, index + 2, 3)
    if best is None:
        return None
    return best[2]
