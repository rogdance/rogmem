"""结晶正文的段级索引与规则路由（并页增量合并）。

把一篇长正文按 ``## 标题`` 切成段，给每段建一份基于词的索引；新材料进来时，
用规则（词面等价）把材料路由到「命中段」，供注入时只送命中段 + 其余段目录，
而不是整篇正文。

刻意不用向量：语义相似度不等于同名判等，路由必须是可解释、可回溯的规则匹配。

一次计算、落档复用：一段的「正文摘要词」在建索引时算好写进 ``crystal_sections``，
并页时直接读回用（带 hash 校验，正文对不上就退回现算），不必每次并页都重新切段
再逐段分词。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3

import jieba

HEADING_RE = re.compile(r"(?m)^##[ \t]+(.+?)[ \t]*$")
GIST_CHARS = 200
DEFAULT_LIMIT = 6
# 标题命中的权重（正文摘要命中记 1）。让短标题（如「验证」）不被长小节压下去。
HEADING_WEIGHT = 3
# 词面匹配只认两个字符以上的词。单字（面、体、的……）几乎不承载「这段讲什么」，
# 留着只会让任意材料都蹭上一批段（实测：无关材料靠单字命中把注入集撑大），
# 与「同名判等靠规则、可解释」同向——单字重叠不是可解释的等价信号。
MIN_TERM_LEN = 2
# ``tokens`` 列的口径版本。改分词口径（如把标题词从正文摘要里剔掉）必须 +1：
# 启动时据它判定要不要把存量行重建一次，之后不再全量重建。
INDEX_VERSION = 2
_EMPTY_HEADING = ""


def _norm(part: str) -> str:
    return part.strip().lower()


def split_sections(body: str) -> list[tuple[str, str]]:
    """按 ``## 标题`` 切段；首个标题之前的内容标题为 ""。全仓唯一的段切分实现。"""
    text = body or ""
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return [(_EMPTY_HEADING, text)]
    out: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        out.append((_EMPTY_HEADING, text[: matches[0].start()]))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        out.append((match.group(1).strip(), text[match.start():end]))
    return out


def terms(text: str) -> set[str]:
    """一段文本的规范词集：小写、去空白、只留两个字符以上的词。"""
    out: set[str] = set()
    for part in jieba.cut_for_search(text or ""):
        piece = _norm(part)
        if len(piece) >= MIN_TERM_LEN:
            out.add(piece)
    return out


def body_terms(heading: str, block: str) -> set[str]:
    """路由的「正文摘要命中」词集：正文前 GIST_CHARS 字里、且不在标题里的词。

    减掉标题词，是为了让标题命中与正文命中不重复计数——标题另有 HEADING_WEIGHT 权重。
    """
    from rogmem.pages_md import strip_source_marks

    return terms(strip_source_marks(block or "")[:GIST_CHARS]) - terms(heading)


def stored_tokens(heading: str, block: str) -> str:
    """落档成 ``crystal_sections.tokens`` 的正文词串：排序、空格分隔，可原样回读成集合。"""
    return " ".join(sorted(body_terms(heading, block)))


def tokens_to_terms(raw: str) -> set[str]:
    return set(str(raw or "").split())


def section_terms(heading: str, block: str) -> set[str]:
    """一段的粗口径词集（标题 + 正文摘要）。保留给外部/兼容用。"""
    return terms(f"{heading}\n{block or ''}")


def block_hash(block: str) -> str:
    return hashlib.sha1((block or "").encode("utf-8")).hexdigest()


def index_rows(body: str) -> list[dict]:
    rows = []
    for order, (heading, block) in enumerate(split_sections(body)):
        rows.append(
            {
                "ord": order,
                "heading": heading,
                "tokens": stored_tokens(heading, block),
                "chars": len(block or ""),
                "hash": block_hash(block),
            }
        )
    return rows


def toc(body: str) -> list[str]:
    return [heading for heading, _ in split_sections(body) if heading]


def pick_sections(
    text: str,
    sections: list[tuple[str, str]],
    limit: int = DEFAULT_LIMIT,
    body_sets: list[set[str]] | None = None,
) -> list[tuple[str, str]]:
    """规则路由：按材料词与段索引键的词面重合度排序，取前 limit 段。确定性、可回溯。

    标题命中权重高于正文摘要命中（HEADING_WEIGHT）：否则像「验证」这种短标题
    会输给「正文里碰巧提到验证」的长小节。

    ``body_sets`` 是每段的正文摘要词集（与 ``sections`` 同序）；给了就直接用，
    否则现算——两者同分，前者省去逐段分词。
    """
    wanted = terms(text)
    if not wanted:
        return []
    scored: list[tuple[int, int, str, str]] = []
    for order, (heading, block) in enumerate(sections):
        if not heading:
            continue
        head_terms = terms(heading)
        if body_sets is not None and order < len(body_sets) and body_sets[order] is not None:
            body = body_sets[order]
        else:
            body = terms((block or "")[:GIST_CHARS]) - head_terms
        head_overlap = len(wanted & head_terms)
        body_overlap = len(wanted & body)
        if not head_overlap and not body_overlap:
            continue
        scored.append((HEADING_WEIGHT * head_overlap + body_overlap, order, heading, block))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [(heading, block) for _, _, heading, block in scored[:limit]]


def heading_matches(text: str, sections: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """标题与材料有词面重合的段（只看标题）。保证"模型可能想改的段"一定被注入。"""
    wanted = terms(text)
    if not wanted:
        return []
    out: list[tuple[str, str]] = []
    for heading, block in sections:
        if heading and (wanted & terms(heading)):
            out.append((heading, block))
    return out


def load_body_term_sets(
    con: sqlite3.Connection, crystal_id: str, sections: list[tuple[str, str]]
) -> list[set[str]] | None:
    """读回落档的正文摘要词集，供路由省去现算。

    只有在索引与当前切段逐行对齐（序号、标题、块 hash 全同）时才返回，否则返回
    None 让调用方现算。正文被改过而索引还没重建时，宁可多算一次也不给出错的词集。
    """
    if not crystal_id:
        return None
    try:
        rows = con.execute(
            "SELECT ord, heading, tokens, hash FROM crystal_sections WHERE crystal_id=? ORDER BY ord",
            (crystal_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if len(rows) != len(sections):
        return None
    out: list[set[str]] = []
    for index, row in enumerate(rows):
        heading, block = sections[index]
        if int(row["ord"]) != index or str(row["heading"] or "") != heading:
            return None
        if str(row["hash"] or "") != block_hash(block):
            return None
        out.append(tokens_to_terms(row["tokens"]))
    return out


def sync_sections(con: sqlite3.Connection, crystal_id: str, body: str) -> None:
    """按当前正文重建某结晶的段级索引。表缺失时静默跳过（迁移前）。"""
    try:
        con.execute("DELETE FROM crystal_sections WHERE crystal_id=?", (crystal_id,))
        for row in index_rows(body):
            con.execute(
                "INSERT INTO crystal_sections(crystal_id, ord, heading, tokens, chars, hash)"
                " VALUES(?,?,?,?,?,?)",
                (crystal_id, row["ord"], row["heading"], row["tokens"], row["chars"], row["hash"]),
            )
    except sqlite3.OperationalError:
        return


def rebuild_section_index(con: sqlite3.Connection) -> int:
    """全量重建段级索引。存量词条接入或口径升版时用。"""
    try:
        con.execute("DELETE FROM crystal_sections")
    except sqlite3.OperationalError:
        return 0
    count = 0
    for row in con.execute("SELECT id, IFNULL(body_md,'') AS body_md FROM crystallizations"):
        sync_sections(con, row["id"], row["body_md"] or "")
        count += 1
    return count


def ensure_index_current(con: sqlite3.Connection, version: int = INDEX_VERSION) -> bool:
    """口径没变就不动索引；变了（或还没建过）就全量重建一次并记下版本。

    替掉「每次启动都全量重建」：存量词条只在首次/升版时付一次代价。
    """
    try:
        row = con.execute("SELECT v FROM settings WHERE k='section_index_version'").fetchone()
    except sqlite3.OperationalError:
        return False
    if row is not None and str(row[0]) == str(version):
        return False
    try:
        rebuild_section_index(con)
        con.execute(
            "INSERT INTO settings(k,v) VALUES('section_index_version',?)"
            " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (str(version),),
        )
    except sqlite3.OperationalError:
        return False
    return True
