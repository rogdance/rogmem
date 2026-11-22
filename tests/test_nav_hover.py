"""顶栏有下级的项悬停展开。没有下级的项仍是原来的一级链接。"""

from __future__ import annotations

import re

from rogmem.ui import CONSOLE, page


def _render(active: str = "crystals") -> str:
    return page("标题", active, "<p>demo</p>")


def _desktop(html: str) -> str:
    header = html.split("<header>", 1)[1].split("</header>", 1)[0]
    return header.split("<nav>", 1)[1].split("</nav>", 1)[0]


def _primary(desktop: str) -> str:
    return re.sub(r"<span class='nav-sub'[\s\S]*?</span></span>", "", desktop)


def test_only_sections_with_children_grow_a_submenu():
    desktop = _desktop(_render())
    primary = _primary(desktop)
    assert desktop.count("class='nav-item'") == 2
    assert "监控中心" in primary and "系统设置" in primary
    for href, label, _key, children in CONSOLE:
        if children:
            assert f">{label}</a>" in primary
            block = desktop.split(f">{label}</a>", 1)[1].split("class='nav-item'", 1)[0]
            for child_href, child_label, _child_key in children:
                assert f"href='{child_href}'>{child_label}</a>" in block
                assert child_label not in primary
        else:
            assert f"href='{href}'>{label}</a>" in primary
            assert f">{label}</a><span class='nav-sub'" not in desktop
    assert "aria-haspopup='menu'" in desktop
    assert desktop.count("aria-haspopup='menu'") == 2
    assert "tabindex='-1'" in desktop


def test_primary_click_target_stays_on_the_section_href():
    desktop = _desktop(_render("queue"))
    assert "class='on' href='/admin/costs'>监控中心" in desktop
    assert "class='on' href='/admin/settings/queue'>队列状态" in desktop
    assert "aria-expanded='false'" in desktop


def test_submenu_reuses_the_existing_menu_surface_and_does_not_restyle_the_bar():
    style = _render().split("<style>", 1)[1].split("</style>", 1)[0]
    assert "header nav a {" in style or "header nav a {{" in style or "header nav a {" in style.replace("  ", " ")
    compact = " ".join(style.split())
    assert "header nav a { display: inline-flex;" in compact
    assert "min-height: 36px; padding: 8px 10px;" in compact
    assert "font-weight: 500;" in compact.split("header nav .nav-item", 1)[0]
    sub = compact.split("header nav .nav-sub a {", 1)[1].split("}", 1)[0]
    assert "font-weight: 400" in sub
    assert "min-height: 32px" in sub
    assert "padding: 4px 10px" in sub
    panel = compact.split("header nav .nav-sub-panel {", 1)[1].split("}", 1)[0]
    assert "var(--surface-popover)" in panel
    assert "var(--border)" in panel
    assert "border-radius: 8px" in panel
    assert "var(--shadow-menu)" in panel
    assert "padding: 4px" in panel
    bridge = compact.split("header nav .nav-sub {", 1)[1].split("}", 1)[0]
    assert "top: 100%" in bridge
    assert "padding-top: 4px" in bridge
    assert "background: transparent" in bridge
    desktop = compact.split("@media (min-width: 861px)", 1)[1].split("@media", 1)[0]
    assert "header > nav { overflow: visible; }" in desktop


def test_keyboard_path_is_added_without_taking_the_tab_sequence():
    script = _render().split("<script>", 2)[2].split("</script>", 1)[0]
    assert 'querySelectorAll("header > nav .nav-item")' in script
    assert 'event.key === "ArrowDown"' in script
    assert 'event.key === "Escape"' in script
    assert "pointerenter" in script and "pointerleave" in script
    drawer = _render().split('<nav class="nav-drawer-list"', 1)[1].split("</nav>", 1)[0]
    assert "成本明细" not in drawer
    assert "基础设置" not in drawer


def test_plain_pages_keep_a_single_primary_link():
    desktop = _desktop(_render("graph"))
    assert "class='on' href='/admin/graph'>记忆星图" in desktop
    assert "<nav class='side'>" not in _render("graph")
    assert "nav-sub-monitor" in desktop and "nav-sub-settings" in desktop
