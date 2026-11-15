"""Split a file by heading, page, or slide. Fall back to the whole text."""

from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_SLIDE = re.compile(r"ppt/slides/slide(\d+)\.xml$")
_DRAWING_TEXT = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"


def plain_text(path: Path) -> str:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("gb18030", errors="replace")
    return text.replace("\r\n", "\n").strip()


def markdown_sections(text: str) -> list[tuple[str, str]]:
    heading = ""
    buf: list[str] = []
    found = False
    sections: list[tuple[str, str]] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if heading or body:
            sections.append((heading, body))

    for line in text.replace("\r\n", "\n").split("\n"):
        match = _HEADING.match(line)
        if match:
            found = True
            flush()
            heading = match.group(1).strip()
            buf = []
        else:
            buf.append(line)
    flush()
    if not found:
        return [("", text.strip())] if text.strip() else []
    return [(title, body) for title, body in sections if title or body]


def pdf_sections(path: Path) -> list[tuple[str, str]]:
    from pypdf import PdfReader

    sections = []
    for index, page in enumerate(PdfReader(str(path)).pages, 1):
        body = (page.extract_text() or "").strip()
        if body:
            sections.append((f"第{index}页", body))
    return sections


def docx_sections(path: Path) -> list[tuple[str, str]]:
    from docx import Document

    document = Document(str(path))
    heading = ""
    buf: list[str] = []
    found = False
    sections: list[tuple[str, str]] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if heading or body:
            sections.append((heading, body))

    for paragraph in document.paragraphs:
        style = (paragraph.style.name if paragraph.style is not None else "") or ""
        if style.startswith("Heading"):
            found = True
            flush()
            heading = paragraph.text.strip()
            buf = []
        else:
            buf.append(paragraph.text)
    flush()
    if not found:
        text = "\n".join(paragraph.text for paragraph in document.paragraphs).strip()
        return [("", text)] if text else []
    return [(title, body) for title, body in sections if title or body]


def pptx_sections(path: Path) -> list[tuple[str, str]]:
    sections = []
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if _SLIDE.fullmatch(name)]
        names.sort(key=lambda name: int(_SLIDE.fullmatch(name).group(1)))
        for index, name in enumerate(names, 1):
            root = ET.fromstring(archive.read(name))
            texts = [(node.text or "").strip() for node in root.iter(_DRAWING_TEXT)]
            body = "\n".join(part for part in texts if part)
            if body:
                sections.append((f"第{index}张", body))
    return sections


def _reader_reason(exc: BaseException) -> str:
    text = str(exc).strip()
    if not text:
        return ""
    line = text.splitlines()[-1].strip()
    if line == "没有抽出正文":
        return ""
    return line[:200]


def xlsx_sections(path: Path) -> list[tuple[str, str]]:
    """Cell text from the workbook itself. A failed rich reader must not erase it."""
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=True, data_only=False)
    sections: list[tuple[str, str]] = []
    try:
        for sheet in book.worksheets:
            lines: list[str] = []
            for row in sheet.iter_rows(values_only=True):
                cells = [str(value).strip() for value in row if value is not None and str(value).strip()]
                if cells:
                    lines.append("\t".join(cells))
            body = "\n".join(lines).strip()
            if body:
                sections.append((sheet.title or "", body))
    finally:
        book.close()
    return sections


def xls_sections(path: Path) -> list[tuple[str, str]]:
    import xlrd

    book = xlrd.open_workbook(str(path))
    sections: list[tuple[str, str]] = []
    for sheet in book.sheets():
        lines: list[str] = []
        for index in range(sheet.nrows):
            cells = []
            for column in range(sheet.ncols):
                text = str(sheet.cell_value(index, column)).strip()
                if text:
                    cells.append(text)
            if cells:
                lines.append("\t".join(cells))
        body = "\n".join(lines).strip()
        if body:
            sections.append((sheet.name or "", body))
    return sections


def _weknora_sections(path: Path) -> list[tuple[str, str]] | None:
    from rogmem.weknora_read import markdown_for, supports

    suffix = path.suffix.lower().lstrip(".")
    if not supports(suffix):
        return None
    text = markdown_for(path)
    sections = markdown_sections(text)
    if not any(body for _, body in sections):
        raise ValueError("没有抽出正文")
    return sections


def read_sections(path: Path) -> list[tuple[str, str]]:
    suffix = path.suffix.lower().lstrip(".")
    rich_error = ""
    if suffix in {"docx", "pdf", "xlsx", "xls", "pptx", "ppt", "csv"}:
        try:
            parsed = _weknora_sections(path)
        except Exception as exc:
            parsed = None
            rich_error = _reader_reason(exc)
        if parsed:
            return parsed
    if suffix == "pdf":
        sections = pdf_sections(path)
    elif suffix == "docx":
        sections = docx_sections(path)
    elif suffix == "pptx":
        sections = pptx_sections(path)
    elif suffix == "xlsx":
        try:
            sections = xlsx_sections(path)
        except Exception as exc:
            raise ValueError(_reader_reason(exc) or rich_error or "没有抽出正文") from exc
    elif suffix == "xls":
        try:
            sections = xls_sections(path)
        except Exception as exc:
            raise ValueError(_reader_reason(exc) or rich_error or "没有抽出正文") from exc
    elif suffix == "ppt":
        raise ValueError(rich_error or "没有抽出正文")
    else:
        sections = markdown_sections(plain_text(path))
    if not any(body for _, body in sections):
        raise ValueError(rich_error or "没有抽出正文")
    return sections


def join_sections(sections: list[tuple[str, str]]) -> str:
    parts = []
    for heading, body in sections:
        if heading:
            parts.append(heading)
        if body:
            parts.append(body)
    return "\n".join(parts).strip()
