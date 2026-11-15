"""Wipe ops precheck: dry-run default, backup gate, no prod wipe by accident.

Does not flip the write switch. Does not claim zero thick writes.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "wipe_thick_blobs_precheck.py"
WIPE_SQL = ROOT / "scripts" / "wipe_thick_blobs.sql"
RUNBOOK = ROOT / "docs" / "ops" / "wipe-thick-blobs.md"
CONFIRM = "I_HAVE_BACKUP_AND_ACCEPT_WIPE"
SCHEMA = (ROOT / "rogmem" / "schema.sql").read_text(encoding="utf-8")
BANNED_IMPORTS = {
    "rogmem.crystal_store",
    "rogmem.distill_gate",
    "rogmem.distill_resim",
    "rogmem.engine",
    "rogmem.prompts",
}


def _load_precheck():
    assert SCRIPT.is_file(), f"missing precheck script: {SCRIPT}"
    import importlib.util

    spec = importlib.util.spec_from_file_location("wipe_thick_blobs_precheck", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = {**os.environ, "PYTHONPATH": str(ROOT)}
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=merged,
        check=False,
    )


def _make_db(path: Path, *, thick: int = 1, thin: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for i in range(thick):
        con.execute(
            "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) "
            "VALUES(?,?,?,'active',?,?)",
            (
                f"thick-{i}",
                "人物",
                f"厚页{i}",
                json.dumps({"概述": f"厚摘要{i}"}, ensure_ascii=False),
                json.dumps({"事业经历": f"厚章节{i}"}, ensure_ascii=False),
            ),
        )
    for i in range(thin):
        con.execute(
            "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json,envelope_json,body_md) "
            "VALUES(?,?,?,'active','{}','{}',?,?)",
            (f"thin-{i}", "人物", f"瘦页{i}", '{"summary":"thin"}', "## 瘦\n\n正文"),
        )
    con.commit()
    con.close()
    return path


def _backup_of(live: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(live, dest)
    return dest


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _thick_count(db: Path) -> int:
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM crystallizations "
            "WHERE TRIM(COALESCE(summary_json,'')) NOT IN ('','{}') "
            "OR TRIM(COALESCE(chapters_json,'')) NOT IN ('','{}')"
        ).fetchone()
        return int(row[0])
    finally:
        con.close()


def test_default_is_dry_run_and_does_not_mutate(tmp_path):
    live = _make_db(tmp_path / "data" / "rogmem.sqlite", thick=2, thin=1)
    backup = _backup_of(live, tmp_path / "backups" / "rogmem.sqlite")
    before = live.read_bytes()
    result = _run("--db", str(live), "--backup", str(backup), "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["mode"] == "dry-run"
    assert report["mutated"] is False
    assert report["ready_to_wipe"] is True
    assert report["checks"]["backup"]["ok"] is True
    assert report["thick_rows"] == 2
    assert report["total_rows"] == 3
    assert len(report["dry_run_select"]) == 2
    assert all(row["id"].startswith("thick-") for row in report["dry_run_select"])
    assert live.read_bytes() == before
    assert _thick_count(live) == 2


def test_missing_backup_is_not_ready_and_does_not_wipe(tmp_path):
    live = _make_db(tmp_path / "rogmem.sqlite", thick=1, thin=0)
    before = live.read_bytes()
    result = _run("--db", str(live), "--json")
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["ready_to_wipe"] is False
    assert report["checks"]["backup"]["ok"] is False
    assert report["mutated"] is False
    assert "backup" in report["checks"]["backup"]["reason"]
    assert live.read_bytes() == before


def test_wipe_without_confirm_is_refused(tmp_path):
    live = _make_db(tmp_path / "rogmem.sqlite", thick=1, thin=1)
    backup = _backup_of(live, tmp_path / "rogmem.sqlite.bak")
    before = live.read_bytes()
    result = _run("--db", str(live), "--backup", str(backup), "--mode", "wipe", "--json")
    assert result.returncode == 2, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["mutated"] is False
    assert report["refused"] == "wipe_requires_explicit_confirm"
    assert live.read_bytes() == before
    assert _thick_count(live) == 1


def test_wipe_without_backup_is_refused(tmp_path):
    live = _make_db(tmp_path / "rogmem.sqlite", thick=1, thin=1)
    before = live.read_bytes()
    result = _run(
        "--db",
        str(live),
        "--mode",
        "wipe",
        "--confirm-wipe",
        CONFIRM,
        "--json",
    )
    assert result.returncode == 2, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["mutated"] is False
    assert report["refused"] == "wipe_requires_backup"
    assert live.read_bytes() == before


def test_confirmed_wipe_clears_thick_blobs_only_when_gated(tmp_path):
    live = _make_db(tmp_path / "rogmem.sqlite", thick=2, thin=1)
    backup = _backup_of(live, tmp_path / "rogmem.sqlite.bak")
    result = _run(
        "--db",
        str(live),
        "--backup",
        str(backup),
        "--mode",
        "wipe",
        "--confirm-wipe",
        CONFIRM,
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["mutated"] is True
    assert report["mode"] == "wipe"
    assert report["thick_rows_after"] == 0
    assert _thick_count(live) == 0
    con = sqlite3.connect(live)
    try:
        ids = [row[0] for row in con.execute("SELECT id FROM crystallizations ORDER BY id")]
        assert ids == ["thick-0", "thick-1", "thin-0"]
        thin = con.execute(
            "SELECT envelope_json, body_md FROM crystallizations WHERE id='thin-0'"
        ).fetchone()
        assert thin[0] == '{"summary":"thin"}'
        assert thin[1] == "## 瘦\n\n正文"
    finally:
        con.close()
    assert "零厚写" not in result.stdout or "勿称" in result.stdout + result.stderr
    assert report["zero_thick_writes_claimed"] is False


def test_rollback_without_backup_is_forbidden(tmp_path):
    live = _make_db(tmp_path / "rogmem.sqlite", thick=1, thin=0)
    before = live.read_bytes()
    result = _run("--db", str(live), "--mode", "rollback", "--json")
    assert result.returncode == 2, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["mutated"] is False
    assert report["refused"] == "rollback_without_backup_forbidden"
    assert live.read_bytes() == before


def test_rollback_with_backup_does_not_auto_restore(tmp_path):
    live = _make_db(tmp_path / "rogmem.sqlite", thick=1, thin=0)
    backup = _backup_of(live, tmp_path / "rogmem.sqlite.bak")
    before = live.read_bytes()
    result = _run(
        "--db",
        str(live),
        "--backup",
        str(backup),
        "--mode",
        "rollback",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["mutated"] is False
    assert report["rollback_automatic"] is False
    assert "restore" in report
    assert live.read_bytes() == before


def test_spot_check_is_read_only_and_does_not_claim_zero_thick(tmp_path):
    live = _make_db(tmp_path / "rogmem.sqlite", thick=0, thin=2)
    before = live.read_bytes()
    result = _run("--db", str(live), "--mode", "spot-check", "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["mode"] == "spot-check"
    assert report["mutated"] is False
    assert report["thick_rows"] == 0
    assert report["zero_thick_writes_claimed"] is False
    assert report["deploy_gate"] == "still_blocked_until_prod_wipe"
    assert live.read_bytes() == before


def test_backup_must_not_be_the_live_database(tmp_path):
    live = _make_db(tmp_path / "rogmem.sqlite", thick=1, thin=0)
    result = _run("--db", str(live), "--backup", str(live), "--json")
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["ready_to_wipe"] is False
    assert report["checks"]["backup"]["ok"] is False
    assert "live" in report["checks"]["backup"]["reason"]


def test_script_stays_off_write_paths():
    found = _imports(SCRIPT)
    banned = found & BANNED_IMPORTS
    assert not banned, banned
    text = SCRIPT.read_text(encoding="utf-8")
    assert "DISTILLER_STORE_WRITE_ENABLED" not in text
    assert "summary_json=?" not in text
    assert "chapters_json=?" not in text


def test_wipe_sql_stays_gated_and_is_the_destructive_source():
    text = WIPE_SQL.read_text(encoding="utf-8")
    assert "UPDATE crystallizations SET summary_json='{}', chapters_json='{}';" in text
    assert "备份" in text or "backup" in text.lower()
    assert "预检" in text or "precheck" in text.lower()
    assert "勿称" in text
    assert "回滚" in text or "rollback" in text.lower()
    precheck = SCRIPT.read_text(encoding="utf-8")
    assert "wipe_thick_blobs.sql" in precheck


def test_runbook_documents_backup_gate_and_deploy_hold():
    text = RUNBOOK.read_text(encoding="utf-8")
    for needle in (
        "备份",
        "无备份不得回滚",
        "预检",
        "dry-run",
        "抽查",
        "知情接受",
        "真上线仍卡 wipe",
        "勿称零厚写",
        "不翻写开关",
    ):
        assert needle in text, needle
    assert "wipe_thick_blobs.sql" in text
    assert "wipe_thick_blobs_precheck.py" in text


def test_rogmem_data_default_db_path(tmp_path):
    live = _make_db(tmp_path / "rogmem.sqlite", thick=1, thin=0)
    backup = _backup_of(live, tmp_path / "bak.sqlite")
    result = _run(
        "--backup",
        str(backup),
        "--json",
        env={"ROGMEM_DATA": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["db"] == str(live.resolve())
    assert report["thick_rows"] == 1


def test_precheck_module_counts_thick_rows(tmp_path):
    live = _make_db(tmp_path / "rogmem.sqlite", thick=3, thin=2)
    module = _load_precheck()
    report = module.precheck(db=live, backup=None, mode="dry-run")
    assert report["thick_rows"] == 3
    assert report["total_rows"] == 5
    assert report["mutated"] is False
    assert report["ready_to_wipe"] is False
