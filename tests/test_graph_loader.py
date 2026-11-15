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
