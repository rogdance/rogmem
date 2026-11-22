"""记忆星图视觉打磨：背景星圆点、全站音乐按钮与跨页续播、音符加载态动效。"""

from __future__ import annotations

import re

from rogmem import ui


def test_background_stars_are_round_not_square():
    """Three.js 的 PointsMaterial 不给 map 会把点画成实心方块；必须补柔边圆点贴图。"""
    page = ui.graph_page()
    assert "function rogmem3dStarSprite(THREE)" in page
    assert "createRadialGradient" in page
    assert "new THREE.CanvasTexture(canvas)" in page
    stars = re.search(
        r"new THREE\.Points\(starGeo, new THREE\.PointsMaterial\(\{.*?\}\)\)", page, re.S
    )
    assert stars, "背景星材质没找到"
    assert "map: rogmem3dStarSprite(THREE)" in stars.group(0)
    # 结晶节点本来就靠 discard 裁圆，别把两者改混。
    assert "if (d > 1.0) discard;" in page


def test_note_button_sits_right_of_model_health_as_the_last_header_control():
    page = ui.graph_page()
    assert page.index('class="ver"') < page.index('id="model-health"') < page.index('id="rga-toggle"')
    assert page.count('id="rga-toggle"') == 1
    header = page.split("<header>", 1)[1].split("</header>", 1)[0]
    assert header.index('id="nav-toggle"') < header.index('id="rga-toggle"')
    assert 'id="rga-toggle"' in header.rsplit("<button", 1)[1]
    # 星图右上角只剩活跃结晶计数；按钮不再叠在星图里。
    assert "<div id=\"graph\" aria-busy=\"true\"><p class='graph-count' id='graph-count'></p>" in page
    assert "graph-chrome" not in page
    assert "#graph > .graph-count" in page


def test_model_health_is_a_round_dot_not_a_square():
    """模型状态用一枚 10px 小圆点当状态灯，不再是 16px 方块。"""
    page = ui.page("结晶记忆", "crystals", "<p>hello</p>")
    dot = re.search(r"header \.model-health \{([^}]*)\}", page)
    assert dot, "顶栏模型状态样式没找到"
    css = dot.group(1)
    assert "border-radius: 50%" in css
    assert "width: 10px" in css and "height: 10px" in css
    assert "border-radius: 3px" not in page
    assert 'id="model-health"' in page
    assert "header .model-health::after" in page


def test_bgm_starts_on_entry_without_waiting_for_a_gesture():
    page = ui.graph_page()
    assert 'id="rogmem-graph-bgm" loop preload="auto"' in page
    script = page.split('var STORE = "rogmem-graph-bgm-v1"', 1)[1]
    # 进页面立即尝试出声，不再有"首次交互才 unlock"的硬门槛。
    assert "attempt();" in script
    assert "var unlocked = false" not in page
    assert "!unlocked" not in page
    # 被浏览器策略挡住时静音打底，任一次交互淡入，不必专点按钮。
    assert "mutedForAutoplay" in page
    assert "bgm.muted = true" in page
    assert "fadeIn()" in page
    # 被策略挡住时语义要诚实：不是"载入中"，而是等一次交互开声。
    assert "点一下开启背景音乐" in page
    # 上次关过音乐：初始直接落暂停态，不能一直转加载圈。
    assert 'if (!playing) { setState("paused"); }' in page
    # 跨页续播：位置存会话，新页面从断点接上。
    for needle in ("rogmem-bgm-position-v1", "sessionStorage", "loadedmetadata",
                   "timeupdate", "pagehide", "function resume()"):
        assert needle in page, needle
    for event in ("pointerdown", "keydown", "touchstart"):
        assert event in page


def test_note_loading_animation_differs_from_playing():
    page = ui.graph_page()
    assert 'data-state="loading"' in page
    for state in ("loading", "playing", "paused", "error"):
        assert 'data-state="' + state + '"' in page
    # 加载中：外圈快速扫掠弧 + 音符明暗呼吸，音符不转。
    assert "@keyframes rga-scan" in page
    assert "@keyframes rga-breathe" in page
    loading = re.search(
        r'\.rga-toggle\[data-state="loading"\] \.rga-note\{[^}]+\}', page
    ).group(0)
    assert "rga-spin" not in loading
    assert "rga-breathe" in loading
    # 播放中：音符匀速自转 + 品牌粉符头。
    playing = re.search(
        r'\.rga-toggle\[data-state="playing"\] \.rga-note\{[^}]+\}', page
    ).group(0)
    assert "rga-spin" in playing
    assert "fill:var(--brand)" in page
    assert "prefers-reduced-motion" in page
    # 状态由音频真实事件驱动，不靠定时器猜。
    for event in ("loadstart", "waiting", "playing", "pause", "error"):
        assert 'addEventListener("' + event + '"' in page
