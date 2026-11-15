"""Device display names: hostname default, ban relative 本机 labels."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rogmem.device_identity import (
    UNNAMED_LABEL,
    default_device_name,
    display_device_name,
    migrate_local_device_labels,
    normalize_device_name,
)


def test_default_strips_domain(monkeypatch):
    monkeypatch.setattr("platform.node", lambda: "studio-mac.local")
    assert default_device_name() == "studio-mac"


def test_default_empty_is_device(monkeypatch):
    monkeypatch.setattr("platform.node", lambda: "")
    monkeypatch.setattr("platform.node", lambda: "   ")
    assert default_device_name() == "device"
    monkeypatch.setattr("platform.node", lambda: "")
    assert default_device_name() == "device"


def test_normalize_bans_relative_labels(monkeypatch):
    monkeypatch.setattr("platform.node", lambda: "office-pc.lan")
    for banned in ("本机", "localhost", "local", "LOCALHOST", "  本机  ", "", None):
        assert normalize_device_name(banned) == "office-pc"
    assert normalize_device_name("工作室电脑") == "工作室电脑"
    assert normalize_device_name("  qa-laptop  ") == "qa-laptop"


def test_normalize_never_returns_banned_when_hostname_is_localhost(monkeypatch):
    monkeypatch.setattr("platform.node", lambda: "localhost")
    assert default_device_name() == "localhost"
    assert normalize_device_name("本机") == "device"
    assert normalize_device_name("") == "device"


def test_display_maps_historical_benji_to_unnamed():
    assert display_device_name("本机") == UNNAMED_LABEL
    assert display_device_name("  本机  ") == UNNAMED_LABEL
    assert display_device_name("") == UNNAMED_LABEL
    assert display_device_name(None) == UNNAMED_LABEL
    assert display_device_name("office-pc") == "office-pc"
    assert display_device_name("本机 · Cursor") == f"{UNNAMED_LABEL} · Cursor"
    assert display_device_name("本机电脑") == "本机电脑"


def test_migrate_local_rewrites_benji_only(monkeypatch):
    monkeypatch.setattr("platform.node", lambda: "devbox")
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE sources (device_name TEXT)")
    con.execute("CREATE TABLE agents (device_name TEXT, display_name TEXT)")
    con.execute("INSERT INTO sources VALUES ('本机')")
    con.execute("INSERT INTO sources VALUES ('studio')")
    con.execute("INSERT INTO agents VALUES ('本机', '本机 · Cursor')")
    changed = migrate_local_device_labels(con, "办公电脑")
    assert changed >= 2
    names = [row[0] for row in con.execute("SELECT device_name FROM sources ORDER BY device_name")]
    assert names == ["studio", "办公电脑"]
    agent = con.execute("SELECT device_name, display_name FROM agents").fetchone()
    assert agent[0] == "办公电脑"
    assert agent[1] == "办公电脑 · Cursor"
    assert "本机" not in agent[1]


def test_installer_authorize_sends_hostname_not_benji(tmp_path, monkeypatch):
    monkeypatch.setattr("platform.node", lambda: "studio-mac.local")
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    from installer.rogmem_installer import authorize_host, write_configs

    write_configs(home, "http://127.0.0.1:9", programs=["Hermes"])
    captured: dict = {}

    def post(body):
        captured.update(body)
        return {"ok": True, "code": "ABCD", "status": "待放行", "id": "a1"}

    result = authorize_host(home, "http://127.0.0.1:9", "Hermes", post)
    assert result["ok"] is True
    assert captured["device_name"] == "studio-mac"
    assert captured["device_name"] != "本机"
    assert captured["device_id"]
    assert captured["device_id"] != captured["device_name"]
    saved = (home / ".rogmem" / "device_name").read_text(encoding="utf-8").strip()
    assert saved == "studio-mac"


def test_installer_device_name_override_and_ban(tmp_path, monkeypatch):
    monkeypatch.setattr("platform.node", lambda: "studio-mac.local")
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    from installer.rogmem_installer import authorize_host, write_configs

    write_configs(home, "http://127.0.0.1:9", programs=["Hermes"])
    captured: dict = {}

    def post(body):
        captured.update(body)
        return {"ok": True, "code": "ABCD", "status": "待放行", "id": "a1"}

    authorize_host(home, "http://127.0.0.1:9", "Hermes", post, device_name="工作室电脑")
    assert captured["device_name"] == "工作室电脑"
    captured.clear()
    authorize_host(home, "http://127.0.0.1:9", "Hermes", post, device_name="本机")
    assert captured["device_name"] == "studio-mac"


def test_engine_ingest_and_enroll_stop_hardcoding_benji(tmp_path, monkeypatch):
    monkeypatch.setattr("platform.node", lambda: "office-pc.lan")
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    from rogmem import crypto, engine

    engine.connect(tmp_path, tmp_path / "scan")
    dest = tmp_path / "scan" / "note.txt"
    dest.write_text("一段原文", encoding="utf-8")
    host = "office-pc"
    filed = engine.ingest_file(dest)
    row = engine.db().execute("SELECT device_name FROM sources WHERE id=?", (filed["id"],)).fetchone()
    assert row["device_name"] == host

    named = engine.ingest_file(dest, device_name="工作室电脑")
    assert named.get("duplicate") is True

    other = tmp_path / "scan" / "other.txt"
    other.write_text("另一段原文", encoding="utf-8")
    custom = engine.ingest_file(other, device_name="本机")
    custom_row = engine.db().execute("SELECT device_name FROM sources WHERE id=?", (custom["id"],)).fetchone()
    assert custom_row["device_name"] == host

    hist = engine.ingest_history(
        [{"role": "user", "text": "历史一句", "event_at": "2026-10-01T00:00:00+00:00", "session_id": "s", "seq": 1}],
        "Cursor",
    )
    assert hist["ok"] is True
    hist_row = engine.db().execute(
        "SELECT device_name FROM sources WHERE body=?", ("历史一句",)
    ).fetchone()
    assert hist_row["device_name"] == host

    hist2 = engine.ingest_history(
        [{"role": "user", "text": "带名历史", "event_at": "2026-10-01T00:00:01+00:00", "session_id": "s2", "seq": 1}],
        "Cursor",
        device_name="客厅电脑",
    )
    assert hist2["ok"] is True
    hist2_row = engine.db().execute(
        "SELECT device_name FROM sources WHERE body=?", ("带名历史",)
    ).fetchone()
    assert hist2_row["device_name"] == "客厅电脑"

    public = crypto.b64encode(crypto.new_keypair()[1])
    enrolled = engine.enroll_apply("dev-stable", "本机", "Cursor", public)
    agent = engine.db().execute("SELECT device_id, device_name, display_name FROM agents WHERE id=?", (enrolled["id"],)).fetchone()
    assert agent["device_id"] == "dev-stable"
    assert agent["device_name"] == host
    assert agent["display_name"] == f"{host} · Cursor"

    saved = engine.save_collect(
        {
            "scan_path": str(tmp_path / "scan"),
            "scan_interval": "60",
            "file_types": "txt,md",
            "turn_wait": "2",
            "sweep_at": "03:00",
            "device_name": "客厅电脑",
        }
    )
    assert saved["ok"] is True
    assert "device_name" not in saved["config"]
    assert engine.get_setting("device_name") == ""
    via_setting = tmp_path / "scan" / "setting.txt"
    via_setting.write_text("设置来的原文", encoding="utf-8")
    setting_row = engine.ingest_file(via_setting)
    stored = engine.db().execute("SELECT device_name FROM sources WHERE id=?", (setting_row["id"],)).fetchone()
    assert stored["device_name"] == host


def test_a_device_name_survives_an_http_header():
    from rogmem.device_identity import header_device_name, read_device_name_header

    assert header_device_name("AAAA") == "AAAA"
    assert read_device_name_header("AAAA") == "AAAA"
    encoded = header_device_name("书房电脑")
    encoded.encode("latin-1")
    assert "书房" not in encoded
    assert read_device_name_header(encoded) == "书房电脑"


def test_new_enroll_import_paths_have_no_benji_literal():
    engine_text = Path("rogmem/engine.py").read_text(encoding="utf-8")
    installer_text = Path("installer/rogmem_installer.py").read_text(encoding="utf-8")
    app_text = Path("rogmem/app.py").read_text(encoding="utf-8")
    ui_text = Path("rogmem/ui.py").read_text(encoding="utf-8")
    assert "'本机'" not in engine_text
    assert '"本机"' not in engine_text
    assert '"device_name": "本机"' not in installer_text
    assert "x-rogmem-device-name" in app_text
    assert "resolve_device_name" in app_text
    assert "未命名设备" in ui_text
    assert "设备显示名" not in ui_text
    assert "display_device_name" in ui_text


def test_installer_local_relabel_skips_real_home(tmp_path, monkeypatch):
    from installer.rogmem_installer import relabel_local_device

    assert relabel_local_device(Path.home()) == 0
    db_path = tmp_path / "rogmem.sqlite"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE sources (device_name TEXT)")
    con.execute("INSERT INTO sources VALUES ('本机')")
    con.commit()
    con.close()
    monkeypatch.setattr("platform.node", lambda: "devbox")
    assert relabel_local_device(tmp_path) == 1
    names = [row[0] for row in sqlite3.connect(db_path).execute("SELECT device_name FROM sources")]
    assert names == ["devbox"]
