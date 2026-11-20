"""
core/console_log.py - keep a copy of Vision's console output in a file.

Everything Vision prints ("[VISION] Connected.", "[Actions] Action loaded: ...")
goes to the terminal it was started from, as before, and also to
~/.local/state/vision/vision.log. The built-in terminals can then show it:
＋ LOG in the terminal panel runs `tail -F` on that file.

Each start begins a new file; the previous run is kept as vision.log.1. The
file is cut back when it passes MAX_BYTES, so a long session cannot fill the
disk. Writing to the file can never break printing: any error there is
swallowed and the console keeps working.

Only output that goes through Python's sys.stdout and sys.stderr is copied.
Lines that Qt or other C libraries write straight to the console are not.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024


def log_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "vision" / "vision.log"


class _Tee:
    """Writes to the real stream, then to the shared log file."""

    def __init__(self, stream, sink: "_Sink"):
        self._stream, self._sink = stream, sink

    def write(self, text):
        n = self._stream.write(text) if self._stream is not None else len(text)
        self._sink.write(text)
        return n

    def flush(self):
        if self._stream is not None:
            self._stream.flush()
        self._sink.flush()

    def __getattr__(self, name):
        # encoding, isatty, fileno, reconfigure, ... come from the real stream
        return getattr(self._stream, name)


class _Sink:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._f = open(path, "a", encoding="utf-8", errors="replace", buffering=1)

    def write(self, text) -> None:
        try:
            with self._lock:
                self._f.write(str(text))
                if self._f.tell() > MAX_BYTES:
                    # tail -F notices the truncation and carries on.
                    self._f.seek(0)
                    self._f.truncate()
                    self._f.write("[log] cut back to keep the file small\n")
        except Exception:
            pass

    def flush(self) -> None:
        try:
            with self._lock:
                self._f.flush()
        except Exception:
            pass


_installed: Path | None = None


def install() -> Path | None:
    """Start copying stdout and stderr to the log file. Safe to call twice.
    Returns the file's path, or None when it could not be opened."""
    global _installed
    if _installed is not None:
        return _installed
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            os.replace(path, path.with_name(path.name + ".1"))
        sink = _Sink(path)
    except Exception as e:
        print(f"[log] console copy not started: {type(e).__name__}: {e}")
        return None
    sys.stdout = _Tee(sys.stdout, sink)
    sys.stderr = _Tee(sys.stderr, sink)
    _installed = path
    return path
