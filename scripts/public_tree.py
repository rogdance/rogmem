"""Paths allowed into rogdance/rogmem, and the paths that must stay here.

Run this before any copy to the public repository. Exit status is 0 only when
every tracked file is classified and none of the blocked text sits in a public
file. This script does not copy, push, or change repository visibility.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Exact files and prefixes that may be copied. Anything else stays private.
ALLOW_FILES = (
    ".gitignore",
    "LICENSE",
    "README.md",
    "Dockerfile",
    "requirements.txt",
    "pytest.ini",
    "compose.yml",
    "docs/install.md",
    "docs/CURRENT.md",
    "docs/brand/rogmem-lockup.svg",
    ".github/workflows/ci.yml",
    "deploy/build_compose.py",
    "deploy/__init__.py",
    "deploy/极空间-rogmem-compose.yml",
    "scripts/public_tree.py",
    "scripts/install_zspace.py",
    "scripts/__init__.py",
)
ALLOW_PREFIXES = (
    "rogmem/",
    "installer/",
    "host/dsh/",
    "docs/prompts/",
    "skills/rogmem-readme/",
    "tests/",
    ".github/",
)

# Denied wins over allowed. These are the private materials from the extract plan.
DENY_FILES = (
    "docs/brand/rogdance.jpeg",
    "rogmem/BUILD_STAMP",
    "rogmem/host_client.whl",
    "rogmem/host_client.tar.gz",
    "docs/rogmem-tech-architecture-v3.md",
    "docs/rogmem-方案审阅-v1.md",
    "docs/rogmem-架构CTO评审-v1.md",
    "docs/sim-dual-resume-report.md",
    "AGENTS.md",
    "scripts/wipe_thick_blobs.sql",
)
DENY_PREFIXES = (
    "tests/fixtures/dual_resume/",
    "docs/bench/",
    "docs/reports/",
    "docs/superpowers/",
    "docs/ops/",
    "skills/zspace-release/",
    "skills/rogmem-ship/",
    "skills/rogmem-lines/",
    "skills/loop-task-design/",
    ".onemem/",
    ".cursor/",
)

# A public test that still names these would fail once the private files are gone.
TEST_DROP_MARKERS = (
    "fixtures/dual_resume",
    "run_dual_resume_resim",
    "run_offline_resim",
    "sim-dual-resume-report",
    "draft-en.json",
    "en-lin-zhiyuan",
    "docs/bench/",
    "docs/reports/",
    "docs/superpowers/",
    "skills/zspace-release/",
    "skills/rogmem-ship/",
    "rogdance.jpeg",
    ".onemem/",
    "loop-task-design/",
)

REQUIRED_PUBLIC = (
    ".gitignore",
    "LICENSE",
    "README.md",
    "Dockerfile",
    "requirements.txt",
    "pytest.ini",
    "compose.yml",
    "docs/install.md",
    "docs/CURRENT.md",
    "docs/brand/rogmem-lockup.svg",
    "docs/prompts/wiki-distill-draft-prompt-v5.md",
    "docs/prompts/wiki-integrate-adjudicate-prompt-v1.md",
    "rogmem/app.py",
    "rogmem/onboard.py",
    "rogmem/vendor/weknora/LICENSE",
    "rogmem/vendor/weknora/NOTICE",
    "rogmem/vendor/cosmos-graph.min.js",
    "rogmem/vendor/cosmos-graph.LICENCE",
    "installer/COLD_START.md",
    "host/dsh/package.json",
    "deploy/build_compose.py",
    "scripts/install_zspace.py",
    "scripts/public_tree.py",
    ".github/workflows/ci.yml",
)


def forbidden_addresses() -> tuple[str, ...]:
    """Contiguous strings that must not appear in a public file."""
    return (
        "nas-" + "18080.example.cn",
        "192.168." + "0.1",
    )


def contact_needles() -> tuple[str, ...]:
    return (
        "1234" + "5678",
        "138-0000-" + "0000",
        "1380000" + "0000",
    )


def cookie_needles() -> tuple[str, ...]:
    return ("Chrome Safe " + "Storage",)


def _norm(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def _matches(path: str, item: str) -> bool:
    if item.endswith("/"):
        return path.startswith(item) or path + "/" == item
    return path == item


def is_denied(path: str) -> bool:
    path = _norm(path)
    if any(_matches(path, item) for item in DENY_FILES):
        return True
    return any(_matches(path, item) for item in DENY_PREFIXES)


def is_allowed(path: str) -> bool:
    path = _norm(path)
    if is_denied(path):
        return False
    if any(_matches(path, item) for item in ALLOW_FILES):
        return True
    return any(_matches(path, item) for item in ALLOW_PREFIXES)


def tracked_files(root: Path | None = None) -> list[str]:
    root = root or ROOT
    result = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    found = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        path = raw.decode()
        if path.endswith("/"):
            continue
        found.append(_norm(path))
    return found


def _read(root: Path, path: str) -> str:
    file = root / path
    if not file.is_file():
        return ""
    return file.read_text(encoding="utf-8", errors="replace")


def test_depends_on_private(path: str, text: str) -> bool:
    if not path.startswith("tests/"):
        return False
    return any(marker in text for marker in TEST_DROP_MARKERS)


def blocked_text(text: str) -> list[str]:
    found = []
    for needle in forbidden_addresses():
        if needle in text:
            found.append("address")
    lowered = text.lower()
    for needle in contact_needles():
        if needle.lower() in lowered:
            found.append("contact")
    for needle in cookie_needles():
        if needle in text:
            found.append("cookie")
    return found


def public_paths(root: Path | None = None) -> list[str]:
    root = root or ROOT
    kept = []
    for path in tracked_files(root):
        if not is_allowed(path):
            continue
        if test_depends_on_private(path, _read(root, path)):
            continue
        kept.append(path)
    return kept


def violations(root: Path | None = None) -> list[str]:
    root = root or ROOT
    problems = []
    public = set(public_paths(root))
    tracked = tracked_files(root)
    for path in tracked:
        if is_denied(path) and path in public:
            problems.append(f"禁止路径进入了公开清单：{path}")
    for path in REQUIRED_PUBLIC:
        if path not in public:
            problems.append(f"公开清单缺少：{path}")
        if is_denied(path):
            problems.append(f"必选路径被标成禁止：{path}")
    for path in sorted(public):
        kinds = blocked_text(_read(root, path))
        if kinds:
            problems.append(f"公开文件含有禁止文本（{'/'.join(dict.fromkeys(kinds))}）：{path}")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    listing = "--list" in args
    public = public_paths()
    problems = violations()
    if listing:
        for path in public:
            print(path)
    print(f"public {len(public)}  violations {len(problems)}", file=sys.stderr)
    for item in problems:
        print(item, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
