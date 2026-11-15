"""docs/prompts 零差异硬闸门。

只验收真实目录 + 已提交快照 + PR 基线树，三者同一套文件集合和字节。
缺路径、空目录、空夹具、漏扫文件都失败，不得 skip。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

PROMPT_REL = "docs/prompts"
MANIFEST_REL = "tests/fixtures/docs_prompts_manifest.json"
_EXPIRY_PREFIX = "> **已过期。**".encode()
PREFERRED_BASES = ("origin/release/2.0", "release/2.0")
BASE_ENV = "ROGMEM_PROMPTS_BASE"


def default_repo() -> Path:
    return Path(__file__).resolve().parents[1]


def default_manifest_path(repo: Path | None = None) -> Path:
    return (repo or default_repo()) / MANIFEST_REL


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def without_expiry_banner(data: bytes) -> bytes:
    """过期标记不改提示正文。比对时拿掉文首那一行和紧跟的空行。"""
    if not data.startswith(_EXPIRY_PREFIX):
        return data
    rest = data.split(b"\n", 1)[1] if b"\n" in data else b""
    if rest.startswith(b"\n"):
        rest = rest[1:]
    return rest


def assert_prompt_dir_real(prompt_dir: Path) -> list[Path]:
    if not prompt_dir.exists():
        raise AssertionError(f"docs/prompts missing: {prompt_dir}")
    if not prompt_dir.is_dir():
        raise AssertionError(f"docs/prompts is not a directory: {prompt_dir}")
    files = sorted(path for path in prompt_dir.rglob("*") if path.is_file())
    if not files:
        raise AssertionError(f"docs/prompts is empty: {prompt_dir}")
    if not any(path.stat().st_size > 0 for path in files):
        raise AssertionError(f"docs/prompts has no non-empty file: {prompt_dir}")
    return files


def read_live_prompt_tree(repo: Path) -> dict[str, bytes]:
    root = repo.resolve()
    directory = root / PROMPT_REL
    files = assert_prompt_dir_real(directory)
    tree: dict[str, bytes] = {}
    for path in files:
        rel = path.relative_to(root).as_posix()
        tree[rel] = path.read_bytes()
    if not tree:
        raise AssertionError(f"docs/prompts walk produced no files: {directory}")
    return tree


def load_prompt_manifest(path: Path) -> dict[str, dict]:
    if not path.is_file():
        raise AssertionError(f"docs/prompts snapshot missing: {path}")
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise AssertionError(f"docs/prompts snapshot is empty: {path}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"docs/prompts snapshot is not JSON: {path}") from exc
    if not isinstance(data, dict):
        raise AssertionError(f"docs/prompts snapshot must be an object: {path}")
    if data.get("root") != PROMPT_REL:
        raise AssertionError(f"docs/prompts snapshot root must be {PROMPT_REL!r}: {path}")
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        raise AssertionError(f"docs/prompts snapshot files must be a non-empty object: {path}")
    parsed: dict[str, dict] = {}
    for rel, meta in files.items():
        if not isinstance(rel, str) or not rel.startswith(PROMPT_REL + "/"):
            raise AssertionError(f"snapshot path is not under {PROMPT_REL}: {rel!r}")
        if not isinstance(meta, dict):
            raise AssertionError(f"snapshot entry must be an object: {rel}")
        digest = meta.get("sha256")
        size = meta.get("size")
        if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise AssertionError(f"snapshot sha256 missing or empty: {rel}")
        if not isinstance(size, int) or size < 0:
            raise AssertionError(f"snapshot size missing: {rel}")
        parsed[rel] = {"sha256": digest, "size": size}
    if not parsed:
        raise AssertionError(f"docs/prompts snapshot parsed to zero files: {path}")
    return parsed


def assert_live_matches_manifest(live: dict[str, bytes], manifest: dict[str, dict]) -> None:
    live_paths = set(live)
    expected_paths = set(manifest)
    extra = sorted(live_paths - expected_paths)
    missing = sorted(expected_paths - live_paths)
    if extra or missing:
        raise AssertionError(
            "docs/prompts file set != snapshot: "
            f"extra={extra} missing={missing}"
        )
    mismatches = []
    for rel, data in sorted(live.items()):
        body = without_expiry_banner(data)
        got = {"sha256": sha256_bytes(body), "size": len(body)}
        if got != manifest[rel]:
            mismatches.append(f"{rel} live={got} snapshot={manifest[rel]}")
    if mismatches:
        raise AssertionError("docs/prompts bytes != snapshot:\n" + "\n".join(mismatches))


def _git(repo: Path, *args: str, binary: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=not binary,
    )


def git_ref_exists(repo: Path, ref: str) -> bool:
    result = _git(repo, "rev-parse", "--verify", ref)
    return result.returncode == 0


def resolve_pr_base(repo: Path) -> str:
    override = os.environ.get(BASE_ENV, "").strip()
    candidates = [override] if override else []
    github_base = os.environ.get("GITHUB_BASE_REF", "").strip()
    if github_base:
        candidates.append(f"origin/{github_base}")
        candidates.append(github_base)
    candidates.extend(PREFERRED_BASES)
    seen: set[str] = set()
    for ref in candidates:
        if not ref or ref in seen:
            continue
        seen.add(ref)
        if git_ref_exists(repo, ref):
            return ref
    raise AssertionError(
        "cannot resolve docs/prompts PR base "
        f"(tried {candidates}; set {BASE_ENV})"
    )


def read_git_prompt_tree(repo: Path, rev: str) -> dict[str, bytes]:
    listed = _git(repo, "ls-tree", "-r", "-z", "--name-only", rev, "--", PROMPT_REL)
    if listed.returncode != 0:
        raise AssertionError(
            f"git ls-tree {rev} -- {PROMPT_REL} failed: {listed.stderr.strip()}"
        )
    names = [name for name in listed.stdout.split("\0") if name]
    if not names:
        raise AssertionError(f"git {rev} has empty {PROMPT_REL}")
    tree: dict[str, bytes] = {}
    for rel in names:
        if not rel.startswith(PROMPT_REL + "/"):
            raise AssertionError(f"git ls-tree returned path outside {PROMPT_REL}: {rel}")
        shown = _git(repo, "show", f"{rev}:{rel}", binary=True)
        if shown.returncode != 0:
            err = shown.stderr.decode("utf-8", "replace") if shown.stderr else ""
            raise AssertionError(f"git show {rev}:{rel} failed: {err.strip()}")
        tree[rel] = shown.stdout
    if not tree:
        raise AssertionError(f"git {rev} produced no {PROMPT_REL} files")
    if not any(tree.values()):
        raise AssertionError(f"git {rev} {PROMPT_REL} files are all empty")
    return tree


def assert_live_matches_git(live: dict[str, bytes], expected: dict[str, bytes], rev: str) -> None:
    extra = sorted(set(live) - set(expected))
    missing = sorted(set(expected) - set(live))
    if extra or missing:
        raise AssertionError(
            f"docs/prompts file set != {rev}: extra={extra} missing={missing}"
        )
    changed = []
    for rel, data in sorted(live.items()):
        body = without_expiry_banner(data)
        baseline = without_expiry_banner(expected[rel])
        if body != baseline:
            changed.append(
                f"{rel} live={sha256_bytes(body)} {rev}={sha256_bytes(baseline)}"
            )
    if changed:
        raise AssertionError(
            f"docs/prompts bytes differ from {rev}:\n" + "\n".join(changed)
        )


def assert_git_diff_docs_prompts_clean(
    repo: Path,
    base: str,
    path: str = PROMPT_REL,
) -> None:
    """真 `git diff <base> -- docs/prompts`。空路径或空树不得当通过。"""
    if not isinstance(path, str) or not path.strip() or path != PROMPT_REL:
        raise AssertionError(
            f"refusing empty or wrong git diff path: {path!r} (must be {PROMPT_REL!r})"
        )
    files = assert_prompt_dir_real(repo / path)
    if not files:
        raise AssertionError(f"docs/prompts path exists but walk is empty: {repo / path}")
    listed = _git(repo, "ls-tree", "-r", "--name-only", base, "--", path)
    if listed.returncode != 0:
        raise AssertionError(
            f"git ls-tree {base} -- {path} failed: {listed.stderr.strip()}"
        )
    names = [line for line in listed.stdout.splitlines() if line.strip()]
    if not names:
        raise AssertionError(f"git {base} has empty {path}; empty diff is not a pass")
    live = read_live_prompt_tree(repo)
    expected = read_git_prompt_tree(repo, base)
    try:
        assert_live_matches_git(live, expected, base)
    except AssertionError as exc:
        raise AssertionError(
            f"docs/prompts git diff vs base is not empty:\n{exc}"
        ) from exc


def assert_docs_prompts_zero_diff(
    repo: Path | None = None,
    *,
    manifest_path: Path | None = None,
    git_base: str | None = None,
) -> None:
    root = (repo or default_repo()).resolve()
    live = read_live_prompt_tree(root)
    base = git_base or resolve_pr_base(root)
    assert_git_diff_docs_prompts_clean(root, base)
    manifest = load_prompt_manifest(manifest_path or default_manifest_path(root))
    assert_live_matches_manifest(live, manifest)
    expected = read_git_prompt_tree(root, base)
    assert_live_matches_git(live, expected, base)
    if not any(len(data) > 0 for data in live.values()):
        raise AssertionError("docs/prompts live tree has no non-empty file")
