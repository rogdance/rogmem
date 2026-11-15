"""The pre-release copy may replace only its own database."""

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "skills" / "zspace-release" / "pre_copy.py"
_SPEC = importlib.util.spec_from_file_location("pre_copy", _PATH)
pre_copy = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(pre_copy)


def test_snapshot_can_land_on_the_pre_database(tmp_path: Path):
    live = tmp_path / "rogmem-data" / "rogmem.sqlite"
    backup = tmp_path / "snapshot" / "rogmem.sqlite"
    pre_db = tmp_path / "rogmem-pre-data" / "rogmem.sqlite"
    live.parent.mkdir()
    live.write_bytes(b"live")
    backup.parent.mkdir()
    backup.write_bytes(b"copy")
    pre_db.parent.mkdir()
    pre_db.write_bytes(b"old")
    assert pre_copy.snapshot_refusal(live, backup, pre_db) == ""


def test_snapshot_refuses_to_cover_the_production_database(tmp_path: Path):
    live = tmp_path / "rogmem-data" / "rogmem.sqlite"
    live.parent.mkdir()
    live.write_bytes(b"live")
    assert "生产库" in pre_copy.snapshot_refusal(live, live, tmp_path / "rogmem-pre-data" / "rogmem.sqlite")
    assert "生产库" in pre_copy.snapshot_refusal(live, tmp_path / "snapshot" / "rogmem.sqlite", live)


def test_snapshot_refuses_a_destination_outside_pre(tmp_path: Path):
    live = tmp_path / "rogmem-data" / "rogmem.sqlite"
    backup = tmp_path / "snapshot" / "rogmem.sqlite"
    other = tmp_path / "rogmem-data-copy" / "rogmem.sqlite"
    for path in (live, backup, other):
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"x")
    assert "预发布" in pre_copy.snapshot_refusal(live, backup, other)
