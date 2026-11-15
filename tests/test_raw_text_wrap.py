"""原文解析在会话详情和文件详情必须自动换行。

根因：源详情里 `<pre>` 没有专属换行样式，继承浏览器默认 `white-space: pre`
导致长无空格字符串（长 URL、长 token、超长中文行）和保留换行一起把容器撑破，
造成右侧溢出。

契约（专属样式，不动全局 code / pre）：

- 源详情里的 `<pre>` 必须带独立类名 `src-body`，便于只针对原文展示打补丁。
- 页面 CSS 必须给 `.src-body` 写出三样东西：`white-space: pre-wrap`、
  `overflow-wrap: anywhere`（破长 URL/长 token），以及约束容器内宽度（
  `max-width: 100%` 或 `min-width: 0`）。
- 不得为了修这个问题把换行行为写到全局 `pre {}` 规则里，别处 `<pre>`（例如
  未来可能的日志展示、代码块）不应被动改变。
- 文本内容必须原样回显，只有 HTML 转义（> & < 等）—— 不得把换行翻成 <br>，
  以免破坏复制时的原始格式；也不得用 overflow:hidden 掩盖。
- 原来带高亮 `<mark id='cite-...'>` 的分支，`<mark>` 必须仍然在
  `<pre class='src-body'>` 里面，避免高亮脱出换行上下文；并且 <mark> 不得改变
  原文的字符构成（textContent 等价）。
"""

import html as _html
import re

from rogmem.ui import source_detail


LONG_URL = "https://example.com/" + "a" * 400
LONG_HASH = "x" * 500
CJK_RUN = "一段很长的中文记录" * 60


def _row_with_body(body: str) -> dict:
    return {
        "id": "s1",
        "kind": "file",
        "filename": "note.md",
        "body": body,
        "status": "蒸馏已结晶",
        "_part": "text",
        "_cited": [],
    }


def _pre_text_content(page_html: str) -> str:
    """模拟 pre.textContent：抠出 <pre class='src-body'>…</pre> 范围，剥所有标签，
    再 HTML 反转义。等价于真实浏览器下 `pre.textContent`（对本页标签集足够）。"""
    inner = page_html.split("<pre class='src-body'>", 1)[1].split("</pre>", 1)[0]
    stripped = re.sub(r"<[^>]+>", "", inner)
    return _html.unescape(stripped)


def test_file_detail_pre_uses_src_body_class_and_preserves_content():
    body = f"第一行文本。\n{LONG_URL}\n第三行里有 {LONG_HASH} 这样长的无空格串。\n{CJK_RUN}"
    html_text = source_detail(_row_with_body(body), [], None, "file")
    assert "<pre class='src-body'>" in html_text
    # 复制语义：pre.textContent 必须和原始 body 严格相等（无 <br>、无 overflow 掩盖）
    assert _pre_text_content(html_text) == body


def test_session_detail_also_uses_src_body_class_and_text_equals_body():
    body = f"会话记录：{LONG_URL}\n{CJK_RUN}\n结束。"
    html_text = source_detail(_row_with_body(body) | {"kind": "user"}, [], None, "session")
    assert "<pre class='src-body'>" in html_text
    assert _pre_text_content(html_text) == body


def test_marked_citation_keeps_pre_src_body_wrapper_and_text_equals_body():
    body = f"开头。{LONG_URL}。中间引文：被引用的那一句。结尾 {CJK_RUN}"
    row = _row_with_body(body) | {"_cite": "st-1"}
    statements = [{"id": "st-1", "quote": "被引用的那一句。"}]
    html_text = source_detail(row, statements, None, "file")
    pre_block = html_text.split("<pre class='src-body'>", 1)[1].split("</pre>", 1)[0]
    assert "<mark id='cite-st-1'>" in pre_block
    assert "被引用的那一句。" in pre_block
    # 高亮分支下，<mark> 不得改变 pre.textContent 的字符构成
    assert _pre_text_content(html_text) == body


def test_pre_text_content_preserves_html_sensitive_chars():
    """`<`、`>`、`&`、单双引号这些敏感字符经 esc 转义写进 HTML 后，
    `pre.textContent` 仍要能还原成原始字符，证明复制内容零失真。"""
    body = "含 < > & \" ' 这些字符的句子\n下一行 <tag>text</tag>"
    html_text = source_detail(_row_with_body(body), [], None, "file")
    assert _pre_text_content(html_text) == body


def test_page_css_declares_src_body_wrap_rules():
    html = source_detail(_row_with_body("x"), [], None, "file")
    css = html.split("<style>", 1)[1].split("</style>", 1)[0]
    normalized = " ".join(css.split())
    # 必须声明 .src-body 的三件套；允许在同一个规则块里
    import re

    block_pattern = re.compile(r"\.src-body\s*\{[^}]*\}")
    blocks = block_pattern.findall(normalized)
    assert blocks, "没有为 .src-body 声明换行规则"
    merged = " ".join(blocks)
    assert "white-space: pre-wrap" in merged
    assert "overflow-wrap: anywhere" in merged
    # 必须带至少一种宽度约束
    assert ("max-width: 100%" in merged) or ("min-width: 0" in merged)


def test_global_pre_rule_is_not_mutated_into_wrap():
    """不得把 pre-wrap 塞进全局 `pre {}` 规则里，别处 <pre> 行为不受影响。"""
    html = source_detail(_row_with_body("x"), [], None, "file")
    css = html.split("<style>", 1)[1].split("</style>", 1)[0]
    normalized = " ".join(css.split())
    # 现有全局是 `code, kbd, pre, samp { font-family: ...; }`
    # 规则本身允许存在，但不允许在里面塞 white-space 控制
    import re

    for match in re.finditer(r"([^{};]*)\{([^}]*)\}", normalized):
        selector, decls = match.group(1).strip(), match.group(2)
        # 只看含裸 `pre`（非 `.xxx pre` 选择器本意是全局 pre）的规则
        parts = [s.strip() for s in selector.split(",")]
        has_bare_pre = any(
            part == "pre" or part.startswith("pre ") or part.endswith(" pre") or part == "code, kbd, pre, samp"
            for part in parts
        )
        bare_pre = any(part == "pre" for part in parts)
        if bare_pre or "code, kbd, pre, samp" in selector:
            assert "pre-wrap" not in decls, f"全局 pre 规则被塞了 pre-wrap: {selector}"
            assert "overflow-wrap" not in decls, f"全局 pre 规则被塞了 overflow-wrap: {selector}"
        del has_bare_pre
