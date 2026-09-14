"""Launch noninteractive media helpers without Windows console windows."""
from __future__ import annotations

import subprocess


def background_process_options() -> dict:
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "stdin": subprocess.DEVNULL,
    }
