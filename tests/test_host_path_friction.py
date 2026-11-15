"""Official host path is installer script + Web admin. No packaged client."""

from __future__ import annotations

import ast
from pathlib import Path

from installer import rogmem_installer as inst

ROOT = Path(__file__).resolve().parents[1]
ADR = ROOT / "docs" / "superpowers" / "specs" / "2026-10-25-rogmem-host-client-adr.md"
DESIGN = ROOT / "docs" / "superpowers" / "specs" / "2026-10-23-rogmem-design.md"
FEATURES = ROOT / "docs" / "superpowers" / "specs" / "2026-10-24-rogmem-features.md"
WORKLIST = ROOT / "docs" / "superpowers" / "specs" / "2026-10-25-rogmem-worklist.md"
INSTALLER = ROOT / "installer" / "rogmem_installer.py"
COLD_START = ROOT / "installer" / "COLD_START.md"

PACKAGE_MARKERS = (
    "tauri",
    "electron",
    ".dmg",
    ".msi",
    "Win/Mac 安装包通过",
    "Windows 安装包通过",
    "macOS 安装包通过",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_installer_module_exposes_find_and_forward():
    assert hasattr(inst, "find_rogmem")
    assert hasattr(inst, "forward")
    assert callable(inst.find_rogmem)
    assert callable(inst.forward)
    source = ast.parse(INSTALLER.read_text(encoding="utf-8"))
    names = {node.name for node in ast.walk(source) if isinstance(node, ast.FunctionDef)}
    assert "find_rogmem" in names
    assert "forward" in names


def test_adr_states_no_packaging_and_script_web_is_official_path():
    adr = _text(ADR)
    assert "不做" in adr and ("打包" in adr or "安装包" in adr)
    assert "脚本" in adr and ("Web" in adr or "管理台" in adr)
    assert "installer/rogmem_installer.py" in adr
    assert "冷启动" in adr
    assert "QA-019" in adr
    assert "127.0.0.1:18780" in adr or "18780" in adr
    for phrase in ("Python", "Windows", "通道", "口令", "prompt"):
        assert phrase in adr


def test_design_section_8_points_at_adr_delivery_shape():
    design = _text(DESIGN)
    assert "2026-10-25-rogmem-host-client-adr.md" in design
    section = design.split("## 8.")[1].split("\n## ")[0]
    assert "交付形态" in section
    assert "脚本" in section
    assert "Web" in section or "管理台" in section
    assert "打包" in section or "安装包" in section


def test_features_and_worklist_do_not_claim_packaged_client_passed():
    features = _text(FEATURES)
    worklist = _text(WORKLIST)
    assert "2026-10-25-rogmem-host-client-adr.md" in features
    assert "2026-10-25-rogmem-host-client-adr.md" in worklist
    assert "脚本" in features and ("Web" in features or "管理台" in features)
    assert "脚本" in worklist and ("Web" in worklist or "管理台" in worklist)
    for text in (features, worklist):
        for marker in PACKAGE_MARKERS:
            assert marker not in text
        assert "安装包通过" not in text
        assert "打包客户端通过" not in text


def test_cold_start_prompt_lands_next_to_installer():
    prompt = _text(COLD_START)
    assert "不做" in prompt and ("打包" in prompt or "安装包" in prompt)
    assert "rogmem_installer.py" in prompt
    assert "find_rogmem" in prompt
    assert "forward" in prompt
    assert "Python" in prompt


def test_installer_names_fixed_forward_port_and_windows_cursor_path(tmp_path, monkeypatch):
    assert inst.FIXED_FORWARD_PORT == 18780
    mac = tmp_path / "mac"
    (mac / "Library" / "Application Support" / "Cursor").mkdir(parents=True)
    win = tmp_path / "win"
    (win / "AppData" / "Roaming" / "Cursor").mkdir(parents=True)
    linux = tmp_path / "linux"
    (linux / ".config" / "Cursor").mkdir(parents=True)
    assert inst.plan(mac) == ["Cursor"]
    assert inst.plan(win) == ["Cursor"]
    assert inst.plan(linux) == ["Cursor"]
    entry = "http://127.0.0.1:18780/admin/crystals"
    assert "--app=" in " ".join(inst.browser_command(entry))
    assert "请记住" not in " ".join(inst.browser_command(entry))
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(inst.platform, "system", lambda: "Windows")
    win_row = next(item for item in inst.detect_programs(empty) if item["program"] == "Cursor")
    assert win_row["found"] is False
    assert "AppData" in win_row["path"]
    assert inst.browser_command(entry)[:3] == ["cmd", "/c", "start"]
    monkeypatch.setattr(inst.platform, "system", lambda: "Linux")
    linux_row = next(item for item in inst.detect_programs(empty) if item["program"] == "Cursor")
    assert ".config" in linux_row["path"].replace("\\", "/")


def test_no_packaging_project_in_tree():
    forbidden_names = {
        "tauri.conf.json",
        "electron-builder.yml",
        "electron-builder.yaml",
        "Cargo.toml",
    }
    forbidden_suffixes = (".dmg", ".msi")
    for path in ROOT.rglob("*"):
        if any(part in {".git", ".venv", "__pycache__", "node_modules"} for part in path.parts):
            continue
        assert path.name not in forbidden_names, path
        assert not str(path).endswith(forbidden_suffixes), path
    installer = INSTALLER.read_text(encoding="utf-8")
    assert "tauri" not in installer.lower()
    assert "electron" not in installer.lower()
