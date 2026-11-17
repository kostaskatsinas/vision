"""
actions/terminal.py - open the built-in terminals by voice.

"Open a Claude terminal", "open a Gemini terminal", "open a terminal": a real
terminal appears inside the Vision window (gui/terminal.py) and the user types
in it. Two can be open at once.

This tool only opens, shows and hides them. It cannot type into a terminal,
read one or close one, so nothing said to Vision can run a command there or
stop a session the user is in the middle of. To hand a job to Claude Code or
Gemini CLI in the background, the tool is cli_helper, with its own limits.

Tests: python -m actions._terminal_tests
"""
from __future__ import annotations

import time
from pathlib import Path

from core import outcome
from core.terminal_session import PROGRAMS, resolve_folder

MAX_TERMINALS = 2

_ALIASES = {
    "claude": "claude", "claude code": "claude", "claude_code": "claude", "claudecode": "claude",
    "gemini": "gemini", "gemini cli": "gemini", "gemini_cli": "gemini",
    "shell": "shell", "bash": "shell", "terminal": "shell", "plain": "shell", "": "shell",
}


def _program(value) -> str | None:
    return _ALIASES.get(str(value or "").strip().lower().replace("-", " "))


def _wait(check, seconds: float = 2.0) -> bool:
    """The window acts on its own thread; give it a moment, then look."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.03)
    return bool(check())


def terminal(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "open").strip().lower()
    if player is None or not hasattr(player, "open_terminal"):
        return outcome.failed("terminal", "the Vision window is not available")
    try:
        if action in ("show", "hide", "minimise", "minimize"):
            show = action == "show"
            player.show_terminals(show)
            if not _wait(lambda: player.terminals_shown() == show):
                return outcome.unverified("terminal", "the window did not confirm it")
            if show:
                return outcome.done("terminal", shown=True, open=player.terminal_count())
            return outcome.done("terminal", hidden=True,
                                note="the terminals keep running in the background")
        if action != "open":
            return outcome.failed("terminal", "action must be open, show or hide")

        program = _program(p.get("program"))
        if program is None:
            return outcome.failed("terminal", "program must be claude, gemini or shell")
        before = player.terminal_count()
        if before >= MAX_TERMINALS:
            player.show_terminals(True)
            return outcome.failed(
                "terminal", "both terminals are already in use; the user has to close one first")
        asked = str(p.get("folder") or "").strip()
        folder = resolve_folder(asked)
        missing = bool(asked) and folder == resolve_folder(None) and not _is_home(asked)
        player.open_terminal(program, str(folder))
        facts = dict(opened=PROGRAMS[program]["name"], folder=str(folder),
                     folder_not_found=asked if missing else None,
                     note="the user types in it themselves; you cannot type into it or read it")
        if not _wait(lambda: player.terminal_count() > before):
            return outcome.unverified("terminal", "the window did not confirm it", **facts)
        return outcome.done("terminal", **facts)
    except Exception as e:
        return outcome.failed("terminal", f"{type(e).__name__}: {e}")


def _is_home(asked: str) -> bool:
    try:
        return Path(asked).expanduser().resolve() == Path.home().resolve()
    except (OSError, RuntimeError, ValueError):
        return False


# -- Tool declaration (auto-discovered by core/action_loader.py) ---------------
TOOL = {
    "name": "terminal",
    "description": (
        "Open a real terminal INSIDE the Vision window for the user to type "
        "in. Use for 'open a Claude terminal', 'open Claude Code', 'open a "
        "Gemini terminal', 'open a terminal here', 'show the terminals', "
        "'hide the terminals'. Two can be open at once, side by side. You "
        "cannot type into these terminals, read them or close them: the user "
        "does. To give Claude Code or Gemini CLI a job yourself and report "
        "the answer, use cli_helper instead. To open the desktop's own "
        "terminal app in its own window, use open_app."
    ),
    "brief": "Open a built-in terminal with Claude Code, Gemini CLI or a shell.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "open (default) | show | hide",
            },
            "program": {
                "type": "STRING",
                "description": "open: claude | gemini | shell (default shell)",
            },
            "folder": {
                "type": "STRING",
                "description": (
                    "open: folder the terminal starts in, if the user named "
                    "one ('~/vision'). Default is the home folder."
                ),
            },
        },
        "required": [],
    },
    "handler": terminal,
}
