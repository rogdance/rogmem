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


def test_note_button_sits_before_the_hamburger_not_on_top_of_it():
    """音乐按钮在汉堡之前，折叠导航按钮才是 header 最后一个 button。"""
    page = ui.graph_page()
    assert page.index('class="ver"') < page.index('id="model-health"') < page.index('id="rga-toggle"') < page.index('id="nav-toggle"')
    assert page.count('id="rga-toggle"') == 1
    header = page.split("<header>", 1)[1].split("</header>", 1)[0]
    assert header.index('id="rga-toggle"') < header.index('id="nav-toggle"')
    assert 'id="nav-toggle"' in header.rsplit("<button", 1)[1]
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
    assert 'id="rogmem-graph-bgm" loop preload="none"' in page
    assert 'data-bgm-src="' + ui.graph_audio_src() + '"' in page
    script = page.split('var STORE = "rogmem-graph-bgm-v1"', 1)[1]
    # 资产就位后自动尝试出声，不再有"首次交互才 unlock"的硬门槛。
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


def test_note_icon_is_small_but_never_squashed():
    """20px 按钮 + 12px 等宽高等比音符：缩小后不变形，几何居中，旋转不跑偏。"""
    page = ui.graph_page()
    btn = re.search(r"header \.rga-toggle\{([^}]*)\}", page)
    assert btn, "顶栏音乐按钮样式没找到"
    css = btn.group(1)
    assert "width:20px" in css and "height:20px" in css
    assert "aspect-ratio:1" in css and "flex:none" in css
    assert "box-sizing:border-box" in css and "margin-right:4px" in css
    note = re.search(r"\.rga-note\{([^}]*)\}", page)
    assert note, "音符样式没找到"
    assert "width:12px" in note.group(1) and "height:12px" in note.group(1)
    assert "aspect-ratio:1" in note.group(1) and "flex:none" in note.group(1)
    # 图形重心对齐 viewBox 中心，旋转时不会偏出小圆圈。
    assert 'preserveAspectRatio="xMidYMid meet"' in page
    assert 'transform="translate(1.6 0)"' in page
    # 缩小后仍保留透明放大热区，且窄屏与汉堡按钮拉开间距。
    assert "header .rga-toggle::before" in page
    assert "@media (max-width: 860px){header .rga-toggle{margin-right:8px}}" in page


def test_bgm_download_waits_until_other_page_assets_are_loaded():
    """初始 HTML 不带音频 src；window.load 之后才启动下载，不与星图资源抢带宽。"""
    for page in (ui.page("结晶记忆", "crystals", "<p>hello</p>"), ui.graph_page()):
        tag = re.search(r'<audio id="rogmem-graph-bgm"[^>]*>', page).group(0)
        assert 'preload="none"' in tag
        assert re.search(r'\ssrc="', tag) is None, tag
        assert 'data-bgm-src="' + ui.graph_audio_src() + '"' in tag
        assert 'bgm.getAttribute("data-bgm-src")' in page
        assert "function startBgm()" in page
        assert "function afterPageAssets()" in page
        assert "bgm.src = bgmSrc" in page
        assert "bgm.load()" in page
        assert 'document.readyState === "complete"' in page
        assert 'window.addEventListener("load", afterPageAssets' in page
        assert "requestIdleCallback" in page
        # 用户主动开音乐时不等资产，立刻下载。
        assert "if (bgmLoaded) attempt(); else startBgm();" in page
