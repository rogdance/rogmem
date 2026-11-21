"""记忆图谱背景音乐：只播随包自带的一首 BGM。

不放交互音效、不加载第三方音频、不在任何页面留下许可或署名文字。
测试冻结了资源清单、单曲播放、白名单静态路由，以及结晶点击不再发声。
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


def test_graph_page_plays_only_the_bgm():
    html = ui.graph_page()
    assert "/admin/static/audio/bgm.mp3" in html
    assert 'id="rogmem-graph-bgm" loop' in html
    assert "rga-toggle" in html
    assert "pointerdown" in html  # 浏览器自动播放策略：首次交互解锁
    # 旧音景、第三方素材和授权文字，一个字节都不留在页面上。
    for gone in (
        "Mixkit", "license", "Vastness", "Andrew Ev", "Crystal chime",
        "vastness", "crystal-chime", "fairy-", "sparkle-touch", "bell-chime",
        "rga-sfx", "rga-volume", "rga-bgm", "rga-panel", "rga-credit",
        "rogmemGraphAudio", "chime(", "AudioContext", "createStereoPanner",
        "decodeAudioData", "结晶轻响", "星空音效", "音效", "致谢",
    ):
        assert gone not in html, gone


def test_audio_layer_is_exclusive_to_the_graph_page():
    plain = ui.page("结晶记忆", "crystals", "<p>hello</p>")
    assert "rga-toggle" not in plain
    assert "/admin/static/audio/" not in plain


def test_no_audio_credit_anywhere_in_the_about_page():
    from rogmem.ui import about_page

    about = about_page("V1-test")
    assert "致谢" in about  # 项目原有的致谢区块仍在，只是不含音频条目
    for gone in (
        "音效", "Mixkit", "license", "Vastness", "Andrew Ev",
        "Crystal chime", "Floating Pulse", "bgm.mp3", "背景音乐",
    ):
        assert gone not in about, gone


def test_a_crystal_click_no_longer_plays_sound():
    script = ui._GRAPH_3D_JS_TEMPLATE
    assert "pulseAt(hits[0].point)" in script
    for gone in ("rogmemGraphAudio", "chime", "GraphAudio"):
        assert gone not in script, gone


def test_audio_route_serves_only_the_bgm(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine
    from rogmem.app import build_app

    engine.connect(tmp_path, tmp_path / "scan")
    client = TestClient(build_app())
    response = client.get("/admin/static/audio/" + ui.GRAPH_AUDIO_BGM)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert _mp3(response.content)
    for name in ("vastness.mp3", "crystal-chime.mp3", "not-a-track.mp3", "app.py"):
        assert client.get("/admin/static/audio/" + name).status_code == 404, name
