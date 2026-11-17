"""
VISION action — open an application, and know whether it actually opened.

WHAT WAS WRONG WITH THIS FILE
    It answered "Opened Spotify." whenever its launcher returned True, and on
    Windows the launcher returned True immediately after

        subprocess.Popen(app_name, shell=True)

    which succeeds because a SHELL started — not because an application did. A
    misspelling, an app that was never installed, an installer that moved:
    every one of them produced "Opened Spotify.", said out loud, while nothing
    appeared on screen.

    The same line was also a command injection. `shutil.which(name.split(".")[0])`
    passed "notepad.exe & whoami" because `which("notepad")` succeeds, and the
    whole string then went to a shell unquoted. Verified before it was changed.

WHAT IT DOES NOW
    * No shell, anywhere. Executables are resolved to a path and started as an
      argument vector; URI schemes go through the OS handler.
    * It watches the process table for a NEW process answering to the name, and
      only says `done` when one appears. When none does, it says `unverified`
      and the assistant tells the user it could not confirm — which is true,
      and is something they can act on.
    * It answers in milliseconds and verifies behind that, so asking to open
      something no longer costs the conversation one to four seconds.

WHY IT IS NOT A LIST OF APPLICATIONS
    Nothing here knows in advance what exists. Verification asks the running
    machine what appeared; matching compares against whatever is running. The
    alias map in core/appidentity.py covers the handful of names that differ
    BETWEEN OPERATING SYSTEMS and restricts nothing — every other name falls
    through untouched, which is what makes software nobody here has heard of
    work exactly as well as the software we have.
"""
import platform
import shutil
import subprocess
import threading
import time

from core import appidentity, outcome
from core.lazyimport import LazyModule, installed

_PYAUTOGUI = installed("pyautogui")
pyautogui = LazyModule("pyautogui")

_SYSTEM = platform.system()          # "Windows" | "Darwin" | "Linux"

# How long to keep watching for the application to appear. Chosen from the two
# ends of the range rather than the middle: a text editor is up in a fraction
# of a second, a game launcher can take ten, and a limit that suits the first
# reports the second as broken.
_LAUNCH_TIMEOUT = 12.0

# Nothing here is a keyword list. The Start-Menu / Spotlight fallback types the
# name and presses Enter, which is what a person does, and works in every
# language the desktop is running in.
_SEARCH_SETTLE = 0.8


# ─────────────────────────────────────────────────────────────────────────────
#  Per-OS launching — no shell on any path
# ─────────────────────────────────────────────────────────────────────────────
def _desktop_search(app_name: str) -> bool:
    """Type the name into the desktop's own search and press Enter.

    The last resort, and the only one that reaches Store apps, shortcuts and
    anything else with no executable on PATH. It is also the one that cannot be
    verified from its own return value — which is fine, because nothing here is
    trusted on its own any more; the process table decides.
    """
    if not _PYAUTOGUI:
        return False
    try:
        pyautogui.press("win")
        time.sleep(_SEARCH_SETTLE)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(_SEARCH_SETTLE)
        pyautogui.press("enter")
        return True
    except Exception as exc:
        print(f"[open_app] desktop search failed: {exc}")
        return False


_LINUX_TERMINALS = [
    "x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal",
    "xterm", "lxterminal", "mate-terminal", "tilix", "alacritty", "kitty",
]


def _on_wayland() -> bool:
    try:
        from core import wayland_desktop
        return wayland_desktop.available()
    except Exception:
        return False


def _place_pointer_on_monitor(monitor_spec: str) -> dict | None:
    """Select `monitor_spec` as the active Wayland monitor and move the pointer
    to its centre so GNOME Mutter opens new windows on that monitor."""
    if not monitor_spec or not _on_wayland():
        return None
    from core import wayland_desktop
    mon = wayland_desktop.use_monitor(monitor_spec)
    wayland_desktop.move_to(mon["width"] // 2, mon["height"] // 2)
    return mon


def _launch_linux(app_name: str) -> bool:
    # "terminal" means whichever one this distribution actually ships.
    if app_name in ("x-terminal-emulator", "gnome-terminal", "terminal"):
        for term in _LINUX_TERMINALS:
            path = shutil.which(term)
            if not path:
                continue
            try:
                subprocess.Popen(
                    [path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return True
            except Exception:
                continue

    binary = (
        shutil.which(app_name)
        or shutil.which(app_name.lower())
        or shutil.which(app_name.lower().replace(" ", "-"))
        or shutil.which(app_name.lower().replace(" ", "_"))
    )
    if binary:
        try:
            subprocess.Popen(
                [binary],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        except Exception:
            pass

    for name in (
        app_name.lower(),
        app_name.lower().replace(" ", "-"),
        app_name.lower().replace(" ", ""),
    ):
        try:
            result = subprocess.run(
                ["gtk-launch", name], capture_output=True, timeout=5
            )
            if result.returncode == 0:
                return True
        except FileNotFoundError:
            break
        except Exception:
            pass

    try:
        result = subprocess.run(
            ["xdg-open", app_name], capture_output=True, timeout=5
        )
        if result.returncode == 0:
            return True
    except Exception:
        pass

    return _desktop_search(app_name)


_LAUNCHERS = {
    "Linux": _launch_linux,
}


# ─────────────────────────────────────────────────────────────────────────────
#  HUD helpers — no-ops without a player, exactly as the whatsapp plugins do
# ─────────────────────────────────────────────────────────────────────────────
def _log(player, message: str) -> None:
    if player:
        try:
            player.write_log(message)
        except Exception:
            pass


def _say(player, instruction: str) -> None:
    if player and hasattr(player, "request_say"):
        try:
            player.request_say(instruction)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  Launch + verify, off the conversation's thread
# ─────────────────────────────────────────────────────────────────────────────
def _launch_and_verify(
    player,
    spoken: str,
    launcher_name: str,
    before: set[int],
    target_mon: dict | None = None,
    tile_spec: str = "",
    quiet: bool = False,
) -> str:
    """The part that takes seconds. Speaks ONLY when something needs saying.

    Returns "" when the application appeared, otherwise what went wrong. With
    `quiet` nothing is said aloud: the caller (a scene opening several apps
    in a row) reports the trouble itself, once, at the end.

    Silence means it worked: the window is on the user's screen and a second
    announcement over the top of it is noise — the same reasoning
    whatsapp_call.py applies to a phone that is already ringing.
    """
    say = (lambda _player, _text: None) if quiet else _say
    try:
        if target_mon is not None and _on_wayland():
            try:
                _place_pointer_on_monitor(str(target_mon["number"]))
            except Exception as exc:
                print(f"[open_app] monitor placement before launch failed: {exc}")

        launcher = _LAUNCHERS[_SYSTEM]
        requested = launcher(launcher_name)
        if not requested and launcher_name.lower() != spoken.lower():
            requested = launcher(spoken)

        appeared = appidentity.wait_for_launch(
            spoken, before, timeout=_LAUNCH_TIMEOUT
        )
        if appeared is not None:
            not_tiled = ""
            if (target_mon is not None or tile_spec) and _on_wayland():
                try:
                    from core import wayland_desktop
                    time.sleep(0.6)
                    focused = appidentity.focus(appeared)
                    if target_mon is not None:
                        wayland_desktop.use_monitor(target_mon["number"])
                        wayland_desktop.move_to(
                            target_mon["width"] // 2, target_mon["height"] // 2
                        )
                    # Tiling is a global shortcut on whatever window is in
                    # front, so it is only sent once the new app is in front.
                    if tile_spec and focused:
                        wayland_desktop.tiling_shell_action("tile", tile_spec)
                    elif tile_spec:
                        not_tiled = (f"{appeared.label} opened, but its window "
                                     "could not be brought to the front, so it "
                                     "was not tiled")
                        say(player, f"[SYSTEM] {not_tiled}. Tell the user briefly.")
                except Exception as exc:
                    print(f"[open_app] post-launch monitor/tile step failed: {exc}")
            mon_note = f" on monitor {target_mon['number']}" if target_mon else ""
            _log(player, f"VISION: {appeared.label} is open{mon_note}.")
            return not_tiled

        if not requested:
            trouble = (f"{spoken} could not be started - nothing on this system "
                       f"answers to that name, so it may not be installed")
        else:
            trouble = (f"{spoken} was asked to start but no new application "
                       f"appeared within {int(_LAUNCH_TIMEOUT)} seconds, so it "
                       f"is NOT confirmed open - it may be slow to load, or not "
                       f"installed under that name")

        _log(player, f"VISION: {trouble}")
        say(player, "Tell the user, in one short sentence in their own "
                    f"language: {trouble}")
        return trouble
    except Exception as exc:
        print(f"[open_app] {type(exc).__name__}: {exc}")
        say(player, "Tell the user, in one short sentence in their own "
                    f"language: opening {spoken} failed - {exc}")
        return f"opening {spoken} failed - {exc}"


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────
def open_app(parameters=None, response=None, player=None,
             session_memory=None) -> str:
    return _open(parameters, player, wait=False)


def open_app_and_wait(parameters=None, player=None) -> str:
    """open_app, but it returns only once the application is confirmed open
    (or has failed), and it says nothing aloud. For callers that open several
    applications one after another: two launches at once would fight over the
    pointer and the front window, which is how each one is placed."""
    return _open(parameters, player, wait=True)


def _open(parameters, player, wait: bool) -> str:
    params = parameters or {}
    spoken = (params.get("app_name") or "").strip()
    monitor_spec = str(params.get("monitor") or params.get("screen") or "").strip()
    tile_spec = str(params.get("tile") or params.get("direction") or "").strip()

    if not spoken:
        return outcome.failed("open_app", "no application name was given")

    if _SYSTEM not in _LAUNCHERS:
        return outcome.failed(
            "open_app", f"this operating system ({_SYSTEM}) is not supported",
            app=spoken,
        )

    target_mon: dict | None = None
    prev_mon: dict | None = None
    if monitor_spec and _on_wayland():
        try:
            from core import wayland_desktop
            prev_mon = wayland_desktop.working_monitor()
            target_mon = _place_pointer_on_monitor(monitor_spec)
        except Exception as exc:
            return outcome.failed(
                "open_app", str(exc), app=spoken, monitor=monitor_spec
            )

    # Already open? Then there is nothing to launch and nothing to wait for,
    # and saying so is more useful than a second copy. This is also the one
    # branch that can answer `done` immediately, because the evidence is
    # already in hand rather than somewhere in the next ten seconds.
    #
    # include_self, unlike the close path, and for the opposite reason. There
    # the assistant's own processes must never be a TARGET; here the question
    # is only "is this already running", and it is already running whether the
    # process belongs to us or not. Without this, asking to open the very
    # application the assistant lives inside reports `started` and goes off to
    # launch a second copy of it — seen exactly that way, against a packaged
    # build whose siblings the close guard had just learnt to protect.
    existing = appidentity.running(spoken, include_self=True)
    if existing:
        best = existing[0]
        for candidate in existing[:8]:
            if appidentity.focus(candidate):
                placement_error = ""
                if _on_wayland() and (target_mon is not None or tile_spec):
                    try:
                        from core import wayland_desktop
                        time.sleep(0.25)
                        if target_mon is not None:
                            wayland_desktop.move_window_to_monitor(
                                target_mon["number"],
                                from_monitor=prev_mon["number"] if prev_mon else None,
                            )
                        if tile_spec:
                            wayland_desktop.tiling_shell_action("tile", tile_spec)
                    except Exception as exc:
                        print(f"[open_app] move/tile focused window failed: {exc}")
                        placement_error = str(exc)
                mon_num = target_mon["number"] if target_mon else (monitor_spec or None)
                _log(player, f"VISION: {candidate.label} brought to the front.")
                if placement_error:
                    return outcome.unverified(
                        "open_app",
                        f"it was brought to the front, but moving or tiling it "
                        f"failed: {placement_error}",
                        app=spoken, matched=candidate.label,
                        already_running=True, focused=True,
                    )
                return outcome.done(
                    "open_app", app=spoken, matched=candidate.label,
                    monitor=mon_num, tile=tile_spec or None,
                    already_running=True, focused=True, verified=True,
                )
        print(f"[open_app] {best.label} is running but no window could be "
              f"raised — launching it instead, which is what brings back an "
              f"app that is only in the tray.")

    launcher_name = appidentity.launcher_name(spoken)
    before = appidentity.snapshot()
    mon_label = f" on monitor {target_mon['number']}" if target_mon else (
        f" on monitor {monitor_spec}" if monitor_spec else ""
    )
    _log(player, f"VISION: Opening {spoken}{mon_label}…")

    if wait:
        trouble = _launch_and_verify(player, spoken, launcher_name, before,
                                     target_mon, tile_spec, quiet=True)
        if trouble and "opened, but" in trouble:
            return outcome.unverified("open_app", trouble, app=spoken)
        if trouble:
            return outcome.failed("open_app", trouble, app=spoken)
        return outcome.done(
            "open_app", app=spoken,
            monitor=target_mon["number"] if target_mon else (monitor_spec or None),
            tile=tile_spec or None, verified=True,
        )

    threading.Thread(
        target=_launch_and_verify,
        args=(player, spoken, launcher_name, before, target_mon, tile_spec),
        daemon=True, name=f"open-app-{spoken[:20]}",
    ).start()

    # Facts, not a sentence. Handing the model a finished English sentence is
    # an invitation to repeat it verbatim — whatsapp_call.py has the receipt:
    # a request made in Turkish came back in English, padded with an offer
    # nobody had made. The model writes the sentence, in the user's language.
    return outcome.started(
        "open_app",
        app=spoken,
        monitor=target_mon["number"] if target_mon else (monitor_spec or None),
        tile=tile_spec or None,
    )


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "open_app",
    "description": (
        "Opens an application on this computer, optionally on a specific "
        "monitor (1..4, left, right, top, bottom, center, main) and/or tiled "
        "with Tiling Shell. Use whenever the user asks to open, launch or "
        "start any app or program ('open Spotify', 'open Spotify to monitor 1', "
        "'open terminal on the left monitor'). Put the application's name in "
        "'app_name' and, if the user named a monitor or screen, put it in "
        "'monitor'. "
        "This tool answers immediately and confirms the launch in the "
        "background: on status=started say only that it is opening, never that "
        "it is open. If it does not appear, you will be told separately - "
        "relay that plainly rather than claiming success. Never say you opened "
        "something without calling this tool. "
        "To CLOSE or KILL an application use computer_control with "
        "action='close_app' or action='kill_app', not this tool. "
        "DO NOT use this tool to reach a WEB PAGE. 'open Chrome and go to "
        "YouTube', 'open YouTube', 'search the web' are all browser_control "
        "alone - it opens the browser itself. Calling open_app for the browser "
        "first and browser_control after it launches the browser TWICE, the "
        "second copy on a different profile, which is what puts a 'Who's using "
        "Chrome?' picker on the screen. Use this tool only when the user wants "
        "the application and nothing in it."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "app_name": {
                "type": "STRING",
                "description": (
                    "The application's REAL name, not the user's shorthand. "
                    "Expand abbreviations and nicknames before passing them: "
                    "'wp' or 'whats' is 'WhatsApp', 'ig' is 'Instagram', 'vs "
                    "code' is 'Visual Studio Code'. A two-letter fragment "
                    "matches the wrong program - 'wp' finds WPS Office. Any "
                    "application on the machine works; there is no fixed list."
                ),
            },
            "monitor": {
                "type": "STRING",
                "description": (
                    "Optional monitor to open the application on: a monitor "
                    "number (1, 2, 3, 4) or position (left, right, top, "
                    "bottom, center, main)."
                ),
            },
            "tile": {
                "type": "STRING",
                "description": (
                    "Optional Tiling Shell position on that monitor after "
                    "opening: left | right | up | down | center | all."
                ),
            },
        },
        "required": ["app_name"],
    },
    "handler": open_app,
}
