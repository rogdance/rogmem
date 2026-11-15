"""记忆主人词条用锁标识说明，不再在标题下写说明句。"""

from rogmem.ui import crystal_page


def _detail(cid: str, name: str) -> dict:
    return {
        "id": cid,
        "type": "人物",
        "name": name,
        "status": "active",
        "status_label": "活跃",
        "summary": {"真名": "尚无来源", "概述": "尚无来源"},
        "chapters": {},
        "field_marks": {},
        "chapter_marks": {},
        "lede": [{"text": "尚无来源", "marks": []}],
        "chains": {},
    }


def test_owner_page_shows_lock_tooltip_not_footer_line():
    tip = "本词条是专为记忆主人预制的特殊词条，系统将自动维护，无法删除。"
    html = crystal_page(_detail("owner", "记忆主人"), [], [_detail("owner", "记忆主人")], "", "active")
    assert "这篇就是记忆主人" not in html
    assert f"<span class='owner-lock' title='{tip}'" in html
    assert "<h1>记忆主人<span class='owner-lock'" in html


def test_lede_uses_the_column_and_the_left_rule_fills_the_page():
    html = crystal_page(_detail("c1", "周生生"), [], [{"id": "c1", "name": "周生生"}], "", "active")
    style = html.split("</style>", 1)[0]
    assert "max-width:42em" not in style
    assert ".lede { display:block; width:100%;" in style
    assert ".wiki > aside.wiki-side { align-self:stretch; min-height:0; max-height:none; overflow:auto; }" in style
    assert "grid-template-columns:252px minmax(0,1fr) 200px" not in style
    assert "toc-dock" in html
    assert "class='toc-dock open'" not in html
    assert "aria-expanded='false'" in html
    modal = style.split(".modal {", 1)[1].split("}", 1)[0]
    dock = style.split(".toc-dock {", 1)[1].split("}", 1)[0]
    tip = style.split(".lamp-pop {", 1)[1].split("}", 1)[0]
    assert int(modal.split("z-index:")[1].split(";")[0]) > int(dock.split("z-index:")[1].split(";")[0])
    assert int(modal.split("z-index:")[1].split(";")[0]) > int(tip.split("z-index:")[1].split(";")[0])


def test_toc_panel_splits_contents_links_and_backlinks():
    detail = _detail("c1", "周生生")
    detail["body_md"] = "## 沿革\n\n一段说明。"
    detail["sketch_md"] = "速写一句。"
    detail["link_targets"] = {}
    detail["out_links"] = [{"id": "org", "name": "南港书院"}]
    detail["in_links"] = [{"id": "person", "name": "云岚甲"}]
    html = crystal_page(detail, [], [detail], "", "active")
    panel = html.split("<div class='toc-panel'>", 1)[1].split("</aside>", 1)[0]
    groups = panel.split("<section class='toc-group'")
    assert len(groups) == 4
    assert "目录" in groups[1] and "沿革" in groups[1] and "南港书院" not in groups[1]
    assert "链接" in groups[2] and "南港书院" in groups[2] and "反向链接" not in groups[2]
    assert "反向链接" in groups[3] and "云岚甲" in groups[3]
    style = html.split("</style>", 1)[0]
    gap = style.split(".toc-group + .toc-group {", 1)[1].split("}", 1)[0]
    assert "margin-top" in gap
    label = style.split(".toc-group .wiki-label {", 1)[1].split("}", 1)[0]
    assert "font-weight:600" in label.replace(" ", "")
    assert "border-bottom" in label


def test_other_crystal_has_no_lock():
    html = crystal_page(
        _detail("c1", "周生生"),
        [],
        [{"id": "c1", "name": "周生生"}],
        "",
        "active",
    )
    assert "<span class='owner-lock'" not in html
    assert "无法删除" not in html
