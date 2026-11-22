"""Markdown pages are the crystal. The program stores and shows them as written."""

from __future__ import annotations

from rogmem.pipeline_knobs import DEFAULTS as _KNOB_DEFAULTS

import html
import re

PAGE_TYPES = ("人物", "工程", "组织", "产品", "概念")

TYPE_GUIDE = (
    "一份材料里，离开它以后还会单独被找的主体，各自成一页。"
    "不要把整份文件收成一页，也不要用文件名当篇名。"
    "一个人，是人物。"
    "一家会继续存在的集体，是组织。"
    "一个已经有名字、被人拿来用的产物，是产品。模板、方法、制度本身不是产品。"
    "一个有自己的 git 仓库地址、以后还会被单独打开的工作，是工程。"
    "没有仓库地址的一项工作，写进所属的人或组织那一页，不要开成工程。"
    "一个换一份材料仍然成立的想法、方法或规矩，是概念。"
    "只在某一页里才成立的零件，留在那一页，不要另开："
    "组织自己的制度、产品里的角色或功能、工程里只在这里生效的规矩、概念下的单条判据。"
    "地点写在相关的人或组织上，不单独成篇。"
    "写记忆主人本人时，篇名用「记忆主人」，不要改成真人姓名。"
    "这是开篇和分类的判断，不是栏目。节名、写哪些、写多少，由你看着材料决定。"
    "材料里没有的不要编。写进正文的，就原样留下。"
)

SKETCH_GUIDE = (
    "这一页是一个还会被单独用到的主体。"
    "不用就会把这个主体认错或用错的名字，照正文写上。"
    "它和别人之间还在生效、漏了就会用错对象的关系，写到下一手不会找错。"
)

PERSON_SKETCH = (
    "这一页是一个还会被单独用到的人，不是记忆主人。\n"
    "不用就会认错的称呼和别名，照正文写。\n"
    "写清他和记忆主人现在是什么关系，以及打交道时会改变做法的事。"
    "比如现在怎么合作、什么不要误会。已经结束的关系不要写成还在。"
)

ORG_SKETCH = (
    "这一页是一个还会被单独用到的组织。\n"
    "现在的名字，以及不用就会认错的曾用名，照正文写。\n"
    "写清记忆主人和它现在是什么关系。"
    "比如人在里面、在合作，还是已经离开；还有没有还算数的来往。\n"
    "只在某一段经历里出现的内部科室，不要写成另一家独立组织。"
)

PRODUCT_SKETCH = (
    "这一页是一个还会被单独用到的产品。\n"
    "现在的名字，以及不用就会认错的曾用名，照正文写。\n"
    "写清它被人拿来做什么，以及现在还在不在用。"
    "比如正在用、还在做，或只是提案。功能沿革放在最后。"
)

CONCEPT_SKETCH = (
    "这一页是一个还会被单独用到的概念。\n"
    "写清它在指导哪一步。"
    "比如遇到什么情况就按它做，什么情况不要用，主人已经把它说定成什么。\n"
    "只复述定义、不写适用边界的，下一手用不上。"
)

OWNER_SKETCH = (
    "这一页是记忆主人。\n"
    "认错人、用错称呼，下一手就会写错。名字和别名照正文写。\n"
    "会改变每一轮协作的，写具体。"
    "比如沟通偏好：话要短还是要展开；决策偏好：先看什么、怎样才算定了；禁忌：什么不要做；和智能体的约定：以后都这样、做之前要不要先问。"
)

PROJECT_SKETCH = (
    "这一页是一个工程，交给以后在这里干活的智能体。\n"
    "不能改的约定带着正文里的原值写，不要收成一句空话。"
    "比如入口或仓库地址、不能改的名字、不能删的数据、碰了就会错的限制。\n"
    "试过又否决的做法留一句，写清当时卡在哪，免得照着再做一遍。\n"
    "做到哪、下一步、卡在哪，写成眼下能接手的状态。\n"
    "不要把整份沿革、做法或改动抄进来。旧做法只在还会被当成现行时留一句。"
)


def canonical_page_type(raw: str) -> str:
    """The stored type. 项目 is the old name of 工程."""
    text = (raw or "").strip()
    if text == "项目":
        return "工程"
    return text


def _meta_type(line: str) -> str:
    """A line that only names the page type, with or without heading marks."""
    text = (line or "").strip()
    while text.startswith("#"):
        text = text[1:].strip()
    if text.startswith("类型：") or text.startswith("类型:"):
        return canonical_page_type(text.split("：", 1)[-1].split(":", 1)[-1].strip())
    return ""


def parse_pages(text: str) -> list[dict]:
    """Split a model reply into pages. Each page starts with one `# 篇名` line."""
    body = (text or "").strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*", "", body)
        body = re.sub(r"\s*```$", "", body).strip()
    if not re.search(r"(?m)^# ", body):
        return []
    pages = []
    for part in re.split(r"(?m)(?=^# )", body):
        part = part.strip()
        if not part.startswith("# ") or part.startswith("## "):
            continue
        first, _, rest = part.partition("\n")
        name = first[2:].strip()
        if not name or name.startswith("#"):
            continue
        named_type = _meta_type(first)
        kept = []
        ctype = named_type
        for line in rest.splitlines():
            meta = _meta_type(line)
            if meta and not ctype:
                ctype = meta
                continue
            if meta and named_type:
                continue
            kept.append(line)
        if named_type:
            if pages:
                if not pages[-1]["type"]:
                    pages[-1]["type"] = named_type
                extra = "\n".join(kept).strip()
                if extra:
                    pages[-1]["body"] = (pages[-1]["body"] + "\n\n" + extra).strip()
            continue
        pages.append({"name": name, "type": ctype, "body": "\n".join(kept).strip()})
    return pages


_CITE_DEF = re.compile(r"^\[\^([^\]]+)\]:\s*(.*)$")

# A paragraph's provenance, written by the program at delivery: ` 〔源 3f2a9c1e 7b0d11aa〕`.
# Each id is a unique prefix of sources.id. It never uses the `[^n]` footnote syntax.
SOURCE_MARK = re.compile(r"[ \t]*〔源((?:[ \t]+[0-9A-Za-z_-]{1,64})+)[ \t]*〕")
SOURCE_MARK_CAP = _KNOB_DEFAULTS["provenance_mark_cap"]


def source_marks(text: str) -> list[str]:
    """Short source ids in the order the marks name them, once each."""
    found: list[str] = []
    for match in SOURCE_MARK.finditer(text or ""):
        for short in match.group(1).split():
            if short not in found:
                found.append(short)
    return found


def strip_source_marks(text: str) -> str:
    return SOURCE_MARK.sub("", text or "")


def source_mark(ids: list[str]) -> str:
    kept = [item for item in ids if item][-SOURCE_MARK_CAP:]
    return f" 〔源 {' '.join(kept)}〕" if kept else ""


_BLOCK_GAP = re.compile(r"(\n(?:[ \t]*\n)+)")
_HEAD_LINE = re.compile(r"^#{1,6}\s")
_RULE_LINE = re.compile(r"^(?:[-*_]\s*){3,}$")
_WORD = re.compile(r"[^\W_]", re.UNICODE)
# A changed paragraph inherits the marks of the old paragraph it most resembles (bigram Dice).
_PREDECESSOR_SIMILARITY = 0.5


def _mark_only(line: str) -> bool:
    return bool(SOURCE_MARK.search(line or "")) and not strip_source_marks(line).strip()


def _structural(line: str) -> bool:
    """Headings, alias and type lines, note definitions, tables, rules, and lines with no words."""
    text = (line or "").strip()
    if not text or _HEAD_LINE.match(text) or text.startswith("|") or _RULE_LINE.match(text):
        return True
    if _CITE_DEF.match(text) or re.match(r"^\[\^[^\]]+\]：", text):
        return True
    probe = text.lstrip("#").strip()
    if probe.startswith(("别名：", "别名:", "类型：", "类型:")):
        return True
    rest = re.sub(r"\[\[[^\[\]\n]+\]\]|\[\^[^\]]+\]", "", strip_source_marks(text))
    return _WORD.search(rest) is None


def _unit_key(text: str) -> str:
    return re.sub(r"\s+", " ", strip_source_marks(text)).strip()


def _grams(key: str) -> set[str]:
    flat = re.sub(r"\s+", "", key)
    return {flat[i : i + 2] for i in range(len(flat) - 1)}


def _units(text: str) -> list[tuple[str, list[str]]]:
    """Each blank-line paragraph that carries words: its comparison key and its marks."""
    found: list[tuple[str, list[str]]] = []
    fenced = False
    for block in _BLOCK_GAP.split(text or "")[::2]:
        lines = block.split("\n")
        fences = sum(1 for line in lines if line.strip().startswith("```"))
        if fenced or fences:
            fenced = fenced ^ (fences % 2 == 1)
            continue
        content = [line for line in lines if not _structural(line)]
        if not content:
            continue
        marked = [line for line in lines if not _structural(line) or _mark_only(line)]
        found.append((_unit_key("\n".join(content)), source_marks("\n".join(marked))))
    return found


def _ordered(ids: list[str]) -> list[str]:
    """Oldest first, a repeated id keeps its latest place."""
    out: list[str] = []
    for item in ids:
        if item in out:
            out.remove(item)
        out.append(item)
    return out


def carry_source_marks(before: str, after: str, attribute=None, cap: int = SOURCE_MARK_CAP) -> str:
    """Give every paragraph of ``after`` its source marks.

    A paragraph whose words are already on ``before`` keeps the marks it had there, even
    when the writer dropped them. A new or changed paragraph keeps the marks of the old
    paragraph it most resembles and gets ``attribute(text)`` added at the end. Only the
    ``cap`` most recent ids stay. Marks that ``before`` never carried are not trusted.
    Headings, alias lines, note definitions and code are never marked.
    """
    old = _units(before)
    known = set(source_marks(before))
    exact: dict[str, list[str]] = {}
    index: dict[str, list[int]] = {}
    old_grams: list[set[str]] = []
    for at, (key, marks) in enumerate(old):
        exact[key] = _ordered(exact.get(key, []) + marks)
        grams = _grams(key)
        old_grams.append(grams)
        for gram in grams:
            index.setdefault(gram, []).append(at)

    def nearest(key: str) -> list[str]:
        grams = _grams(key)
        if len(grams) < 3:
            return []
        shared: dict[int, int] = {}
        for gram in grams:
            for at in index.get(gram, ()):
                shared[at] = shared.get(at, 0) + 1
        best, score = None, 0.0
        for at, count in shared.items():
            value = 2 * count / (len(grams) + len(old_grams[at]))
            if value > score:
                best, score = at, value
        if best is None or score < _PREDECESSOR_SIMILARITY:
            return []
        return list(old[best][1])

    pieces = _BLOCK_GAP.split(after or "")
    fenced = False
    for at in range(0, len(pieces), 2):
        lines = pieces[at].split("\n")
        fences = sum(1 for line in lines if line.strip().startswith("```"))
        if fenced or fences:
            fenced = fenced ^ (fences % 2 == 1)
            continue
        content = [j for j, line in enumerate(lines) if not _structural(line)]
        if not content:
            continue
        loose = {j for j, line in enumerate(lines) if _mark_only(line)}
        written = [
            mark
            for mark in source_marks("\n".join(lines[j] for j in sorted(set(content) | loose)))
            if mark in known
        ]
        key = _unit_key("\n".join(lines[j] for j in content))
        if key in exact:
            marks = _ordered(exact[key] + written)
        else:
            fresh = [item for item in (attribute(key) if attribute else []) if item]
            marks = _ordered(nearest(key) + written + fresh)
        marks = marks[-cap:] if cap > 0 else []
        last = content[-1]
        shaped: list[str] = []
        for j, line in enumerate(lines):
            if j in loose:
                continue
            if j in content and SOURCE_MARK.search(line):
                line = strip_source_marks(line).rstrip()
            if j == last and marks:
                line = line.rstrip() + source_mark(marks)
            shaped.append(line)
        pieces[at] = "\n".join(shaped)
    return "".join(pieces)


def cite_notes(text: str) -> dict[str, str]:
    """Footnote definitions are the citation payload, not part of the reading text."""
    notes: dict[str, str] = {}
    for line in (text or "").splitlines():
        match = _CITE_DEF.match(line.strip())
        if match:
            notes[match.group(1)] = match.group(2).strip()
    return notes


def render_markdown(text: str, targets: dict | None = None, sources: dict | None = None) -> str:
    """Preview the page as written. Citation notes become a hover mark, not a second copy of the sentence.

    ``sources`` maps a short id in a 〔源 …〕 mark to the full source id it links to.
    """
    targets = targets or {}
    notes = cite_notes(text)
    sources = sources or {}
    blocks: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        blocks.append("<p>" + _inline(" ".join(paragraph), targets, notes, sources) + "</p>")
        paragraph.clear()

    def flush_list() -> None:
        if not list_items:
            return
        blocks.append("<ul>" + "".join(f"<li>{item}</li>" for item in list_items) + "</ul>")
        list_items.clear()

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush_paragraph()
            flush_list()
            continue
        if line.startswith("### "):
            flush_paragraph()
            flush_list()
            title = line[4:].strip()
            blocks.append(f"<h3 id='{_esc(title)}'>{_inline(title, targets, notes, sources)}</h3>")
            continue
        if line.startswith("## "):
            flush_paragraph()
            flush_list()
            title = line[3:].strip()
            blocks.append(f"<h2 id='{_esc(title)}'>{_inline(title, targets, notes, sources)}</h2>")
            continue
        if line.startswith("# "):
            flush_paragraph()
            flush_list()
            title = line[2:].strip()
            blocks.append(f"<h2 id='{_esc(title)}'>{_inline(title, targets, notes, sources)}</h2>")
            continue
        if _CITE_DEF.match(line.strip()):
            flush_paragraph()
            flush_list()
            continue
        if line.lstrip().startswith(("- ", "* ")):
            flush_paragraph()
            list_items.append(_inline(line.lstrip()[2:], targets, notes, sources))
            continue
        flush_list()
        paragraph.append(line.strip())
    flush_paragraph()
    flush_list()
    return "".join(blocks) or f"<p>{_esc(text or '')}</p>"


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def citation_passage(note: str) -> str:
    """The words a reader should see in the mark. Empty when the note has none."""
    text = re.sub(r"/admin/sources/\S+", "", note or "").strip()
    if text.startswith("实") or text.startswith("推"):
        text = text[1:].strip()
    quoted = re.search(r"「([^」]*)」", text)
    if quoted:
        text = quoted.group(1).strip()
    return text.strip(" 「」")


def _cite_html(note: str) -> str:
    """A mark without a passage is not a citation. Do not open an empty card."""
    passage = citation_passage(note)
    if not passage:
        return ""
    kind = "推" if (note or "").startswith("推") else "实"
    link = ""
    found = re.search(r"(/admin/sources/[A-Za-z0-9_-]+)", note or "")
    if found:
        link = f"<a href='{_esc(found.group(1))}'>打开出处</a>"
    return (
        f"<sup class='cite' tabindex='0'>{kind}"
        f"<span class='cite-card'><b>{kind}</b><q>{_esc(passage)}</q>{link}</span></sup>"
    )


def _source_mark_html(ids: str, sources: dict) -> str:
    links = []
    for short in ids.split():
        full = str(sources.get(short) or short)
        links.append(f"<a href='/admin/sources/{_esc(full)}' title='打开原文 {_esc(full)}'>{_esc(short[:8])}</a>")
    return " <sup class='cite src'>源 " + " ".join(links) + "</sup>"


def _inline(text: str, targets: dict, notes: dict | None = None, sources: dict | None = None) -> str:
    notes = notes or {}
    sources = sources or {}
    parts: list[str] = []
    cursor = 0
    pattern = re.compile(
        r"\[\[([^\[\]\n]{1,80})\]\]|\*\*([^*]+)\*\*|\[\^([^\]]+)\]|" + SOURCE_MARK.pattern
    )
    for match in pattern.finditer(text or ""):
        parts.append(_esc(text[cursor : match.start()]))
        if match.group(4) is not None:
            parts.append(_source_mark_html(match.group(4), sources))
        elif match.group(1) is not None:
            label = match.group(1).strip()
            dest = targets.get(label)
            if dest:
                parts.append(f"<a href='/admin/crystals/{_esc(dest)}'>{_esc(label)}</a>")
            else:
                parts.append(_esc(match.group(0)))
        elif match.group(2) is not None:
            parts.append(f"<strong>{_esc(match.group(2))}</strong>")
        else:
            parts.append(_cite_html(notes.get(match.group(3), "")))
        cursor = match.end()
    parts.append(_esc((text or "")[cursor:]))
    return "".join(parts)
