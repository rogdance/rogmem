"""Shared admin loading motion: top bar, content enter, list partial busy."""

from rogmem.ui import _LIST_PARTIAL_SCRIPT, page


def test_page_shell_ships_shared_loading_motion():
    html = page("关于", "about", "<h1>关于</h1><p>hello</p>")
    assert 'id="rogmem-progress"' in html
    assert "RogmemLoad" in html
    assert "rogmem-progress-run" in html
    assert ".content.rogmem-enter" in html or "rogmem-enter" in html
    assert ".list-busy" in html
    assert "prefers-reduced-motion" in html
    assert "RogmemLoad.start" in html
    assert "RogmemLoad.done" in html


def test_list_partial_refresh_marks_list_busy():
    assert "RogmemLoad.list" in _LIST_PARTIAL_SCRIPT or "list-busy" in _LIST_PARTIAL_SCRIPT
    assert "aria-busy" in _LIST_PARTIAL_SCRIPT


def test_post_uses_shared_navigation_progress():
    html = page("关于", "about", "<h1>关于</h1>")
    # Shared post() must start the bar before reload, and clear it on failure.
    post_block = html.split("async function post(", 1)[1].split("function confirmDelete", 1)[0]
    assert "RogmemLoad.start" in post_block
    assert "RogmemLoad.done" in post_block
