"""记忆图谱音景：随包的成品音频、白名单静态路由、结晶点击的发声交接点。

The audio files are downloaded, finished recordings (Mixkit Free License).
This test freezes that the page carries the player, that only whitelisted files
are served, and that a crystal click hands off to the audio layer at the same
moment the shader pulse starts.
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


def _channel_mode(data: bytes) -> int | None:
    """MP3 frame header: 00 stereo / 01 joint-stereo / 11 mono."""
    i = 0
    if data[:3] == b"ID3":
        size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        i = 10 + size
    while i + 4 < len(data) and i < 200000:
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            return (data[i + 3] >> 6) & 3
        i += 1
    return None


def test_bundled_audio_is_real_and_the_bgm_is_stereo():
    assert ui.GRAPH_AUDIO_BGM == "vastness.mp3"
    assert ui.GRAPH_AUDIO_CHIMES[0] == "crystal-chime.mp3"
    assert set(ui.GRAPH_AUDIO_CHIMES) <= set(ui.GRAPH_AUDIO_SOURCES)
    for name in ui.GRAPH_AUDIO_SOURCES:
        data = (AUDIO / name).read_bytes()
        assert _mp3(data), name
        assert len(data) > 1000, name
    bgm = (AUDIO / ui.GRAPH_AUDIO_BGM).read_bytes()
    assert _channel_mode(bgm) in (0, 1), "BGM 必须是立体声"


def test_graph_page_carries_the_audio_layer():
    html = ui.graph_page()
    assert "/admin/static/audio/vastness.mp3" in html
    assert "rogmemGraphAudio" in html
    assert "crystal-chime.mp3" in html
    for element in ("rga-toggle", "rga-bgm", "rga-sfx", "rga-volume"):
        assert element in html
    assert "Mixkit" in html and "license" in html  # 来源与许可
    assert "AudioContext" in html
    assert "createStereoPanner" in html  # 立体声空间感
    assert "pointerdown" in html  # 浏览器自动播放策略：首次交互解锁


def test_audio_layer_is_exclusive_to_the_graph_page():
    plain = ui.page("结晶记忆", "crystals", "<p>hello</p>")
    assert "rga-toggle" not in plain
    assert "/admin/static/audio/" not in plain


def test_a_crystal_click_hands_off_to_the_audio_layer():
    script = ui._GRAPH_3D_JS_TEMPLATE
    assert "pulseAt(hits[0].point)" in script
    assert "rogmemGraphAudio.chime" in script
    assert "window.innerWidth" in script  # 按点击横坐标做左右声像


def test_audio_route_serves_only_whitelisted_files(tmp_path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import engine
    from rogmem.app import build_app

    engine.connect(tmp_path, tmp_path / "scan")
    client = TestClient(build_app())
    for name in ui.GRAPH_AUDIO_SOURCES:
        response = client.get("/admin/static/audio/" + name)
        assert response.status_code == 200, name
        assert response.headers["content-type"].startswith("audio/mpeg"), name
        assert _mp3(response.content), name
    assert client.get("/admin/static/audio/not-a-track.mp3").status_code == 404
    assert client.get("/admin/static/audio/app.py").status_code == 404
