"""结晶导航可以收成贴边悬浮窗，把横向空间还给正文。"""

from rogmem.ui import crystal_page


def _detail() -> dict:
    return {
        "id": "c1",
        "type": "人物",
        "name": "云岚甲",
        "status": "active",
        "status_label": "活跃",
        "summary": {},
        "chapters": {},
        "field_marks": {},
        "chapter_marks": {},
        "lede": [],
        "chains": {},
        "body_md": "# 云岚甲\n\n正文留在中间。\n",
        "sketch_md": "",
    }


def test_nav_fold_button_sits_on_the_right_of_the_label():
    html = crystal_page(_detail(), [], [{"id": "c1", "name": "云岚甲"}], "", "active")
    head = html.split("class='nav-head'", 1)[1].split("</div>", 1)[0]
    assert "结晶导航" in head
    assert head.index("结晶导航") < head.index("id='crystal-nav-fold'")
    assert "aria-expanded='true'" in head
    assert "aria-controls='crystal-nav-body'" in head
    assert "aria-label='收起导航'" in head
    assert "fold-close" in head
    assert ">收起</button>" not in head
    assert "id='crystal-nav'" in html
    assert "rogmem-crystal-nav" in html
    style = html.split("</style>", 1)[0]
    assert "html.nav-collapsed .wiki, html.nav-collapsed .wiki.solo { grid-template-columns: minmax(0, 1fr); }" in style
    assert "html.nav-collapsed.nav-dock-right .wiki > aside.wiki-side { left: auto; right: 0; }" in style
    assert "html.nav-collapsed #crystal-nav-body { display: none; }" in style


def _rules(style: str) -> list[tuple[str, str]]:
    rules = []
    for chunk in style.split("}"):
        if "{" not in chunk:
            continue
        selector, body = chunk.rsplit("{", 1)
        rules.append((" ".join(selector.split()), " ".join(body.split())))
    return rules


def test_collapsed_nav_tab_reads_nav_like_the_toc_tab():
    """收起后贴边按钮写成「导航」，和右侧「目录」共用同一套竖排悬浮样式。"""
    html = crystal_page(_detail(), [], [{"id": "c1", "name": "云岚甲"}], "", "active")
    button = html.split("id='crystal-nav-fold'", 1)[1].split("</button>", 1)[0]
    assert "<span class='fold-name'>导航</span>" in button
    assert "fold-open" not in button
    style = html.split("</style>", 1)[0]
    assert ".toc-dock button.toc-tab,\nhtml.nav-collapsed button.nav-fold {" in style
    assert "html.nav-collapsed button.nav-fold svg { display: none; }" in style
    assert "html.nav-collapsed button.nav-fold .fold-name { display: block; }" in style
    assert "button.nav-fold .fold-name { display: none; }" in style


def test_open_nav_meets_the_header_and_the_title_moves_into_the_column():
    """展开时左栏贴到顶栏下沿。词条名仍离顶栏 24px，但让进中间栏，不占左栏。"""
    html = crystal_page(_detail(), [], [{"id": "c1", "name": "云岚甲"}], "", "active")
    sheet = html.split("<div class='reader-sheet'>", 1)[1]
    assert sheet.startswith("<div class='wiki solo'>")
    assert sheet.index("<aside class='wiki-side'>") < sheet.index("<div class='wiki-main'>")
    column = sheet.split("<div class='wiki-main'>", 1)[1].split("<article class='wiki-article'>", 1)[0]
    assert column.startswith("<div class='entry-head'><h1>云岚甲")
    style = " ".join(html.split("</style>", 1)[0].split())
    assert "body.reader-screen { --page-pad: 24px;" in style
    assert "body.reader-screen main.reader { display: flex; flex-direction: column; flex: 1; height: auto; min-height: 0; align-items: stretch; padding: 0 var(--page-pad) 48px 0; }" in style
    assert ".reader-sheet .wiki, .reader-sheet .wiki.solo { grid-template-columns: calc(252px + var(--page-pad)) minmax(0, 1fr); }" in style
    assert "html:not(.nav-collapsed) .reader-sheet .wiki-side { padding-left: calc(12px + var(--page-pad)); }" in style
    assert ".reader-sheet .wiki-main { display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; padding-top: var(--page-pad); padding-left: var(--page-pad); }" in style
    assert "html.nav-collapsed .reader-sheet .wiki-main { padding-left: var(--page-pad); }" in style
    assert ".entry-head h1 { display: flex; align-items: center; gap: 8px; margin: 0; }" in style
    assert "h1 { font-size: 28px; line-height: 1.4;" in style


def test_open_nav_uses_the_same_left_inset_as_the_collapsed_column():
    """展开时中栏也离开导航的边线，左边距和收起后那一栏相同。"""
    html = crystal_page(_detail(), [], [{"id": "c1", "name": "云岚甲"}], "", "active")
    style = " ".join(html.split("</style>", 1)[0].split())
    open_rule = ".reader-sheet .wiki-main { display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; padding-top: var(--page-pad); padding-left: var(--page-pad); }"
    collapsed_rule = "html.nav-collapsed .reader-sheet .wiki-main { padding-left: var(--page-pad); }"
    assert open_rule in style
    assert collapsed_rule in style


def test_collapsed_nav_keeps_the_article_on_the_title_edge():
    """收起后导航离开网格。正文和标题共用同一条内容边，贴边按钮不得再改写正文内边距。"""
    html = crystal_page(_detail(), [], [{"id": "c1", "name": "云岚甲"}], "", "active")
    style = html.split("</style>", 1)[0]
    shifted = [
        (selector, body)
        for selector, body in _rules(style)
        if "nav-collapsed" in selector and "wiki-article" in selector and "padding" in body
    ]
    assert shifted == []
