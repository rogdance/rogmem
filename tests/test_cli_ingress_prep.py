"""QA parallel B: envelope → Ingress. Marker/name = cli_ingress_prep.

No distill. No crystal write. No long-term skip standing in for green.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rogmem import cli, ingress

pytestmark = pytest.mark.cli_ingress_prep

ROOT = Path(__file__).resolve().parents[1]
BANNED_MODULES = {
    "rogmem.crystal_store",
    "rogmem.distill_gate",
    "rogmem.distill_resim",
    "rogmem.prompts",
    "rogmem.publish_gate",
    "rogmem.engine",
}


def _run(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.run(
        [sys.executable, "-m", "rogmem", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        input=input_text,
        check=False,
    )


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] if False else alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_cli_ingress_prep_help_exits_zero():
    root = _run("--help")
    assert root.returncode == 0
    assert "hosts" in root.stdout
    assert "history" in root.stdout
    assert "ingress" in root.stdout
    assert "serve" in root.stdout
    for args in (
        ("hosts", "--help"),
        ("hosts", "scan", "--help"),
        ("hosts", "connect", "--help"),
        ("history", "--help"),
        ("history", "preview", "--help"),
        ("history", "upload", "--help"),
        ("ingress", "--help"),
        ("ingress", "accept", "--help"),
        ("serve", "--help"),
    ):
        result = _run(*args)
        assert result.returncode == 0, (args, result.stderr)
        assert "dry-run" in result.stdout or args[0] == "serve"


def test_cli_ingress_prep_no_args_dispatches_to_serve(monkeypatch):
    called = {}

    def fake_serve() -> int:
        called["serve"] = True
        return 0

    monkeypatch.setattr(cli, "serve", fake_serve)
    assert cli.main([]) == 0
    assert called["serve"] is True


def test_cli_ingress_prep_hosts_scan_dry_run_shape(tmp_path):
    (tmp_path / ".cursor").mkdir()
    (tmp_path / "Library" / "Application Support" / "Cursor").mkdir(parents=True)
    result = _run("hosts", "scan", "--dry-run", "--home", str(tmp_path))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["command"] == "hosts.scan"
    assert payload["dry_run"] is True
    assert payload["wrote"] is False
    assert payload["distilled"] is False
    assert payload["crystal_written"] is False
    programs = {row["program"] for row in payload["hosts"]}
    assert "Cursor" in programs
    assert payload["text"].startswith("确认之前不会写入")


def test_cli_ingress_prep_hosts_connect_dry_run_does_not_write(tmp_path):
    (tmp_path / ".cursor").mkdir()
    (tmp_path / "Library" / "Application Support" / "Cursor").mkdir(parents=True)
    result = _run(
        "hosts",
        "connect",
        "--dry-run",
        "--home",
        str(tmp_path),
        "--program",
        "Cursor",
        "--base-url",
        "http://127.0.0.1:18780",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["command"] == "hosts.connect"
    assert payload["dry_run"] is True
    assert payload["wrote"] is False
    assert payload["written"] == []
    assert payload["distilled"] is False
    assert payload["crystal_written"] is False
    assert not (tmp_path / ".cursor" / "mcp.json").exists()


def test_cli_ingress_prep_history_preview_envelope_shape(tmp_path):
    session = tmp_path / ".cursor" / "projects" / "demo"
    session.mkdir(parents=True)
    record = {
        "role": "user",
        "session_id": "sess-1",
        "timestamp": "2026-01-02T03:04:05+00:00",
        "content": "合成句：北坡灯塔还在闪。",
    }
    (session / "chat.jsonl").write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    result = _run("history", "preview", "--dry-run", "--envelope", "--home", str(tmp_path))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["command"] == "history.preview"
    assert payload["dry_run"] is True
    assert payload["distilled"] is False
    assert payload["crystal_written"] is False
    assert payload["importable"] >= 1
    assert payload["envelopes"]
    first = payload["envelopes"][0]
    assert first["schema"] == ingress.SCHEMA
    assert first["kind"] == "turn"
    assert first["body"] == "合成句：北坡灯塔还在闪。"


def test_cli_ingress_prep_ingress_accept_dry_run_from_file(tmp_path):
    env = ingress.envelope(
        kind="turn",
        body="合成句：南门钟楼敲过三下。",
        event_at="2026-03-04T05:06:07+00:00",
        role="user",
        session_id="sess-2",
        seq=1,
        program="Cursor",
    )
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    result = _run("ingress", "accept", "--dry-run", "--file", str(path))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["command"] == "ingress.accept"
    assert payload["dry_run"] is True
    assert payload["ok"] is True
    assert payload["posted"] == 0
    assert payload["distilled"] is False
    assert payload["crystal_written"] is False
    assert payload["accepted"][0]["idempotency_key"] == env["idempotency_key"]


def test_cli_ingress_prep_ingress_refuses_crystal_envelope():
    crystal = {
        "type": "人物",
        "name": "合成人",
        "body_md": "# 概述\n无",
        "facts": [],
        "aliases": [],
        "owner_relation": "暂无信息",
    }
    checked = ingress.validate(crystal)
    assert checked["ok"] is False
    assert "schema" in checked["errors"]
    assert "crystal-envelope-refused" in checked["errors"]
    accepted = ingress.accept(crystal, dry_run=True)
    assert accepted["ok"] is False
    assert accepted["distilled"] is False
    assert accepted["crystal_written"] is False
    assert accepted["posted"] == 0


def test_cli_ingress_prep_accept_dry_run_never_calls_post():
    called: list[list[dict]] = []
    env = ingress.envelope(
        kind="turn",
        body="合成句：西仓门牌还在。",
        event_at="2026-05-06T07:08:09+00:00",
        role="assistant",
        session_id="sess-3",
        seq=2,
    )

    def boom(turns: list[dict]) -> dict:
        called.append(turns)
        raise AssertionError("dry-run must not post")

    result = ingress.accept(env, dry_run=True, post=boom)
    assert called == []
    assert result["posted"] == 0
    assert result["distilled"] is False
    assert result["crystal_written"] is False
    assert result["accepted"][0]["schema"] == ingress.SCHEMA


def test_cli_ingress_prep_accept_without_post_is_no_target():
    env = ingress.envelope(
        kind="turn",
        body="合成句：东堤潮位已记下。",
        event_at="2026-07-08T09:10:11+00:00",
        role="user",
        session_id="sess-4",
        seq=3,
    )
    result = ingress.accept(env, dry_run=False, post=None)
    assert result["refused"] == "no-target"
    assert result["posted"] == 0
    assert result["distilled"] is False
    assert result["crystal_written"] is False


def test_cli_ingress_prep_history_upload_goes_through_ingress(tmp_path):
    session = tmp_path / ".hermes" / "sessions"
    session.mkdir(parents=True)
    (tmp_path / ".hermes").mkdir(exist_ok=True)
    record = {
        "role": "assistant",
        "session_id": "sess-5",
        "timestamp": "2026-08-09T10:11:12+00:00",
        "content": "合成句：潮汐表只抄原句。",
    }
    (session / "session_demo.json").write_text(json.dumps({"messages": [record]}, ensure_ascii=False), encoding="utf-8")
    result = _run("history", "upload", "--dry-run", "--envelope", "--home", str(tmp_path), "--program", "Hermes")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["command"] == "history.upload"
    assert payload["dry_run"] is True
    assert payload["posted"] == 0
    assert payload["distilled"] is False
    assert payload["crystal_written"] is False
    assert payload["ingress"]["distilled"] is False
    assert payload["ingress"]["crystal_written"] is False
    assert payload["ingress"]["accepted"]


def test_cli_ingress_prep_modules_do_not_import_write_path():
    ingress_imports = _imports(ROOT / "rogmem" / "ingress.py")
    cli_imports = _imports(ROOT / "rogmem" / "cli.py")
    assert not (ingress_imports & BANNED_MODULES)
    assert not (cli_imports & BANNED_MODULES)
    ingress_text = (ROOT / "rogmem" / "ingress.py").read_text(encoding="utf-8")
    cli_text = (ROOT / "rogmem" / "cli.py").read_text(encoding="utf-8")
    assert "DISTILLER_STORE_WRITE_ENABLED" not in ingress_text
    assert "DISTILLER_STORE_WRITE_ENABLED" not in cli_text
    assert "crystal_store" not in ingress_text
    assert "distill_gate" not in ingress_text


def test_cli_ingress_stdin_is_utf8_when_the_process_code_page_is_legacy():
    body = "合成句：北坡灯塔还在闪。"
    env = ingress.envelope(
        kind="turn",
        body=body,
        event_at="2026-03-04T05:06:07+00:00",
        role="user",
        session_id="sess-utf8",
        seq=1,
        program="Cursor",
    )
    raw = json.dumps(env, ensure_ascii=False).encode("utf-8")
    child = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "gbk"}
    result = subprocess.run(
        [sys.executable, "-m", "rogmem", "ingress", "accept", "--dry-run", "--stdin"],
        cwd=ROOT,
        capture_output=True,
        env=child,
        input=raw,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.decode("utf-8"))
    assert payload["ok"] is True
    assert body in result.stdout.decode("utf-8")


def test_cli_ingress_prep_file_has_no_skip_posing_as_green():
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"skip", "skipif"}:
            raise AssertionError("cli_ingress_prep tests must not skip")
