"""First-install script stops for a person and never calls the private smoke path."""

from __future__ import annotations

import json
from pathlib import Path

from scripts import install_zspace as inst
from scripts.public_tree import cookie_needles, forbidden_addresses

ROOT = Path(__file__).resolve().parents[1]


class Completed:
    def __init__(self, code: int, stdout: str = "", stderr: str = ""):
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


def test_repo_check_stops_when_an_install_file_has_a_fixed_address(tmp_path):
    for name in ("docs/install.md", "Dockerfile", "deploy/build_compose.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n", encoding="utf-8")
    (tmp_path / "docs" / "install.md").write_text(forbidden_addresses()[0] + "\n", encoding="utf-8")
    try:
        inst.repo_ready(tmp_path)
    except inst.Stop as exc:
        assert exc.step == "仓库"
    else:
        raise AssertionError("expected stop")


def test_one_personal_space_becomes_the_project_directory():
    payload = {"ok": True, "data": {"personal_spaces": ["/sata1/my/data"]}}
    assert inst.pick_destination(payload) == "/sata1/my/data/Dockers/rogmem"
    try:
        inst.pick_destination({"ok": True, "data": {"personal_spaces": ["/sata1/my/data", "/nvme1/my/data"]}})
    except inst.Stop as exc:
        assert exc.step == "放置目录"
        assert "/sata1/my/data/Dockers/rogmem" in str(exc)
        assert "/nvme1/my/data/Dockers/rogmem" in str(exc)
    else:
        raise AssertionError("expected stop")


def test_compose_rejects_any_port_other_than_18080():
    good = inst.compose_text(cpus="2.0", mem_limit="4g", budget="0.5", hf_endpoint="https://hf-mirror.com")
    inst.compose_ok(good)
    assert "ROGMEM_NO_PASSWORD:" not in good
    assert "ROGMEM_NO_PASSWORD=" not in good
    bad = good.replace('"18080:18080"', '"18081:18080"', 1)
    try:
        inst.compose_ok(bad)
    except inst.Stop as exc:
        assert exc.step == "端口"
    else:
        raise AssertionError("expected stop")


def test_preflight_requires_stamp_and_weight():
    stamp = inst.preflight_ok("STAMP 2611140907\n80000001 /opt/fastembed/model.onnx\n")
    assert stamp == "2611140907"
    try:
        inst.preflight_ok("STAMP 2611140907\n10 /opt/fastembed/tiny\n")
    except inst.Stop as exc:
        assert exc.step == "镜像预检"
    else:
        raise AssertionError("expected stop")


def _runner(tar_size: int):
    calls = []

    def run(command):
        calls.append(command)
        if command[:3] == ["docker", "buildx", "version"]:
            return Completed(0, "buildx\n")
        if "buildx" in command and "build" in command:
            return Completed(0)
        if command[:3] == ["docker", "image", "inspect"]:
            return Completed(0, "amd64\n")
        if command[:2] == ["docker", "run"]:
            return Completed(0, "STAMP 2611140907\n80000001 /opt/fastembed/model.onnx\n")
        if command[:2] == ["docker", "save"]:
            Path(command[command.index("-o") + 1]).write_bytes(b"x" * tar_size)
            return Completed(0)
        if command[1:3] == ["--profile", "home"] and "status" in command:
            return Completed(0, json.dumps({"ok": True, "data": {"logged_in": True}}))
        if "file" in command and "space" in command:
            return Completed(0, json.dumps({"ok": True, "data": {"personal_spaces": ["/sata1/my/data"]}}))
        if "file" in command and "info" in command:
            params = json.loads(command[command.index("--params") + 1])
            if params["path"].endswith(".tar"):
                return Completed(0, json.dumps({"ok": True, "data": {"size": tar_size}}))
            return Completed(1, json.dumps({"ok": False, "error": {"code": "missing", "message": "no"}}))
        if "mkdir" in command or "upload" in command:
            return Completed(0, json.dumps({"ok": True, "data": {}}))
        return Completed(1, json.dumps({"ok": False, "error": {"code": "unexpected", "message": " ".join(command)}}))

    return calls, run


def test_without_confirm_absent_it_does_not_upload(tmp_path, capsys):
    calls, run = _runner(4)
    cli = tmp_path / "zspace-cli"
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    cli.chmod(0o755)
    code = inst.main(["--root", str(ROOT), "--profile", "home"], runner=run, cli_path=cli)
    assert code == 2
    assert "停止于项目" in capsys.readouterr().err
    assert not any("upload" in part for command in calls for part in command)
    assert not any(part == "build" for command in calls for part in command)


def test_confirm_absent_uploads_and_stops_for_a_person(tmp_path, capsys):
    calls, run = _runner(4)
    cli = tmp_path / "zspace-cli"
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    cli.chmod(0o755)
    code = inst.main(
        ["--root", str(ROOT), "--profile", "home", "--confirm-absent"],
        runner=run,
        cli_path=cli,
    )
    assert code == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert out.startswith("请到极空间确认\n")
    assert "/sata1/my/data/Dockers/rogmem/rogmem-local-2611140907.tar" in out
    assert not any("delete" in part for command in calls for part in command)
    assert any("upload" in command for command in calls)


def test_script_does_not_point_at_browser_sessions_or_private_smoke():
    text = (ROOT / "scripts" / "install_zspace.py").read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")
    for needle in forbidden_addresses() + cookie_needles():
        assert needle not in text
        assert needle not in docs
    assert "zspace-release" not in text
    assert "zspace-release" not in docs
    assert "sqlite3" not in text
    assert "rogmem-data" in docs
    assert "不删除" in docs
