"""docs/prompts 零差异闸门必须是真比对，不能只看文件在不在。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.docs_prompts_zero_diff import (
    PROMPT_REL,
    assert_docs_prompts_zero_diff,
    assert_git_diff_docs_prompts_clean,
    assert_prompt_dir_real,
    default_manifest_path,
    default_repo,
    load_prompt_manifest,
    read_git_prompt_tree,
    read_live_prompt_tree,
    sha256_bytes,
)


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _manifest_for(files: dict[str, bytes]) -> dict:
    return {
        "root": PROMPT_REL,
        "files": {
            rel: {"sha256": sha256_bytes(data), "size": len(data)}
            for rel, data in files.items()
        },
    }


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


def _init_repo(root: Path, files: dict[str, bytes]) -> None:
    _write_tree(root, files)
    _git(root, "init")
    _git(root, "config", "user.email", "zero-diff@test")
    _git(root, "config", "user.name", "zero-diff")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base prompts")


SAMPLE = {
    "docs/prompts/README.md": b"# prompts\n",
    "docs/prompts/wiki-distill-draft-prompt-v5.md": b"draft body\n",
    "docs/prompts/wiki-integrate-adjudicate-prompt-v1.md": b"integrate body\n",
    "docs/prompts/nested/extra.txt": b"keep nested files\n",
}


def test_clean_repo_tree_matches_snapshot_and_pr_base():
    assert_docs_prompts_zero_diff()
    repo = default_repo()
    live = read_live_prompt_tree(repo)
    assert live, "live docs/prompts must be non-empty"
    assert all(rel.startswith("docs/prompts/") for rel in live)
    manifest = load_prompt_manifest(default_manifest_path(repo))
    assert manifest
    assert set(live) == set(manifest)
    assert (repo / "docs" / "prompts").is_dir()


def test_missing_directory_fails(tmp_path):
    with pytest.raises(AssertionError, match="missing"):
        assert_prompt_dir_real(tmp_path / "docs" / "prompts")
    with pytest.raises(AssertionError, match="missing"):
        read_live_prompt_tree(tmp_path)


def test_empty_directory_fails(tmp_path):
    (tmp_path / "docs" / "prompts").mkdir(parents=True)
    with pytest.raises(AssertionError, match="empty"):
        read_live_prompt_tree(tmp_path)


def test_all_empty_files_fail(tmp_path):
    _write_tree(tmp_path, {"docs/prompts/README.md": b""})
    with pytest.raises(AssertionError, match="no non-empty file"):
        read_live_prompt_tree(tmp_path)


def test_walk_includes_nested_and_non_md(tmp_path):
    _write_tree(tmp_path, SAMPLE)
    live = read_live_prompt_tree(tmp_path)
    assert set(live) == set(SAMPLE)
    assert live["docs/prompts/nested/extra.txt"] == SAMPLE["docs/prompts/nested/extra.txt"]


def test_missing_manifest_fails(tmp_path):
    with pytest.raises(AssertionError, match="snapshot missing"):
        load_prompt_manifest(tmp_path / "nope.json")


def test_empty_manifest_fails(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(AssertionError, match="snapshot is empty"):
        load_prompt_manifest(empty)
    blank = tmp_path / "blank.json"
    blank.write_text(json.dumps({"root": "docs/prompts", "files": {}}), encoding="utf-8")
    with pytest.raises(AssertionError, match="non-empty object"):
        load_prompt_manifest(blank)


def test_edited_file_fails_snapshot_and_git(tmp_path):
    _init_repo(tmp_path, SAMPLE)
    manifest_path = tmp_path / "tests" / "fixtures" / "docs_prompts_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(_manifest_for(SAMPLE)), encoding="utf-8")
    assert_docs_prompts_zero_diff(tmp_path, manifest_path=manifest_path, git_base="HEAD")

    target = tmp_path / "docs" / "prompts" / "wiki-distill-draft-prompt-v5.md"
    target.write_bytes(b"draft body\nCHANGED\n")
    with pytest.raises(AssertionError, match="git diff vs base is not empty"):
        assert_docs_prompts_zero_diff(tmp_path, manifest_path=manifest_path, git_base="HEAD")


def test_added_or_removed_file_fails(tmp_path):
    _init_repo(tmp_path, SAMPLE)
    manifest_path = tmp_path / "snap.json"
    manifest_path.write_text(json.dumps(_manifest_for(SAMPLE)), encoding="utf-8")

    extra = tmp_path / "docs" / "prompts" / "new.md"
    extra.write_bytes(b"new file\n")
    with pytest.raises(AssertionError, match="git diff vs base is not empty"):
        assert_docs_prompts_zero_diff(tmp_path, manifest_path=manifest_path, git_base="HEAD")
    extra.unlink()

    (tmp_path / "docs" / "prompts" / "README.md").unlink()
    with pytest.raises(AssertionError, match="git diff vs base is not empty"):
        assert_docs_prompts_zero_diff(tmp_path, manifest_path=manifest_path, git_base="HEAD")


def test_git_base_diff_fails_even_if_snapshot_updated(tmp_path):
    _init_repo(tmp_path, SAMPLE)
    target = tmp_path / "docs" / "prompts" / "README.md"
    target.write_bytes(b"# prompts\nedited against base\n")
    edited = dict(SAMPLE)
    edited["docs/prompts/README.md"] = target.read_bytes()
    manifest_path = tmp_path / "snap.json"
    manifest_path.write_text(json.dumps(_manifest_for(edited)), encoding="utf-8")
    with pytest.raises(AssertionError, match="git diff vs base is not empty"):
        assert_docs_prompts_zero_diff(tmp_path, manifest_path=manifest_path, git_base="HEAD")


def test_git_tree_empty_fails(tmp_path):
    _init_repo(tmp_path, {"docs/other.md": b"not prompts\n"})
    with pytest.raises(AssertionError, match="empty"):
        read_git_prompt_tree(tmp_path, "HEAD")


def test_unresolved_pr_base_fails(tmp_path, monkeypatch):
    _init_repo(tmp_path, SAMPLE)
    monkeypatch.delenv("ROGMEM_PROMPTS_BASE", raising=False)
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    from tests import docs_prompts_zero_diff as gate

    with pytest.raises(AssertionError, match="cannot resolve docs/prompts PR base"):
        gate.resolve_pr_base(tmp_path)


def test_git_diff_rejects_empty_or_wrong_path(tmp_path):
    _init_repo(tmp_path, SAMPLE)
    with pytest.raises(AssertionError, match="empty or wrong git diff path"):
        assert_git_diff_docs_prompts_clean(tmp_path, "HEAD", path="")
    with pytest.raises(AssertionError, match="empty or wrong git diff path"):
        assert_git_diff_docs_prompts_clean(tmp_path, "HEAD", path="   ")
    with pytest.raises(AssertionError, match="empty or wrong git diff path"):
        assert_git_diff_docs_prompts_clean(tmp_path, "HEAD", path="docs/prompt")


def test_committed_prompt_change_fails_git_diff_vs_base(tmp_path):
    """工作区干净但已提交的 prompt 改动，对 base 的 git diff 仍必须红。"""
    _init_repo(tmp_path, SAMPLE)
    _git(tmp_path, "branch", "release/2.0")
    target = tmp_path / "docs" / "prompts" / "README.md"
    target.write_bytes(b"# prompts\ncommitted change\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "touch prompts")
    manifest_path = tmp_path / "snap.json"
    edited = dict(SAMPLE)
    edited["docs/prompts/README.md"] = target.read_bytes()
    manifest_path.write_text(json.dumps(_manifest_for(edited)), encoding="utf-8")
    porcelain = _git(tmp_path, "status", "--porcelain", "--", "docs/prompts")
    assert porcelain.stdout.strip() == ""
    with pytest.raises(AssertionError, match="git diff vs base is not empty"):
        assert_docs_prompts_zero_diff(
            tmp_path, manifest_path=manifest_path, git_base="release/2.0"
        )


def test_NL_PROMPT_02_real_tree_edit_must_fail():
    """故意改真 docs/prompts 树时，零差异测必须红；还原后必须绿。"""
    repo = default_repo()
    target = repo / "docs" / "prompts" / "README.md"
    original = target.read_bytes()
    assert original, "real docs/prompts/README.md must be non-empty"
    try:
        target.write_bytes(original + b"\n# NL-PROMPT-02 probe\n")
        with pytest.raises(AssertionError, match="git diff vs base is not empty") as exc:
            assert_docs_prompts_zero_diff()
        assert "docs/prompts" in str(exc.value)
        assert "git diff" in str(exc.value)
    finally:
        target.write_bytes(original)
    assert target.read_bytes() == original
    assert_docs_prompts_zero_diff()
