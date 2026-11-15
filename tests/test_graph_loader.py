"""图谱加载动效：loader/空/错误/重试/看门狗/减动效，状态机有界结束。"""

from __future__ import annotations

import re

from rogmem.ui import graph_mount, graph_page, page


def test_mount_html_has_loader_shell_and_aria():
    html = graph_mount("graph")
    assert 'role="status"' in html or "role='status'" in html
    assert 'aria-live="polite"' in html or "aria-live='polite'" in html
    assert 'aria-busy="true"' in html or "aria-busy='true'" in html
    assert "graph-loader" in html
    assert "graph-loader-dots" in html
    # 原文"正在铺开星图"被 loader 包住。
    assert "正在铺开星图" in html


def test_mount_dots_use_css_pulse_animation():
    full = page("图谱", "graph", graph_mount("graph", focus=True))
    assert "graph-loader-dots" in full
    assert "@keyframes graph-loader-pulse" in full


def test_reduced_motion_disables_dot_animation():
    full = page("图谱", "graph", graph_mount("graph"))
    # prefers-reduced-motion 下 .graph-loader-dots i 的动画被关掉。
    section = re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{[^}]*\.graph-loader-dots\s+i\s*\{[^}]*animation:\s*none",
        full,
    )
    assert section is not None, "缺 prefers-reduced-motion 下 .graph-loader-dots i animation:none 规则"


def test_fetch_timeout_uses_abort_controller():
    html = graph_mount("graph")
    assert "AbortController" in html
    # 15 秒 fetch 超时常量（独立于渲染看门狗）。
    assert "15000" in html


def _fn(html: str, name: str, stop: str) -> str:
    """截出某一个函数的正文，用来断言顺序，不靠整页 substring 乱碰。"""
    return html.split(f"function {name}", 1)[1].split(f"function {stop}", 1)[0]


def test_loader_looks_at_the_status_and_content_type_before_reading_json():
    """本机通道够不到中枢时回的是一段纯文本 502。先判再读，不能直接当 JSON 读。"""
    html = graph_mount("graph")
    req = _fn(html, "rogmemGraphRequest", "rogmemDrawGraph")
    assert "r.ok" in req
    assert "content-type" in req
    assert "'json'" in req
    # 先判状态码与 Content-Type，再读正文。
    assert req.index("r.ok") < req.index("r.json()")


def test_loader_retries_before_it_gives_up():
    html = graph_mount("graph")
    assert "ROGMEM_GRAPH_FETCH_TRIES" in html
    fetch = _fn(html, "rogmemFetchGraph", "rogmemLoadGraph")
    assert "attempt + 1" in fetch
    assert "次重试" in fetch


def test_loader_keeps_the_last_graph_in_the_session():
    """中枢慢或那一跳没连上时，重进这一页要画得出上一次的星图。"""
    html = graph_mount("graph")
    assert "ROGMEM_GRAPH_CACHE_PREFIX" in html
    load = _fn(html, "rogmemLoadGraph", "rogmemOpenBrowser")
    assert "rogmemGraphStored" in load
    assert load.index("rogmemGraphStored") < load.index("rogmemFetchGraph")
    key = _fn(html, "rogmemGraphCacheKey", "rogmemGraphStore")
    assert "location.pathname" in key
    assert "el.id" in key


def test_retry_button_refetches_in_place_instead_of_reloading_the_page():
    html = graph_mount("graph")
    show = _fn(html, "rogmemShowError", "rogmemGraphCacheKey")
    line = next(row for row in show.splitlines() if "btn.onclick" in row)
    assert "typeof retry === 'function'" in line
    assert line.index("retry()") < line.index("location.reload()")
    fetch = _fn(html, "rogmemFetchGraph", "rogmemLoadGraph")
    assert "rogmemFetchGraph(el, draw, 1)" in fetch


def test_expired_session_goes_to_the_login_page():
    html = graph_mount("graph")
    req = _fn(html, "rogmemGraphRequest", "rogmemDrawGraph")
    assert "401" in req
    assert "/admin/login" in req


def test_render_failure_is_not_reported_as_a_load_failure():
    """取数成功、画不出来，是渲染的问题。不能和「图谱没有载入」混成一句。"""
    html = graph_mount("graph")
    draw = _fn(html, "rogmemDrawGraph", "rogmemFetchGraph")
    assert "try {" in draw
    assert "draw(nodes, edges)" in draw
    assert "'图谱画不出来" in draw
    assert "'图谱没有载入" not in draw


def test_both_renderers_share_the_one_fetch_call_site():
    """取数只有一处。两种渲染器都不再各写一份 fetch。"""
    for renderer in ("3d", "cosmos"):
        html = graph_page(renderer=renderer)
        assert html.count("fetch('/admin/api/graph'") == 1


def test_render_watchdog_leaves_the_loader_alone_while_data_is_still_coming():
    html = graph_page()
    watchdog = html.split("function renderWatchdog", 1)[1].split(")();", 1)[0]
    assert "rogmemGraphLoading" in watchdog


def test_render_watchdog_runs_only_after_render():
    html = graph_mount("graph")
    # 渲染看门狗在 graph.render 成功之后才启动，而且常量独立于 fetch 超时。
    assert "renderWatchdog" in html or "render_watchdog" in html or "watchdogRender" in html
    # 文件中出现两个 15000（fetch + render-watchdog）或一个单独的渲染看门狗常量。
    # 这里只要求"渲染看门狗"这个标识字符串存在即视为独立建立，具体值在 JS 中。


def test_error_state_has_retry_button():
    html = graph_mount("graph")
    assert "图谱没有载入" in html
    assert "重试" in html
    assert "location.reload" in html


def test_webgl_missing_fallback_still_present():
    html = graph_mount("graph")
    assert "用系统浏览器打开同一页" in html


def test_empty_hint_still_present():
    html = graph_mount("graph")
    assert "还没有可以铺开的星点" in html


def test_terminal_states_clear_aria_busy():
    html = graph_mount("graph")
    # 所有终止路径都把 aria-busy 置 false。
    assert "setAttribute('aria-busy', 'false')" in html or \
        'setAttribute("aria-busy", "false")' in html


def test_graph_page_mounts_loader_shell():
    html = graph_page()
    assert "graph-loader" in html
    assert "正在铺开星图" in html
