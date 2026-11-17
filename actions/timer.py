"""actions/timer.py - countdown timers and pomodoro focus sessions by voice.

"Timer for ten minutes", "set a tea timer for 4 minutes", "how long is left?",
"cancel the tea timer", "start a 25 minute focus session", "stop the session".

The counting is done by core/timers.py. When a timer runs out this module
shows a desktop notification, plays a short sound and asks the assistant to
say so. The notification and the sound do not need the conversation, so a
timer still rings while Vision is asleep waiting for its wake word.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from core import outcome, timers

_player = None      # the window, remembered from the last call, for speech

_SOUNDS = (
    "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
    "/usr/share/sounds/freedesktop/stereo/complete.oga",
    "/usr/share/sounds/freedesktop/stereo/bell.oga",
)


def _run_quiet(cmd: list[str]) -> None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            print(f"[Timer] {cmd[0]} failed: {(r.stderr or '').strip()[:200]}")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[Timer] {cmd[0]} failed: {e}")


def _notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        _run_quiet(["notify-send", "--urgency=critical", "--app-name=Vision", title, body])


def _chime() -> None:
    sound = next((s for s in _SOUNDS if Path(s).exists()), None)
    if not sound:
        return
    for player_cmd in ("pw-play", "paplay", "canberra-gtk-play"):
        if shutil.which(player_cmd):
            cmd = [player_cmd, "-f", sound] if player_cmd == "canberra-gtk-play" else [player_cmd, sound]
            _run_quiet(cmd)
            return


def _headline(t: dict) -> str:
    if t["kind"] == "work":
        return "Focus block finished"
    if t["kind"] == "break":
        return "Break is over"
    return f"{t['label'].capitalize()} timer finished" if t["label"] else "Timer finished"


def _alert(t: dict, late: bool) -> None:
    """Called by the engine's thread when a timer runs out."""
    headline = _headline(t)
    detail = t.get("note") or f"{timers.fmt_duration(t['seconds'])} are up."
    if late:
        detail = "It ran out while Vision was closed."
    print(f"[Timer] {headline} - {detail}")
    _notify(headline, detail[:1].upper() + detail[1:])
    if not late:
        _chime()
    player = _player
    if player is None:
        return
    try:
        player.write_log(f"VISION: {headline}.")
    except Exception:
        pass
    if hasattr(player, "request_say"):
        what = (f"the user's {timers.fmt_duration(t['seconds'])} "
                f"{(t['label'] + ' ') if t['label'] and t['kind'] == 'timer' else ''}timer has just finished"
                if t["kind"] == "timer" else detail)
        if late:
            what = f"{headline.lower()}, but it ran out while Vision was closed"
        try:
            player.request_say("[SYSTEM] Tell the user now, in one short sentence in "
                               f"their own language: {what}.")
        except Exception:
            pass


def _engine() -> timers.TimerEngine:
    return timers.engine(on_fire=_alert)


def _seconds(p: dict) -> float:
    total = 0.0
    for key, mult in (("hours", 3600), ("minutes", 60), ("seconds", 1)):
        value = p.get(key)
        if value in (None, ""):
            continue
        total += float(value) * mult
    return total


def _describe(t: dict) -> dict:
    d = {"id": t["id"], "kind": t["kind"], "left": timers.fmt_duration(t["remaining"])}
    if t["label"]:
        d["label"] = t["label"]
    if t["waiting"]:
        d["starts_in"] = timers.fmt_duration(t["remaining"] - t["seconds"])
        d["left"] = timers.fmt_duration(t["seconds"])
    return d


def timer(parameters: dict, player=None, session_memory=None) -> str:
    global _player
    if player is not None:
        _player = player
    p = parameters or {}
    action = str(p.get("action") or "start").strip().lower().replace("-", "_")
    try:
        eng = _engine()

        if action in ("start", "set", "add"):
            try:
                seconds = _seconds(p)
            except (TypeError, ValueError):
                return outcome.failed("timer", "the duration was not a number")
            if seconds <= 0:
                return outcome.ask("timer", "How long should the timer run?")
            t = eng.add(seconds, label=str(p.get("label") or ""))
            return outcome.done("timer", started=timers.fmt_duration(seconds),
                                label=t["label"] or None, id=t["id"], verified=True)

        if action in ("pomodoro", "focus", "start_pomodoro"):
            made = eng.add_pomodoro(work_min=p.get("work_minutes") or 25,
                                    break_min=5 if p.get("break_minutes") in (None, "") else p.get("break_minutes"),
                                    rounds=p.get("rounds") or 4,
                                    label=str(p.get("label") or ""))
            work = next(t for t in made if t["kind"] == "work")
            rounds = sum(1 for t in made if t["kind"] == "work")
            brk = next((t for t in made if t["kind"] == "break"), None)
            return outcome.done("timer", focus_session="started", rounds=rounds,
                                work=timers.fmt_duration(work["seconds"]),
                                breaks=timers.fmt_duration(brk["seconds"]) if brk else "none",
                                verified=True)

        if action in ("list", "status", "left", "remaining"):
            items = [t for t in eng.list()]
            if not items:
                return outcome.done("timer", running=0, note="no timer is running")
            shown = [_describe(t) for t in items if not t["waiting"]]
            later = sum(1 for t in items if t["waiting"])
            return outcome.done("timer", running=len(shown), timers=shown,
                                later_phases=later or None)

        if action in ("cancel", "stop", "delete", "remove"):
            gone = eng.cancel(str(p.get("which") or p.get("label") or p.get("id") or ""))
            if not gone:
                return outcome.failed("timer", "no running timer matches that")
            if any(t["kind"] != "timer" for t in gone) and all(t["kind"] != "timer" for t in gone):
                return outcome.done("timer", cancelled="the focus session", verified=True)
            return outcome.done("timer", cancelled=len(gone),
                                labels=[t["label"] for t in gone if t["label"]] or None,
                                verified=True)

        return outcome.failed("timer", f"unknown action '{action}'")
    except ValueError as e:
        return outcome.failed("timer", str(e))
    except Exception as e:
        return outcome.failed("timer", f"{type(e).__name__}: {e}")


def _resume_saved() -> None:
    """Timers saved by an earlier run keep counting from the moment Vision
    starts, without waiting for the first timer request."""
    try:
        if timers.default_path().exists() and timers.default_path().stat().st_size > 20:
            _engine()
    except Exception as e:
        print(f"[Timer] saved timers not resumed: {e}")


_resume_saved()


# -- Tool declaration (auto-discovered by core/action_loader.py) ---------------
TOOL = {
    "name": "timer",
    "description": (
        "Countdown timers and pomodoro focus sessions, measured from now. Use "
        "for 'set a timer for 10 minutes', 'tea timer 4 minutes', 'how long is "
        "left on the timer', 'cancel the timer', 'start a pomodoro', 'start a "
        "25 minute focus session', 'stop the focus session'. It rings with a "
        "notification, a sound and a spoken sentence when it runs out; do not "
        "also set a reminder. For something at a CLOCK TIME or on a date "
        "('remind me at 6', 'tomorrow at 9') use reminder instead."
    ),
    "brief": "Countdown timers and pomodoro focus sessions.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "start | pomodoro | list | cancel",
            },
            "hours": {"type": "NUMBER", "description": "start: hours to count down"},
            "minutes": {"type": "NUMBER", "description": "start: minutes to count down"},
            "seconds": {"type": "NUMBER", "description": "start: seconds to count down"},
            "label": {
                "type": "STRING",
                "description": "start: what the timer is for, if the user said ('tea', 'pasta')",
            },
            "which": {
                "type": "STRING",
                "description": (
                    "cancel: the label or id of the timer, 'pomodoro' for the "
                    "focus session, or 'all'. Leave empty when only one is running."
                ),
            },
            "work_minutes": {"type": "NUMBER", "description": "pomodoro: length of a work block, default 25"},
            "break_minutes": {"type": "NUMBER", "description": "pomodoro: length of a break, default 5"},
            "rounds": {"type": "INTEGER", "description": "pomodoro: number of work blocks, default 4"},
        },
        "required": ["action"],
    },
    "handler": timer,
}
