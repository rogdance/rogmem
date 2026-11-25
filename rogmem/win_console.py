"""Run OS console helpers without flashing a terminal window.

``netstat`` / ``bcdedit`` / ``taskkill`` are console-subsystem executables. When
rogmem runs detached and Windows Terminal is the default terminal, each call
inherits (or creates) a console: conhost + OpenConsole + WindowsTerminal
-Embedding appear and a window flickers on screen. A door that polls on every
request turns that into a visible storm.

CREATE_NO_WINDOW gives the child its own hidden console. On POSIX the flag does
not exist and the call stays byte-for-byte the same.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def is_windows() -> bool:
    """One switch for every Windows-only console decision in the host client."""
    return os.name == "nt"


def silent_run(command: list[str], **kwargs: Any):
    """``subprocess.run`` with captured output and no visible console on Windows."""
    options: dict[str, Any] = {"check": False, "capture_output": True}
    options.update(kwargs)
    if is_windows():
        options["creationflags"] = options.get("creationflags", 0) | CREATE_NO_WINDOW
    return subprocess.run(command, **options)
