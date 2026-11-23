"""「文件资料」按体量分级读取：小文件整本预览，大文件按片显示。

详情页原本按「一条来源 = 一轮对话，几百字」设计。整本书进来时，服务端会把
全文读进内存、拼进 HTML，前端渲染器再整本塞进一个 DOM 节点，浏览器排版进程
被拖垮。这里把体量判定、分片读取与编码探测收在一处，让所有入口共用同一份
预算；任何文件都先过这道闸，而不是靠某个具体文件名兜底。
"""

from __future__ import annotations

import codecs
from pathlib import Path

# 小文件才交给离线渲染器整本渲染（原件字节）。
PREVIEW_INLINE_LIMIT = 256 * 1024
# 任何格式超过这个体量都不再整本交给浏览器渲染器。
PREVIEW_HARD_LIMIT = 32 * 1024 * 1024
# 纯文本类会整本渲染成 DOM，是本次事故的类型；PDF / Office 自带按页渲染。
TEXT_SUFFIXES = {"txt", "md", "markdown", "csv"}
# 一片最多读多少字节。浏览器只排版这一片。
WINDOW_BYTES = 64 * 1024
# 详情页「文本解析」直出的字符上限。
TEXT_RENDER_LIMIT = 120 * 1024
# 超过这个字符数就不做全文 O(句数 × 引文数) 对账。
DETAIL_SCAN_LIMIT = 256 * 1024

_ENCODINGS = ("utf-8", "gb18030")


def decode_document(data: bytes) -> tuple[str, str]:
    """严格探测解码，失败才兜底替换。中文 txt 常见于 GBK / GB18030。"""
    for enc in _ENCODINGS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8/replace"


def _decode_window(data: bytes) -> tuple[str, str, int]:
    """窗口可能切在多字节字符中间：末尾最多回退 3 字节，并返回实际消费字节数。

    下一片从实际消费处开始，既不丢字也不产生替换字符。
    """
    for cut in range(0, 4):
        chunk = data[: len(data) - cut] if cut else data
        if not chunk:
            continue
        for enc in _ENCODINGS:
            try:
                text = chunk.decode(enc)
            except UnicodeDecodeError:
                continue
            return text, enc, len(chunk)
    return data.decode("utf-8", errors="replace"), "utf-8/replace", len(data)


def needs_window(size: int, suffix: str = "") -> bool:
    """文本类大文件必须按片走；二进制格式由各自渲染器按页处理。"""
    if int(size or 0) <= PREVIEW_INLINE_LIMIT:
        return False
    return (not suffix) or suffix.lower().lstrip(".") in TEXT_SUFFIXES


def needs_download_only(size: int, suffix: str = "") -> bool:
    """非文本类超过绝对上限时不再整本交渲染器，只留下载 / 新窗口。"""
    return int(size or 0) > PREVIEW_HARD_LIMIT and not needs_window(size, suffix)


def sniff_encoding(path: Path) -> str:
    """只读文件头探测真实编码，给 inline 原件声明正确的 charset。

    用增量解码器：文件头恰好切在多字节字符中间不算解码失败。
    """
    with path.open("rb") as handle:
        head = handle.read(4096)
    for enc in _ENCODINGS:
        decoder = codecs.getincrementaldecoder(enc)()
        try:
            decoder.decode(head)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"


def window_limit(value) -> int:
    try:
        asked = int(value)
    except (TypeError, ValueError):
        asked = WINDOW_BYTES
    return max(1, min(asked, WINDOW_BYTES))


def window_offset(value, total: int) -> int:
    try:
        asked = int(value)
    except (TypeError, ValueError):
        asked = 0
    return max(0, min(asked, max(0, int(total))))


def read_window(path: Path, offset: int = 0, limit: int = WINDOW_BYTES) -> dict:
    """只把一片读进内存，绝不 read_bytes() 整本。"""
    total = path.stat().st_size
    start = window_offset(offset, total)
    count = window_limit(limit)
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read(count)
    text, encoding, used = _decode_window(data)
    end = start + used
    return {
        "text": text,
        "encoding": encoding,
        "offset": start,
        "limit": used,
        "total": total,
        "has_prev": start > 0,
        "has_more": end < total,
        "next_offset": end if end < total else start,
        "prev_offset": max(0, start - count),
    }
