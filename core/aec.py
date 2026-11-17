"""core/aec.py - the desktop's own echo canceller in front of Vision's microphone.

Why it is needed
----------------
To stop talking when Kostas talks over it, Vision has to hear him while its own
voice is coming out of the speakers. The microphone hears both. `core/echo.py`
tells them apart well when our echo is quiet (headphones, a mic far from the
speakers), but on open desk speakers our echo is as loud as a person and no
check on the microphone alone can separate them reliably.

A real echo canceller can: it subtracts what is being played from what the mic
hears, sample by sample. PipeWire (Ubuntu's sound server) ships one, the same
WebRTC canceller video-call apps use, as `module-echo-cancel`. Nothing has to be
installed for it.

What this does
--------------
1. Finds Vision's own playback and recording streams (by process id).
2. Loads `module-echo-cancel` once, on top of the speaker and microphone those
   streams are using right now. That adds a "Vision echo cancel" sink and
   source; nothing else on the desktop is moved or changed.
3. Moves Vision's two streams onto them, so the canceller hears exactly what
   Vision plays and cleans exactly what Vision records.
4. Unloads the module when Vision closes.

Everything goes through `pactl`, so it works on PipeWire and on plain
PulseAudio. If `pactl` is missing or the streams do not go through the sound
server (a raw hardware device picked in settings), it says so once and Vision
carries on without it: talking over it then works with headphones only.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from typing import Callable, Optional

SINK_NAME = "vision_aec_sink"
SOURCE_NAME = "vision_aec_source"


def _run_pactl(args: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        p = subprocess.run(["pactl", *args], capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


class SystemAEC:
    """Keeps Vision's audio routed through the desktop's echo canceller."""

    def __init__(self, run: Callable[[list[str]], tuple[int, str]] = _run_pactl,
                 pid: Optional[int] = None,
                 which: Callable[[str], Optional[str]] = shutil.which) -> None:
        self._run = run
        self._pid = str(pid if pid is not None else os.getpid())
        self._which = which
        self._lock = threading.Lock()
        self._module: Optional[str] = None     # index of the module we own
        self._masters: tuple[str, str] = ("", "")
        self.status = "off"     # off | on | no-pactl | no-streams | failed
        self.detail = ""
        self._last_try = 0.0

    # ── pactl helpers ────────────────────────────────────────────────────────
    def _json(self, *args: str) -> list:
        code, out = self._run(["-f", "json", "list", *args])
        if code != 0:
            raise RuntimeError(out.strip() or f"pactl list {' '.join(args)} failed")
        try:
            data = json.loads(out or "[]")
        except ValueError as e:
            raise RuntimeError(f"pactl gave no JSON ({e}); it may be too old") from e
        return data if isinstance(data, list) else []

    def _ours(self, kind: str) -> list[dict]:
        out = []
        for item in self._json(kind):
            props = item.get("properties") or {}
            if str(props.get("application.process.id", "")) == self._pid:
                out.append(item)
        return out

    @staticmethod
    def _names(items: list) -> dict[str, str]:
        return {str(i.get("index")): str(i.get("name", "")) for i in items}

    def _find_module(self) -> Optional[str]:
        """A module left behind by an earlier run that did not close cleanly."""
        for m in self._json("short", "modules"):
            if (m.get("name") == "module-echo-cancel"
                    and f"sink_name={SINK_NAME}" in str(m.get("argument", ""))):
                return str(m.get("index"))
        return None

    # ── the one entry point ──────────────────────────────────────────────────
    def ensure(self) -> str:
        """Route Vision through the canceller if it is not already. Returns the
        status. Safe to call often and from any thread; it is a few `pactl`
        calls, so keep it off the audio thread and the event loop."""
        with self._lock:
            self._last_try = time.monotonic()
            try:
                return self._ensure()
            except Exception as e:  # noqa: BLE001
                self.status, self.detail = "failed", str(e)
                return self.status

    def due(self, every: float = 20.0) -> bool:
        return time.monotonic() - self._last_try >= every

    def _ensure(self) -> str:
        if not self._which("pactl"):
            self.status = "no-pactl"
            self.detail = "pactl is not installed (package pulseaudio-utils)"
            return self.status

        sinks = self._names(self._json("short", "sinks"))
        sources = self._names(self._json("short", "sources"))
        plays = self._ours("sink-inputs")
        records = self._ours("source-outputs")
        if not plays or not records:
            self.status = "no-streams"
            self.detail = ("Vision's speaker or microphone does not go through "
                           "the sound server")
            return self.status

        play_on = sinks.get(str(plays[0].get("sink")), "")
        rec_on = sources.get(str(records[0].get("source")), "")
        if play_on == SINK_NAME and rec_on == SOURCE_NAME and self._module:
            self.status = "on"
            return self.status

        # The real devices under the canceller: whatever Vision is using now,
        # unless it is already on the canceller (then keep the old masters).
        masters = (rec_on if rec_on != SOURCE_NAME else self._masters[0],
                   play_on if play_on != SINK_NAME else self._masters[1])

        if self._module is None:
            self._module = self._find_module()
        if self._module is not None and self._masters != ("", "") \
                and masters != self._masters:
            # Kostas switched speaker or microphone: rebuild on the new ones.
            self._unload()
        if self._module is None:
            args = ["load-module", "module-echo-cancel", "aec_method=webrtc",
                    f"source_name={SOURCE_NAME}", f"sink_name={SINK_NAME}"]
            if masters[0]:
                args.append(f"source_master={masters[0]}")
            if masters[1]:
                args.append(f"sink_master={masters[1]}")
            code, out = self._run(args)
            if code != 0 or not out.strip().isdigit():
                self.status = "failed"
                self.detail = out.strip() or "load-module failed"
                return self.status
            self._module = out.strip()
        self._masters = masters

        for p in plays:
            self._run(["move-sink-input", str(p.get("index")), SINK_NAME])
        for r in records:
            self._run(["move-source-output", str(r.get("index")), SOURCE_NAME])
        self.status, self.detail = "on", ""
        return self.status

    def _unload(self) -> None:
        if self._module is not None:
            self._run(["unload-module", self._module])
        self._module = None
        self._masters = ("", "")

    def stop(self) -> None:
        """Vision is closing: take the canceller away again."""
        with self._lock:
            try:
                self._unload()
            except Exception:  # noqa: BLE001
                pass
            self.status = "off"


def describe(status: str, detail: str) -> str:
    """One line for the activity log."""
    if status == "on":
        return "SYS: Echo cancelling on: you can talk over Vision to stop it."
    return ("SYS: Echo cancelling unavailable (" + (detail or status) + "). "
            "Talking over Vision works with headphones; on speakers use the "
            "interrupt button.")
