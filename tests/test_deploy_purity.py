"""Compose artifacts must reference a prebuilt image, not embed business source."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = (
    ROOT / "compose.yml",
    ROOT / "deploy" / "极空间-rogmem-compose.yml",
)
BUILDER = ROOT / "deploy" / "build_compose.py"
IMAGE_TAG = "rogmem:local"
SIZE_LIMIT = 20_000


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def test_compose_files_have_no_embedded_gzip_payload():
    for path in COMPOSE_FILES:
        text = _read(path)
        assert path.is_file(), path
        assert "H4sI" not in text
        assert "rogmem-pack" not in text
        assert len(text) < SIZE_LIMIT
        assert path.stat().st_size < SIZE_LIMIT


def test_compose_files_reference_documented_image():
    for path in COMPOSE_FILES:
        text = _read(path)
        assert f"image: {IMAGE_TAG}" in text
        assert "image: python:3.12-slim" not in text
        assert "command:" not in text
        assert "ROGMEM_NO_PASSWORD:" not in text
        assert "ROGMEM_NO_PASSWORD=" not in text
        assert "ROGMEM_DATA: /data" in text
        assert "ROGMEM_SCAN: /scan" in text
        assert "18080:18080" in text
        assert 'cpus: "2.0"' in text
        assert "mem_limit: 4g" in text
        assert 'cpus: "6.0"' not in text
        assert "mem_limit: 12g" not in text
        assert 'ROGMEM_BUDGET: "0.5"' in text
        assert "127.0.0.1:10000" not in text
        assert "rogmem-data:/data" in text
        assert "rogmem-scan:/scan" in text


def test_builder_does_not_pack_sources():
    source = _read(BUILDER)
    assert "_pack_files" not in source
    assert "gzip.compress" not in source
    assert "base64.b64encode" not in source
    assert "rogmem-unpack" not in source
    assert IMAGE_TAG in source


def test_builder_regenerates_committed_compose():
    from deploy import build_compose

    rendered = build_compose.render()
    assert "H4sI" not in rendered
    assert "rogmem-pack" not in rendered
    assert f"image: {IMAGE_TAG}" in rendered
    assert "127.0.0.1:10000" not in rendered
    assert len(rendered) < SIZE_LIMIT
    for path in COMPOSE_FILES:
        assert _read(path) == rendered
    custom = build_compose.render(cpus="1.5", mem_limit="2g", budget="0.25", hf_endpoint="https://example.invalid")
    assert 'cpus: "1.5"' in custom
    assert "mem_limit: 2g" in custom
    assert 'ROGMEM_BUDGET: "0.25"' in custom
    assert "HF_ENDPOINT: https://example.invalid" in custom
    assert "18080:18080" in custom


def test_dockerfile_holds_runtime_deps():
    import re

    from rogmem.distill_resim import DRAFT_PROMPT_PATH, INTEGRATE_PROMPT_PATH, ROOT as PKG_ROOT
    from rogmem.runtime_files import REQUIRED_RUNTIME_FILES, missing_runtime_files

    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "libgomp1" in text
    assert "venv" in text
    assert "COPY rogmem" in text
    copies = re.findall(r"^COPY\s+(\S+)\s+", text, re.M)
    assert missing_runtime_files() == []
    for path in (DRAFT_PROMPT_PATH, INTEGRATE_PROMPT_PATH):
        rel = path.relative_to(PKG_ROOT).as_posix()
        assert rel in REQUIRED_RUNTIME_FILES
        assert any(rel == src or rel.startswith(src.rstrip("/") + "/") for src in copies)
    assert "COPY docs/prompts" in text
    assert "ARG HF_ENDPOINT=" in text
    compose = _read(COMPOSE_FILES[0])
    assert "libgomp" not in compose
    assert "apt-get" not in compose
    assert "pip install" not in compose


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")
def test_docker_build_smoke():
    subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, "."],
        cwd=ROOT,
        check=True,
    )
