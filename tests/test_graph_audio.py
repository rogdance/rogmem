"""全站背景音乐：只播随包自带的一首 BGM。

顶栏一枚旋转的音符控制播放；控制台每一页都在，跨页从断点续播。
曲目按内容指纹做本地缓存。不放交互音效、不加载第三方音频、不留下许可或署名文字。
"""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from rogmem import ui

ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "rogmem" / "vendor" / "audio"


def _mp3(data: bytes) -> bool:
    return data[:3] == b"ID3" or (
        len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0
    )


def test_only_one_bgm_is_bundled():
    assert ui.GRAPH_AUDIO_BGM == "bgm.mp3"
    assert ui.GRAPH_AUDIO_FILES == frozenset({"bgm.mp3"})
    files = {path.name for path in AUDIO.iterdir() if path.is_file()}
    assert files == {"bgm.mp3"}
    data = (AUDIO / ui.GRAPH_AUDIO_BGM).read_bytes()
    assert _mp3(data)
    assert len(data) > 100_000


def test_bgm_url_is_versioned_by_content():
    etag, last_modified, size = ui.graph_audio_fingerprint()
    assert etag.startswith('"') and etag.endswith('"') and len(etag) > 40
    assert last_modified.endswith("GMT")
    assert size == (AUDIO / ui.GRAPH_AUDIO_BGM).stat().st_size
    src = ui.graph_audio_src()
    assert src.startswith("/admin/static/audio/bgm.mp3?v=")
    assert src.endswith(etag.strip('"')[:12])


def test_every_page_has_the_music_toggle_default_on():
    html = ui.graph_page()
    plain = ui.page("结晶记忆", "crystals", "<p>hello</p>")
    for page in (html, plain):
        assert ui.graph_audio_src() in page
        assert 'id="rogmem-graph-bgm" loop' in page
        assert 'class="rga-note"' in page
        assert "@keyframes rga-spin" in page
        assert "animation-play-state:paused" in page
        assert 'id="rga-toggle" aria-pressed="true"' in page
        assert 'aria-label="暂停背景音乐"' in page
        assert page.count('id="rga-toggle"') == 1
        assert page.count('id="rogmem-graph-bgm"') == 1
        assert "rga-disc" not in page
        assert "rga-badge" not in page
        assert "pointerdown" in page
        assert "localStorage" in page
    # 旧音景、第三方素材和授权文字，一个字节都不留在页面上。
    for gone in (
        "Mixkit", "license", "Vastness", "Andrew Ev", "Crystal chime",
        "vastness", "crystal-chime", "fairy-", "sparkle-touch", "bell-chime",
        "rga-sfx", "rga-volume", "rga-bgm", "rga-panel", "rga-credit",
        "rogmemGraphAudio", "chime(", "AudioContext", "createStereoPanner",
        "decodeAudioData", "结晶轻响", "星空音效", "音效", "致谢",
    ):
        assert gone not in html, gone


def test_music_toggle_sits_before_the_hamburger_not_on_top_of_it():
    """音乐按钮排在折叠导航按钮之前，窄屏下不压住汉堡 icon。"""
    plain = ui.page("结晶记忆", "crystals", "<p>hello</p>")
    assert plain.index('class="ver"') < plain.index('id="model-health"') < plain.index('id="rga-toggle"')
    header = plain.split("<header>", 1)[1].split("</header>", 1)[0]
    assert header.index('id="rga-toggle"') < header.index('id="nav-toggle"')
    assert 'id="nav-toggle"' in header.rsplit("<button", 1)[1]


def test_bgm_resumes_from_the_saved_position_on_every_page():
    plain = ui.page("结晶记忆", "crystals", "<p>hello</p>")
    for needle in (
        "rogmem-bgm-position-v1", "sessionStorage", "timeupdate",
        "loadedmetadata", "pagehide", "beforeunload",
        "function resume()", "function savePos()", "bgm.currentTime",
    ):
        assert needle in plain, needle


def test_audio_layer_is_global_not_tied_to_the_graph_page():
    plain = ui.page("结晶记忆", "crystals", "<p>hello</p>")
    assert 'id="rga-toggle"' in plain
    assert ui.graph_audio_src() in plain
    graph = ui.graph_page()
    assert graph.count('id="rga-toggle"') == 1
    assert graph.count('id="rogmem-graph-bgm"') == 1
    # 未登录的口令页不算"系统内"，不发音频。
    assert "rogmem-graph-bgm" not in ui.login_page()


def test_no_audio_credit_anywhere_in_the_about_page():
    from rogmem.ui import about_page

    about = about_page("V1-test")
    assert "致谢" in about  # 项目原有的致谢区块仍在，只是不含音频条目
    # 全站 BGM 会出现在每一页，但不得留下任何署名或许可文字。
    for gone in (
        "音效", "Mixkit", "license", "Vastness", "Andrew Ev",
        "Crystal chime", "Floating Pulse",
    ):
        assert gone not in about, gone


def test_a_crystal_click_no_longer_plays_sound():
    script = ui._GRAPH_3D_JS_TEMPLATE
    assert "pulseAt(hits[0].point)" in script
    for gone in ("rogmemGraphAudio", "chime", "GraphAudio"):
        assert gone not in script, gone


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine
    from rogmem.app import build_app

    engine.connect(tmp_path, tmp_path / "scan")
    return TestClient(build_app())


def test_audio_route_serves_only_the_bgm(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.get("/admin/static/audio/" + ui.GRAPH_AUDIO_BGM)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert _mp3(response.content)
    for name in ("vastness.mp3", "crystal-chime.mp3", "not-a-track.mp3", "app.py"):
        assert client.get("/admin/static/audio/" + name).status_code == 404, name


def test_audio_route_caches_locally_and_revalidates(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    src = ui.graph_audio_src()
    response = client.get(src)
    assert response.status_code == 200
    assert len(response.content) == (AUDIO / ui.GRAPH_AUDIO_BGM).stat().st_size
    cache = response.headers["cache-control"]
    assert "private" in cache and "max-age=31536000" in cache and "immutable" in cache
    assert response.headers["etag"] == ui.graph_audio_fingerprint()[0]
    assert response.headers["last-modified"]

    # 内容没变：只回 304，不重传整首曲子。
    etag = response.headers["etag"]
    conditional = client.get(src, headers={"If-None-Match": etag})
    assert conditional.status_code == 304
    assert conditional.content == b""
    assert conditional.headers["etag"] == etag

    dated = client.get(src, headers={"If-Modified-Since": response.headers["last-modified"]})
    assert dated.status_code == 304
    assert dated.content == b""

    # 指纹不匹配时仍返回完整文件。
    stale = client.get(src, headers={"If-None-Match": '"some-old-version"'})
    assert stale.status_code == 200
    assert _mp3(stale.content)
