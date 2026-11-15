"""Read office and PDF files with the vendored WeKnora docreader parsers.

The parsers live under rogmem/vendor/weknora and keep the upstream import name
``docreader``. Text, Markdown, and a failed parse stay on rogmem's own readers.

A page the parser can only hand back as a picture is still unread until the
words in that picture are read. The markdown filename is not those words.
"""

from __future__ import annotations

import base64
import importlib
import io
import re
import sys
import threading
from pathlib import Path

_VENDOR = Path(__file__).resolve().parent / "vendor" / "weknora"

# suffix -> (module, class). Docx uses the table-aware parser first.
_PARSERS = {
    "docx": ("docreader.parser.docx_parser", "DocxParser"),
    "pdf": ("docreader.parser.pdf_parser", "PDFParser"),
    "xlsx": ("docreader.parser.excel_parser", "ExcelParser"),
    "xls": ("docreader.parser.excel_parser", "ExcelParser"),
    "pptx": ("docreader.parser.markitdown_parser", "MarkitdownParser"),
    "ppt": ("docreader.parser.markitdown_parser", "MarkitdownParser"),
    "csv": ("docreader.parser.markitdown_parser", "MarkitdownParser"),
}


def supports(suffix: str) -> bool:
    return suffix.lower().lstrip(".") in _PARSERS


# A picture link is the embed itself. The destination may contain spaces;
# those spaces are part of the name, not a reason to treat the link as prose.
_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]*)\)")
_READ_LOCK = threading.Lock()
_READER = None
_READER_MISSING = False


def _ensure_path() -> None:
    root = str(_VENDOR)
    if root not in sys.path:
        sys.path.insert(0, root)


def markdown_for(path: Path) -> str:
    """Return markdown extracted by WeKnora. Raises if this suffix is unsupported or empty."""
    suffix = path.suffix.lower().lstrip(".")
    spec = _PARSERS.get(suffix)
    if spec is None:
        raise ValueError(f"不使用 WeKnora 解析 {suffix}")
    _ensure_path()
    module_name, class_name = spec
    module = importlib.import_module(module_name)
    parser = getattr(module, class_name)(file_name=path.name, file_type=suffix)
    document = parser.parse(path.read_bytes())
    text = _absorb_images(
        (getattr(document, "content", "") or "").strip(),
        getattr(document, "images", None) or {},
    )
    if not text:
        raise ValueError("没有抽出正文")
    return text


def picture_links_removed(text: str) -> str:
    """Drop picture embeds. Their filenames are not words."""
    return _IMAGE.sub(" ", text or "")


def _absorb_images(text: str, images: dict) -> str:
    """Keep prose. A picture embed contributes the words in its pixels, or nothing."""
    if not text or _IMAGE.search(text) is None:
        return text

    def replace(match: re.Match[str]) -> str:
        data = _image_bytes(match.group(1).strip(), images)
        if not data:
            return ""
        return _words_in_image(data)

    absorbed = _IMAGE.sub(replace, text)
    blocks = []
    for block in re.split(r"\n{2,}", absorbed):
        lines = [line for line in block.splitlines() if line.strip()]
        body = "\n".join(lines).strip()
        if body:
            blocks.append(body)
    return "\n\n".join(blocks).strip()


def _image_bytes(ref: str, images: dict) -> bytes | None:
    raw = images.get(ref)
    if raw is None:
        name = ref.rsplit("/", 1)[-1]
        raw = next((value for key, value in images.items() if key.rsplit("/", 1)[-1] == name), None)
    if not raw:
        return None
    try:
        return base64.b64decode(raw)
    except (ValueError, TypeError):
        return None


def _reader():
    global _READER, _READER_MISSING
    if _READER_MISSING:
        return None
    if _READER is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ModuleNotFoundError:
            _READER_MISSING = True
            return None
        _READER = RapidOCR()
    return _READER


def _words_in_image(data: bytes) -> str:
    """Words a reader can see in the picture. Empty when the picture has none."""
    try:
        reader = _reader()
    except Exception:
        return ""
    if reader is None or not data:
        return ""
    try:
        from PIL import Image
    except ModuleNotFoundError:
        return ""
    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return ""
    try:
        with _READ_LOCK:
            result, _elapsed = reader(image)
    except Exception:
        return ""
    if not result:
        return ""
    lines = []
    for item in result:
        if not item or len(item) < 2:
            continue
        words = str(item[1]).strip()
        if words:
            lines.append(words)
    return "\n".join(lines)
