"""core/paths.py — where the app lives on disk.

One answer to "which folder is VISION installed in?", so the modules that need
it stop carrying their own copy of the same four lines. Standard library only,
and nothing else from this project is imported here, so any module may import
it without risking an import cycle.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def base_dir() -> Path:
    """The project folder: the one holding main.py, config/, core/ and actions/.

    For a frozen build it is the folder of the executable instead."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def read_gemini_api_key(config_path: Path) -> str:
    """The Gemini key stored in the given api_keys.json.

    Raises exactly what reading the file by hand would: OSError when the file
    is missing, ValueError when it is not JSON, KeyError when no key is saved.
    Callers rely on that to tell "not set up yet" from a usable key."""
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]
