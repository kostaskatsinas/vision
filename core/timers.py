"""core/timers.py - countdown timers and pomodoro sessions.

A timer is "tell me in ten minutes", measured from now. That is different from
a reminder (actions/reminder.py), which rings at a clock time and is handed to
systemd so it survives a reboot. Timers live in this process: one scheduler
thread sleeps until the next one is due.

They are saved to memory/timers.json after every change, so closing and
reopening Vision keeps a running timer. One that ran out while Vision was
closed is reported once at the next start instead of being lost.

A pomodoro session is nothing more than a row of timers sharing a group id:
work, break, work, break... Cancelling the session cancels the group.

Nothing here touches Qt. The window reads `hud_line()` once a second.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from core.paths import base_dir

MAX_SECONDS = 24 * 3600          # a longer wait is a reminder, not a timer
MAX_TIMERS = 20
_LATE_GRACE = 90.0               # finished this long ago or less: still ring


def default_path() -> Path:
    return base_dir() / "memory" / "timers.json"


def fmt_duration(seconds: float) -> str:
    """'25 minutes', '1 hour 5 minutes', '45 seconds' - for the model to say."""
    s = int(round(max(0.0, seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if sec and not h:
        parts.append(f"{sec} second{'s' if sec != 1 else ''}")
    return " ".join(parts) or "0 seconds"


def fmt_clock(seconds: float) -> str:
    """'09:41' or '1:05:00' - for the window."""
    s = int(max(0.0, seconds) + 0.999)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


class TimerEngine:
    """Every method is safe to call from any thread."""

    def __init__(self, path: Optional[Path] = None,
                 on_fire: Optional[Callable[[dict, bool], None]] = None,
                 clock: Callable[[], float] = time.time):
        self._path = Path(path) if path else default_path()
        self._on_fire = on_fire
        self._clock = clock
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self._timers: list[dict] = self._load()

    # -- storage ------------------------------------------------------------
    def _load(self) -> list[dict]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out = []
        for t in data.get("timers", []) if isinstance(data, dict) else []:
            try:
                out.append({"id": str(t["id"]), "label": str(t.get("label") or ""),
                            "end_ts": float(t["end_ts"]),
                            "seconds": float(t.get("seconds") or 0),
                            "kind": str(t.get("kind") or "timer"),
                            "group": str(t.get("group") or ""),
                            "note": str(t.get("note") or "")})
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"timers": self._timers}, indent=1), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as e:
            print(f"[Timers] could not save: {e}")

    # -- the scheduler ------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._loop, daemon=True, name="timers")
            self._thread.start()

    def shutdown(self) -> None:
        self._stop = True
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop:
            wait = self.tick()
            self._wake.wait(timeout=wait)
            self._wake.clear()

    def tick(self) -> float:
        """Ring everything that is due. Returns the seconds until the next one
        (capped, so a changed system clock is noticed)."""
        now = self._clock()
        with self._lock:
            due = [t for t in self._timers if t["end_ts"] <= now]
            if due:
                self._timers = [t for t in self._timers if t["end_ts"] > now]
                self._save()
            nxt = min((t["end_ts"] for t in self._timers), default=None)
        for t in sorted(due, key=lambda t: t["end_ts"]):
            late = (now - t["end_ts"]) > _LATE_GRACE
            if self._on_fire:
                try:
                    self._on_fire(dict(t), late)
                except Exception as e:      # one bad alert must not stop the rest
                    print(f"[Timers] alert failed: {e}")
        if nxt is None:
            return 3600.0
        return max(0.05, min(30.0, nxt - now))

    # -- what the tool calls -------------------------------------------------
    def add(self, seconds: float, label: str = "", kind: str = "timer",
            group: str = "", note: str = "", start_in: float = 0.0) -> dict:
        seconds = float(seconds)
        if not (seconds >= 1):
            raise ValueError("a timer needs at least one second")
        if seconds + start_in > MAX_SECONDS:
            raise ValueError("a timer can run for 24 hours at most; use a reminder for longer")
        with self._lock:
            if len(self._timers) >= MAX_TIMERS:
                raise ValueError(f"there are already {MAX_TIMERS} timers running")
            t = {"id": uuid.uuid4().hex[:8], "label": " ".join(str(label or "").split())[:60],
                 "end_ts": self._clock() + start_in + seconds, "seconds": seconds,
                 "kind": kind, "group": group, "note": note}
            self._timers.append(t)
            self._save()
        self._wake.set()
        return dict(t)

    def add_pomodoro(self, work_min: float = 25, break_min: float = 5,
                     rounds: int = 4, label: str = "") -> list[dict]:
        work_s, break_s, rounds = float(work_min) * 60, float(break_min) * 60, int(rounds)
        if not (1 <= rounds <= 12):
            raise ValueError("a session has between 1 and 12 rounds")
        if work_s < 60 or break_s < 0:
            raise ValueError("a work block needs at least one minute")
        total = rounds * work_s + (rounds - 1) * break_s
        if total > MAX_SECONDS:
            raise ValueError("that session would run for more than 24 hours")
        with self._lock:
            if any(t["kind"] != "timer" for t in self._timers):
                raise ValueError("a focus session is already running; stop it first")
            phases = rounds if break_s <= 0 else 2 * rounds - 1
            if len(self._timers) + phases > MAX_TIMERS + 24:
                raise ValueError("too many timers are running")
            group = "pomo-" + uuid.uuid4().hex[:6]
            made, at = [], 0.0
            for i in range(1, rounds + 1):
                last = i == rounds
                note = ("the last work block is over; the focus session is finished" if last
                        else f"work block {i} of {rounds} is over; "
                             + (f"take a {fmt_duration(break_s)} break" if break_s > 0
                                else "the next one starts now"))
                made.append(self._raw_add(work_s, label or "focus", "work", group, note, at))
                at += work_s
                if not last and break_s > 0:
                    made.append(self._raw_add(
                        break_s, "break", "break", group,
                        f"the break is over; work block {i + 1} of {rounds} starts now", at))
                    at += break_s
            self._save()
        self._wake.set()
        return made

    def _raw_add(self, seconds, label, kind, group, note, start_in) -> dict:
        t = {"id": uuid.uuid4().hex[:8], "label": label, "end_ts": self._clock() + start_in + seconds,
             "seconds": seconds, "kind": kind, "group": group, "note": note}
        self._timers.append(t)
        return dict(t)

    def list(self) -> list[dict]:
        """Running timers, soonest first, each with `remaining` seconds. A
        pomodoro phase that has not begun yet has `waiting` True."""
        now = self._clock()
        with self._lock:
            out = []
            for t in sorted(self._timers, key=lambda t: t["end_ts"]):
                d = dict(t)
                d["remaining"] = max(0.0, t["end_ts"] - now)
                d["waiting"] = d["remaining"] > t["seconds"] + 0.5
                out.append(d)
            return out

    def cancel(self, which: str = "") -> list[dict]:
        """Cancel by id, by label, 'all', 'pomodoro', or (empty) the only timer
        running. Returns what was cancelled; raises ValueError when the choice
        is not clear, so nothing is cancelled by a guess."""
        key = " ".join(str(which or "").lower().split())
        with self._lock:
            if not self._timers:
                return []
            if key in ("all", "everything", "every timer", "all timers"):
                gone = list(self._timers)
            elif key in ("pomodoro", "focus", "focus session", "session"):
                gone = [t for t in self._timers if t["kind"] != "timer"]
            else:
                plain = [t for t in self._timers if t["kind"] == "timer"]
                gone = [t for t in self._timers if t["id"] == key]
                if not gone and key:
                    gone = [t for t in plain if t["label"].lower() == key] or \
                           [t for t in plain if key in t["label"].lower()]
                    if len(gone) > 1:
                        raise ValueError("several timers match that name: "
                                         + ", ".join(t["label"] for t in gone))
                if not gone and not key:
                    groups = {t["group"] for t in self._timers if t["group"]}
                    if len(plain) == 1 and not groups:
                        gone = plain
                    elif not plain and len(groups) == 1:
                        gone = list(self._timers)
                    else:
                        raise ValueError("more than one timer is running; say which one")
                # one phase of a session takes the whole session with it
                groups = {t["group"] for t in gone if t["group"]}
                if groups:
                    gone = [t for t in self._timers if t in gone or t["group"] in groups]
            ids = {t["id"] for t in gone}
            self._timers = [t for t in self._timers if t["id"] not in ids]
            if gone:
                self._save()
        self._wake.set()
        return [dict(t) for t in gone]

    def hud_line(self) -> str:
        """The soonest running timer as 'TEA 09:41' (plus '+2' for others), or
        '' when none is running."""
        items = [t for t in self.list() if not t["waiting"]]
        if not items:
            return ""
        first = items[0]
        name = {"work": "FOCUS", "break": "BREAK"}.get(first["kind"]) or (first["label"].upper()[:14] or "TIMER")
        more = len(items) - 1
        return f"{name} {fmt_clock(first['remaining'])}" + (f"  +{more}" if more else "")


# -- one engine for the app ----------------------------------------------------
_engine: Optional[TimerEngine] = None
_engine_lock = threading.Lock()


def engine(on_fire: Optional[Callable[[dict, bool], None]] = None) -> TimerEngine:
    """The app's engine, created and started on first use."""
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = TimerEngine(on_fire=on_fire)
            _engine.start()
        elif on_fire is not None and _engine._on_fire is None:
            _engine._on_fire = on_fire
        return _engine


def hud_line() -> str:
    """For the window: never raises, and never creates the engine by itself."""
    try:
        return _engine.hud_line() if _engine is not None else ""
    except Exception:
        return ""
