"""Source coverage invariant: evidence must land before a version publishes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

DISTILLED = "蒸馏已结晶"
COVERAGE_COMPLETE = "complete"
SOURCE_STATES = {"partial", "complete"}

CAREER_CHAPTER = "事业经历"
EDU_CHAPTER = "教育背景"
RESIDUAL_CHAPTERS = {"仅保留原文", "残段摘要", "未覆盖原文"}
RESIDUAL_OPS = {"residual", "keep_raw"}

YEAR_SPAN = re.compile(
    r"(?:19|20)\d{2}(?:[./]\d{1,2})?\s*[-–—~～至到]\s*(?:(?:19|20)\d{2}(?:[./]\d{1,2})?|至今|今)"
)
COMPANY_MARK = re.compile(r"公司|集团|有限|科技|腾讯|阿里|字节")
SCHOOL_MARK = re.compile(r"大学|学院|中学|学校")
CAREER_HEAD = re.compile(r"(?m)^#{0,3}\s*(经历|任职|工作经历|事业经历)\s*$")
EDU_HEAD = re.compile(r"(?m)^#{0,3}\s*(教育|教育背景|学历|学业)\s*$")
SECTION_HEAD = re.compile(r"(?m)^#{1,3}\s+\S")
NAMED_HEADS = {
    "代表作品": re.compile(r"(?m)^#{0,3}\s*(代表作品|著作)\s*$"),
    "参与项目": re.compile(r"(?m)^#{0,3}\s*(参与项目|项目经历)\s*$"),
    "个人生活": re.compile(r"(?m)^#{0,3}\s*(个人生活|生活)\s*$"),
    "社交关系": re.compile(r"(?m)^#{0,3}\s*(社交关系|社会关系)\s*$"),
    "健康档案": re.compile(r"(?m)^#{0,3}\s*(健康档案|健康)\s*$"),
}


@dataclass
class CoverageReport:
    evidence: set[str] = field(default_factory=set)
    uncovered: list[dict] = field(default_factory=list)
    complete: bool = False
    residual: str = ""


def career_lines(body: str) -> list[str]:
    return _section_lines(body, CAREER_HEAD, COMPANY_MARK)


def education_lines(body: str) -> list[str]:
    return _section_lines(body, EDU_HEAD, SCHOOL_MARK)


def _section_lines(body: str, head: re.Pattern[str], mark: re.Pattern[str]) -> list[str]:
    lines: list[str] = []
    in_section = False
    for raw in (body or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if head.match(stripped):
            in_section = True
            continue
        if SECTION_HEAD.match(stripped):
            in_section = False
            continue
        if in_section or (YEAR_SPAN.search(stripped) and mark.search(stripped)):
            if YEAR_SPAN.search(stripped) or in_section:
                lines.append(stripped)
    return lines


def evidence_chapters(body: str, ctype: str, owner: bool = False) -> set[str]:
    """Which default person chapters the body actually supports."""
    del owner
    if ctype != "人物":
        return set()
    found: set[str] = set()
    if CAREER_HEAD.search(body or "") or career_lines(body):
        found.add(CAREER_CHAPTER)
    if EDU_HEAD.search(body or "") or education_lines(body):
        found.add(EDU_CHAPTER)
    for chapter, head in NAMED_HEADS.items():
        if head.search(body or ""):
            found.add(chapter)
    return found


def unit_chunks(chunks: Iterable[dict] | None, scope: str) -> list[dict]:
    """Chunks that overlap this distill unit.

    An empty overlap is empty. It does not widen to every chunk of the source,
    or one passage is refused until later passages are covered.
    """
    scope = scope or ""
    if not scope:
        return []
    picked: list[dict] = []
    for chunk in chunks or []:
        text = str(chunk.get("text") or "")
        if not text:
            continue
        if text in scope or scope in text:
            picked.append(chunk)
    return picked


def matching_chunk_seqs(chunks: Iterable[dict], quote: str) -> list[int]:
    quote = (quote or "").strip()
    if not quote:
        return []
    hits: list[int] = []
    for chunk in chunks:
        text = str(chunk.get("text") or "").strip()
        if not text:
            continue
        if quote in text or text in quote:
            hits.append(int(chunk["seq"]))
    return hits


def residual_summary_required(uncovered: list) -> str:
    if not uncovered:
        return ""
    bits = []
    for chunk in uncovered:
        if not isinstance(chunk, dict):
            text = str(getattr(chunk, "text", chunk) or "").strip()
            seq = getattr(chunk, "seq", "")
        else:
            text = str(chunk.get("text") or "").strip()
            seq = chunk.get("seq", "")
        if not text:
            continue
        bits.append(f"片段{seq}：{text[:80]}")
    return "未覆盖残段：\n" + "\n".join(bits)


def _item_chapters(items: list[dict], crystal: str = "") -> set[str]:
    names: set[str] = set()
    for item in items or []:
        if item.get("op") != "chapter":
            continue
        target = item.get("crystal") or "记忆主人"
        if crystal and target != crystal:
            continue
        name = (item.get("chapter") or "").strip()
        if name:
            names.add(name)
    return names


def _has_explicit_residual(items: list[dict] | None, sheet) -> bool:
    for item in items or []:
        if item.get("op") in RESIDUAL_OPS or item.get("residual"):
            return True
        if (item.get("chapter") or "").strip() in RESIDUAL_CHAPTERS:
            return True
    for block in sheet or []:
        if not isinstance(block, dict):
            continue
        for entry in block.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            key = (entry.get("key") or "").strip()
            if key in RESIDUAL_CHAPTERS:
                return True
            if entry.get("verdict") == "仅保留原文":
                return True
    return False


def _sheet_chapter_verdict(sheet, crystal: str, key: str) -> str | None:
    for block in sheet or []:
        if not isinstance(block, dict):
            continue
        if (block.get("crystal") or "") != crystal:
            continue
        for entry in block.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("kind") == "chapter" and entry.get("key") == key:
                return str(entry.get("verdict") or "")
    return None


def _targets(sheet, items: list[dict]) -> list[tuple[str, str, bool]]:
    found: dict[str, tuple[str, bool]] = {}
    for item in items or []:
        name = item.get("crystal") or ("记忆主人" if item.get("op") in {"field", "chapter"} else "")
        if item.get("op") == "open":
            name = item.get("name") or ""
            ctype = item.get("type") or ""
            if name and ctype:
                found[name] = (ctype, False)
            continue
        if not name:
            continue
        owner = name == "记忆主人"
        ctype = item.get("type") or ("人物" if owner else "")
        if ctype:
            found.setdefault(name, (ctype, owner))
    for block in sheet or []:
        if not isinstance(block, dict) or not block.get("crystal"):
            continue
        name = block["crystal"]
        ctype = block.get("type") or ("人物" if name == "记忆主人" else "")
        owner = name == "记忆主人"
        if ctype:
            found.setdefault(name, (ctype, owner))
    return [(name, meta[0], meta[1]) for name, meta in found.items()]


def forbid_synthetic_none(sheet, body: str, items: list[dict] | None) -> None:
    """「无」is only legal when the body has no evidence for that chapter."""
    items = items or []
    targets = _targets(sheet, items)
    if not targets and evidence_chapters(body, "人物", True):
        targets = [("记忆主人", "人物", True)]
    written_anywhere = _item_chapters(items)
    for name, ctype, owner in targets:
        evidence = evidence_chapters(body, ctype, owner)
        written = _item_chapters(items, name)
        residual = _has_explicit_residual(items, sheet)
        for chapter in evidence:
            if chapter in written or chapter in written_anywhere or residual:
                continue
            verdict = _sheet_chapter_verdict(sheet, name, chapter)
            if verdict == "无":
                raise RuntimeError(f"禁止合成「无」：{chapter} 原文有证据却标成无，不能覆盖")
            if sheet is None:
                raise RuntimeError(f"禁止合成「无」：缺清单且原文有{chapter}证据")


def _slice_evidence(text: str, wanted: set[str]) -> set[str]:
    return evidence_chapters(text, "人物", True) & wanted


def _chunk_covered(chunk: dict, items: list[dict]) -> bool:
    text = str(chunk.get("text") or "").strip()
    if not text:
        return True
    for item in items or []:
        quote = str(item.get("quote") or "").strip()
        if not quote:
            continue
        if item.get("op") not in {"chapter", "field", "open", "residual", "keep_raw"} and not item.get("residual"):
            continue
        if item.get("op") != "chapter":
            continue
        if quote in text or text in quote:
            return True
    return False


def assert_coverage(body: str, chunks: list[dict] | None, items: list[dict] | None, sheet=None) -> CoverageReport:
    """Refuse field-only or silent drops when the body has career/education evidence."""
    items = items or []
    chunks = [dict(chunk) for chunk in (chunks or [])]
    evidence = evidence_chapters(body, "人物", True)
    for _name, ctype, owner in _targets(sheet, items):
        evidence |= evidence_chapters(body, ctype, owner)
    residual = _has_explicit_residual(items, sheet)
    written = _item_chapters(items)
    missing = evidence - written
    if evidence and not written and not residual:
        raise RuntimeError("覆盖不足：只有字段没有章节，缺" + "、".join(sorted(evidence)))
    if missing and not residual:
        raise RuntimeError("覆盖不足：缺少章节 " + "、".join(sorted(missing)))
    uncovered = []
    for chunk in chunks:
        needed = _slice_evidence(str(chunk.get("text") or ""), evidence)
        if not needed:
            continue
        if _chunk_covered(chunk, items):
            continue
        uncovered.append(chunk)
    if uncovered and not residual:
        raise RuntimeError("覆盖不足：未覆盖残段未落点，缺" + "、".join(sorted(evidence)))
    summary = residual_summary_required(uncovered) if uncovered else ""
    return CoverageReport(
        evidence=evidence,
        uncovered=uncovered,
        complete=not uncovered,
        residual=summary,
    )


def mark_chunks_cited(con: Any, source_id: str, chunk_seqs: list[int]) -> None:
    """Cite only the given chunk_seq values. Never mark a whole source."""
    for seq in chunk_seqs or []:
        if seq is None:
            continue
        con.execute(
            "UPDATE chunks SET cited=1 WHERE source_id=? AND seq=?",
            (source_id, int(seq)),
        )


def coverage_state_for_source(con: Any, source_id: str, body: str) -> Literal["partial", "complete"]:
    chunks = [dict(row) for row in con.execute(
        "SELECT seq, text, cited FROM chunks WHERE source_id=? ORDER BY seq",
        (source_id,),
    )]
    evidence = evidence_chapters(body, "人物", True)
    residual = ""
    row = con.execute("SELECT residual FROM sources WHERE id=?", (source_id,)).fetchone()
    if row is not None:
        residual = (row["residual"] if not isinstance(row, tuple) else row[0]) or ""
    uncovered = []
    for chunk in chunks:
        if chunk.get("cited"):
            continue
        if _slice_evidence(str(chunk.get("text") or ""), evidence):
            uncovered.append(chunk)
    if uncovered and not residual:
        return "partial"
    return "complete"


def set_source_distill_state(con: Any, source_id: str, state: Literal["partial", "complete"]) -> None:
    if state not in SOURCE_STATES:
        raise ValueError(f"非法蒸馏状态：{state}")
    con.execute(
        "UPDATE sources SET status=?, coverage=? WHERE id=?",
        (DISTILLED, state, source_id),
    )
