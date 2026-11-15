"""Files the server reads from the repo root, outside the rogmem package.

The image must COPY each of these. connect() refuses to start if one is missing,
so a forgotten COPY fails at boot instead of on the first distill.
"""

from __future__ import annotations

from pathlib import Path

REQUIRED_RUNTIME_FILES = (
    "docs/prompts/wiki-distill-draft-prompt-v5.md",
    "docs/prompts/wiki-integrate-adjudicate-prompt-v1.md",
)


def package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def missing_runtime_files(root: Path | None = None) -> list[str]:
    base = root or package_root()
    return [rel for rel in REQUIRED_RUNTIME_FILES if not (base / rel).is_file()]


def assert_runtime_files(root: Path | None = None) -> None:
    missing = missing_runtime_files(root)
    if missing:
        raise FileNotFoundError("运行时要读的文件不在程序旁边，打包时漏了：" + "、".join(missing))
