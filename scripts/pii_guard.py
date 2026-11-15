"""Refuse to publish a personal value *shape* in the public tree.

Only value shapes are written down here: a personal mailbox and a standalone
Chinese mobile number. Site-specific values — real name, organisation, account
handle, private mailbox, DDNS host — are never written down at all. They are
handed in for one run through ROGMEM_PRIVATE_NEEDLES, exactly like
scripts/public_tree.py, so a clean machine holds no personal value in its
source tree.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Site-specific values (real name, handle, private mailbox, ...) supplied at run
# time and never committed. Comma separated, e.g. ROGMEM_PRIVATE_NEEDLES="a,b".
PRIVATE_NEEDLES_ENV = "ROGMEM_PRIVATE_NEEDLES"

# Mailbox domains the public tree may mention. Anything else is a leak: the
# public source only ever talks about reserved examples.
_RESERVED_MAIL = (
    "example.com",
    "example.test",
    "example.cn",
    "example.org",
    "example.net",
    "github.com",
    "users.noreply.github.com",
    "localhost",
)

# Values the public fixtures use on purpose; they must not trip the mobile
# shape check. A blanket phone scan would flag every one of them.
_ALLOW_VALUES = (
    "138" + "00000000",
)

# Only shape classes with no legitimate public occurrence belong here. A
# standalone mobile is one: a bare 11-digit window inside a longer digit run
# (SVG path data, minified vendor code) is not a phone number.
_PATTERNS = (
    ("mobile", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
)

_MAIL = re.compile(r"(?i)[a-z0-9._%+-]+@([a-z0-9.-]+\.[a-z]{2,})")


def private_needles() -> tuple[str, ...]:
    """Site values handed in for one run through ROGMEM_PRIVATE_NEEDLES."""
    raw = os.environ.get(PRIVATE_NEEDLES_ENV, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _tracked(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [raw.decode() for raw in result.stdout.split(b"\0") if raw]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def scan_text(text: str, needles: tuple[str, ...] = ()) -> list[str]:
    """Return the kinds of blocked shape found in `text`."""
    found: list[str] = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            if match.group(0) in _ALLOW_VALUES:
                continue
            found.append(kind)
            break
    for match in _MAIL.finditer(text):
        domain = match.group(1).lower().rstrip(".")
        if not any(domain == item or domain.endswith("." + item) for item in _RESERVED_MAIL):
            found.append("mailbox")
            break
    lowered = text.lower()
    for needle in needles:
        if needle.lower() in lowered:
            found.append("needle")
            break
    return list(dict.fromkeys(found))


def violations(root: Path | None = None) -> list[str]:
    root = root or ROOT
    needles = private_needles()
    problems: list[str] = []
    for rel in sorted(_tracked(root)):
        kinds = scan_text(_read(root / rel), needles)
        if kinds:
            problems.append(f"{rel}: {'/'.join(kinds)}")
    return problems


def main(argv: list[str] | None = None) -> int:
    _ = argv
    files = _tracked(ROOT)
    problems = violations()
    print(f"scanned {len(files)}  violations {len(problems)}", file=sys.stderr)
    for item in problems:
        print(item, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
