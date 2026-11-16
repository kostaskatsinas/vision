#computer_settings.py
import json
import re
import shutil
import sys
import time
import subprocess
import platform
from pathlib import Path

from core.lazyimport import LazyModule, installed

# Imported on first USE, not at launch. This file has eighty-six `pyautogui.`
# call sites; not one of them changes — see core/lazyimport.py.
_PYAUTOGUI = installed("pyautogui")
_PYPERCLIP = installed("pyperclip")


def _configure_pyautogui(module) -> None:
    module.FAILSAFE = True
    module.PAUSE    = 0.05


pyautogui = LazyModule("pyautogui", _configure_pyautogui)
pyperclip = LazyModule("pyperclip")

from core import confirm, outcome
from core.undo import push_undo

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

def _get_api_key() -> str:
    path = _get_base_dir() / "config" / "api_keys.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]

# ── Volume ───────────────────────────────────────────────────────────────────
# "Turn it up by 30", "set it to 100", "a bit quieter": the level is read and
# written as a NUMBER wherever the OS allows it, so the change is the one asked
# for. The media keys are only the fallback. The old code pressed the volume key
# five times whatever was asked — two points a press on Windows — which is how
# "volume up by 50" and, via volume_up, "volume to 100" both became +10.

def _number(value) -> int | None:
    """The whole number in a value: 40, 40.0, "40", "40%", "%40" -> 40.
    None when there is no number in it."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    return int(round(float(m.group(0).replace(",", ".")))) if m else None


def _volume_step(delta: int) -> None:
    now = volume_get()
    if now is not None:
        volume_set(now + delta)
        return
    sign = "+" if delta > 0 else "-"
    subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{sign}{abs(delta)}%"],
        capture_output=True)


def volume_up(step: int = 10):
    _volume_step(abs(int(step)))


def volume_down(step: int = 10):
    _volume_step(-abs(int(step)))


def mute_get() -> bool | None:
    """Whether the sound is muted, or None where this system will not say."""
    try:
        r = subprocess.run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
        low = r.stdout.lower()
        return True if "yes" in low else False if "no" in low else None
    except Exception:
        return None


def mute_set(muted: bool) -> None:
    """Muted, or not — as asked, never a toggle.

    'unmute' used to press the mute KEY, which is a toggle: on a machine that was
    not muted, asking to unmute muted it. The same was true of macOS, where both
    words ran "set volume with output muted"."""
    muted = bool(muted)
    subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0"],
                   capture_output=True)


def volume_mute():
    mute_set(True)


def volume_unmute():
    mute_set(False)


def volume_toggle_mute():
    state = mute_get()
    if state is not None:
        mute_set(not state)
    else:
        subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"],
                       capture_output=True)


def volume_get() -> int | None:
    """Current master volume 0-100, or None if this platform will not say.

    Undo needs a "before" value, and reading one is cheap on every OS we
    support. Where it is not readable the action simply is not registered as
    undoable — a wrong undo is worse than no undo."""
    try:
        r = subprocess.run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
        m = re.search(r"(\d+)%", r.stdout)
        return max(0, min(100, int(m.group(1)))) if m else None
    except Exception:
        return None


def brightness_get() -> int | None:
    """Current brightness 0-100, or None where it cannot be read."""
    try:
        if shutil.which("brightnessctl"):
            cur = int(subprocess.run(["brightnessctl", "get"],
                                     capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5).stdout.strip())
            mx  = int(subprocess.run(["brightnessctl", "max"],
                                     capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5).stdout.strip())
            return max(0, min(100, round(cur * 100 / mx))) if mx else None
        if shutil.which("xrandr"):
            out = subprocess.run(["xrandr", "--verbose"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=5).stdout
            m = re.search(r"Brightness:\s*([\d.]+)", out)
            return max(0, min(100, round(float(m.group(1)) * 100))) if m else None
    except Exception:
        pass
    return None


def brightness_set(value: int) -> None:
    """Set brightness to an absolute percentage — "brightness to 70" — and the
    way undo puts back a value captured before a change."""
    value = max(0, min(100, int(value)))
    if shutil.which("brightnessctl"):
        subprocess.run(["brightnessctl", "set", f"{value}%"], capture_output=True)
    elif shutil.which("xrandr"):
        _xrandr_brightness(target=value / 100)


def volume_set(value: int):
    value = max(0, min(100, int(value)))
    subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{value}%"],
        capture_output=True)
    if value > 0:
        subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"],
            capture_output=True)
    return

def _xrandr_brightness(target: float | None = None, delta: float = 0.0) -> None:
    """Linux without brightnessctl: xrandr's brightness on the first connected
    output, 0.1-1.0.

    Done in Python rather than the shell one-liner it replaces, which could never
    have worked: the Python it ran was inside double quotes and contained
    unescaped double quotes, so the shell stripped them and Python saw
    `[xrandr,--verbose]` — a NameError, every time."""
    out = subprocess.run(["xrandr", "--verbose"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=5).stdout
    name = next((line.split()[0] for line in out.splitlines() if " connected" in line), None)
    if not name:
        return
    m = re.search(r"Brightness:\s*([\d.]+)", out)
    current = float(m.group(1)) if m else 1.0
    new = target if target is not None else current + delta
    subprocess.run(["xrandr", "--output", name, "--brightness", f"{max(0.1, min(1.0, new)):.2f}"],
                   capture_output=True, timeout=5)


def _brightness_step(delta: int) -> None:
    now = brightness_get()
    if now is not None:
        brightness_set(now + delta)
        return
    if shutil.which("xrandr"):
        _xrandr_brightness(delta=delta / 100)
    # Windows with no readable brightness is an external monitor, which WMI
    # cannot drive; the caller reports that it could not be read back.


def brightness_up(step: int = 10):
    _brightness_step(abs(int(step)))


def brightness_down(step: int = 10):
    _brightness_step(-abs(int(step)))

def close_app():
    """Removed. Closing an application now lives in computer_control.

    WHAT THIS USED TO BE, AND WHY IT HAD TO GO

        if _OS == "Darwin": pyautogui.hotkey("command", "q")
        else:               pyautogui.hotkey("alt", "f4")

    Two lines with no target. It sent the quit chord to WHATEVER WINDOW HAD
    FOCUS — and while somebody is talking to the assistant, the window with
    focus is very often the assistant. That is the whole of the bug people
    reported as "I asked it to close an app and it closed itself".

    It could not be fixed by aiming better. "The focused window" is the wrong
    idea: the user names an application, so the application is what has to be
    found. `computer_control` action='close_app' does that — matched against
    what is actually running, never the assistant's own process tree, asked
    politely so unsaved work survives, and checked afterwards.

    The function stays as a named refusal rather than disappearing, because a
    model that learnt the old name from an older prompt will still reach for
    it, and a KeyError here would surface as "computer_settings failed" with
    nothing to act on.
    """
    return outcome.failed(
        "close_app",
        "this action needs to know WHICH application to close - use the "
        "computer_control tool with action='close_app' and the application's "
        "name in 'app_name'",
    )

def close_window():
    pyautogui.hotkey("ctrl", "w")

def full_screen():
    pyautogui.press("f11")

def minimize_window():
    pyautogui.hotkey("win", "down")

def maximize_window():
    try:
        subprocess.run(["wmctrl", "-r", ":ACTIVE:", "-b", "add,maximized_vert,maximized_horz"],
            capture_output=True)
    except Exception:
        pyautogui.hotkey("super", "up")

def snap_left():
    try:
        subprocess.run(["wmctrl", "-r", ":ACTIVE:", "-e", "0,0,0,960,1080"],
            capture_output=True)
    except Exception:
        pass

def snap_right():
    try:
        subprocess.run(["wmctrl", "-r", ":ACTIVE:", "-e", "0,960,0,960,1080"],
            capture_output=True)
    except Exception:
        pass

def switch_window():
    pyautogui.hotkey("alt", "tab")

def show_desktop():
    pyautogui.hotkey("super", "d")

def open_task_manager():
    for cmd in [["gnome-system-monitor"], ["xfce4-taskmanager"], ["htop"]]:
        if subprocess.run(["which", cmd[0]], capture_output=True).returncode == 0:
            subprocess.Popen(cmd)
            break


def focus_search():
    pyautogui.hotkey("ctrl", "l")

def pause_video():      pyautogui.press("space")


# ── Media ────────────────────────────────────────────────────────────────────
# The system media keys reach whatever is playing — music, a browser tab, a
# video app — without its window having focus. On Linux, playerctl talks to the
# players directly (MPRIS), where an X key name may not exist.

def _media(key: str, playerctl_cmd: str) -> None:
    if shutil.which("playerctl"):
        subprocess.run(["playerctl", playerctl_cmd], capture_output=True, timeout=5)
        return
    pyautogui.press(key)


def media_play_pause(): _media("playpause", "play-pause")
def media_next():       _media("nexttrack", "next")
def media_previous():   _media("prevtrack", "previous")
def media_stop():       _media("stop", "stop")


# Seconds one arrow key moves YouTube and most web players. Players differ and
# none reports where it is, so a seek in another application says it is an
# estimate. A video on the assistant's own display is exact: video_player.
SEEK_PER_PRESS = 5


def seek_presses(seconds: int) -> int:
    """Arrow keys to the window in front, about `seconds` worth. Returns presses."""
    presses = max(1, min(120, round(abs(seconds) / SEEK_PER_PRESS)))
    key = "right" if seconds > 0 else "left"
    for _ in range(presses):
        pyautogui.press(key)
    return presses


def _playerctl_position() -> float | None:
    try:
        r = subprocess.run(["playerctl", "position"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=5)
        return float(r.stdout.strip()) if r.returncode == 0 else None
    except Exception:
        return None

def refresh_page():
    pyautogui.press("f5")

def close_tab():
    pyautogui.hotkey("ctrl", "w")

def new_tab():
    pyautogui.hotkey("ctrl", "t")

def next_tab():
    pyautogui.hotkey("ctrl", "tab")

def prev_tab():
    pyautogui.hotkey("ctrl", "shift", "tab")

def go_back():
    pyautogui.hotkey("alt", "left")

def go_forward():
    pyautogui.hotkey("alt", "right")

def zoom_in():
    pyautogui.hotkey("ctrl", "equal")

def zoom_out():
    pyautogui.hotkey("ctrl", "minus")

def zoom_reset():
    pyautogui.hotkey("ctrl", "0")

def find_on_page():
    pyautogui.hotkey("ctrl", "f")

def reload_page_n(n: int):
    for _ in range(max(1, n)):
        refresh_page()
        time.sleep(0.8)


def _wheel(notches: int) -> None:
    """Scroll by whole wheel notches — the same distance on every OS.

    pyautogui's unit is not: on Windows it is 1/120 of a notch, elsewhere a whole
    notch. The old scroll(500) therefore moved about four notches on Windows and
    five hundred on Linux."""
    pyautogui.scroll(notches)


def scroll_up(notches: int = 5):    _wheel(abs(int(notches)))
def scroll_down(notches: int = 5):  _wheel(-abs(int(notches)))

def scroll_top():
    pyautogui.hotkey("ctrl", "home")

def scroll_bottom():
    pyautogui.hotkey("ctrl", "end")

def page_up():   pyautogui.press("pageup")
def page_down(): pyautogui.press("pagedown")


def copy():
    pyautogui.hotkey("ctrl", "c")

def paste():
    pyautogui.hotkey("ctrl", "v")

def cut():
    pyautogui.hotkey("ctrl", "x")

def undo():
    pyautogui.hotkey("ctrl", "z")

def redo():
    pyautogui.hotkey("ctrl", "y")

def select_all():
    pyautogui.hotkey("ctrl", "a")

def save_file():
    pyautogui.hotkey("ctrl", "s")

def press_enter():   pyautogui.press("enter")
def press_escape():  pyautogui.press("escape")
def press_key(key: str): pyautogui.press(key)

def type_text(text: str, press_enter_after: bool = False):
    if not text:
        return
    if _PYPERCLIP:
        pyperclip.copy(str(text))
        time.sleep(0.15)
        paste()
    else:
        pyautogui.write(str(text), interval=0.03)
    if press_enter_after:
        time.sleep(0.1)
        pyautogui.press("enter")

def take_screenshot():
    for cmd in [["scrot"], ["gnome-screenshot"], ["import", "-window", "root", "screenshot.png"]]:
        if subprocess.run(["which", cmd[0]], capture_output=True).returncode == 0:
            subprocess.Popen(cmd)
            return
    pyautogui.hotkey("ctrl", "print_screen")

def lock_screen():
    for cmd in [
        ["gnome-screensaver-command", "-l"],
        ["xdg-screensaver", "lock"],
        ["loginctl", "lock-session"],
    ]:
        if subprocess.run(["which", cmd[0]], capture_output=True).returncode == 0:
            subprocess.run(cmd, capture_output=True)
            return

def open_system_settings():
    for cmd in [["gnome-control-center"], ["xfce4-settings-manager"], ["kcmshell5"]]:
        if subprocess.run(["which", cmd[0]], capture_output=True).returncode == 0:
            subprocess.Popen(cmd)
            return

def open_file_explorer():
    for cmd in [["nautilus"], ["thunar"], ["dolphin"], ["nemo"]]:
        if subprocess.run(["which", cmd[0]], capture_output=True).returncode == 0:
            subprocess.Popen(cmd)
            return
    subprocess.Popen(["xdg-open", str(Path.home())])

def sleep_display():
    subprocess.run(["xset", "dpms", "force", "off"], capture_output=True)

def open_run():
    pass

def dark_mode():
    try:
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        current = result.stdout.strip()
        new_scheme = "'default'" if "dark" in current else "'prefer-dark'"
        subprocess.run(
            ["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", new_scheme],
            capture_output=True
        )
    except Exception as e:
        print(f"[Settings] dark_mode Linux failed: {e}")

def toggle_wifi():
    try:
        result = subprocess.run(["nmcli", "radio", "wifi"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        state  = "off" if "enabled" in result.stdout else "on"
        subprocess.run(["nmcli", "radio", "wifi", state], capture_output=True)
    except Exception as e:
        print(f"[Settings] toggle_wifi Linux failed: {e}")

def restart_computer():
    subprocess.run(["systemctl", "reboot"], capture_output=True)

def shutdown_computer():
    subprocess.run(["systemctl", "poweroff"], capture_output=True)

ACTION_MAP: dict[str, callable] = {
    "volume_up":           volume_up,
    "volume_down":         volume_down,
    "mute":                volume_mute,
    "unmute":              volume_unmute,
    "toggle_mute":         volume_toggle_mute,
    "media_play_pause":    media_play_pause,
    "media_next":          media_next,
    "next_track":          media_next,
    "media_previous":      media_previous,
    "previous_track":      media_previous,
    "media_stop":          media_stop,
    "brightness_up":       brightness_up,
    "brightness_down":     brightness_down,
    "sleep_display":       sleep_display,
    "screen_off":          sleep_display,
    "pause_video":         pause_video,
    "play_pause":          pause_video,
    "close_app":           close_app,
    "close_window":        close_window,
    "full_screen":         full_screen,
    "fullscreen":          full_screen,
    "minimize":            minimize_window,
    "maximize":            maximize_window,
    "snap_left":           snap_left,
    "snap_right":          snap_right,
    "switch_window":       switch_window,
    "show_desktop":        show_desktop,
    "task_manager":        open_task_manager,
    "focus_search":        focus_search,
    "refresh_page":        refresh_page,
    "reload":              refresh_page,
    "close_tab":           close_tab,
    "new_tab":             new_tab,
    "next_tab":            next_tab,
    "prev_tab":            prev_tab,
    "go_back":             go_back,
    "go_forward":          go_forward,
    "zoom_in":             zoom_in,
    "zoom_out":            zoom_out,
    "zoom_reset":          zoom_reset,
    "find_on_page":        find_on_page,
    "scroll_up":           scroll_up,
    "scroll_down":         scroll_down,
    "scroll_top":          scroll_top,
    "scroll_bottom":       scroll_bottom,
    "page_up":             page_up,
    "page_down":           page_down,
    "copy":                copy,
    "paste":               paste,
    "cut":                 cut,
    "undo":                undo,
    "redo":                redo,
    "select_all":          select_all,
    "save":                save_file,
    "enter":               press_enter,
    "escape":              press_escape,
    "screenshot":          take_screenshot,
    "lock_screen":         lock_screen,
    "open_settings":       open_system_settings,
    "file_explorer":       open_file_explorer,
    "open_run":            open_run,
    "dark_mode":           dark_mode,
    "toggle_wifi":         toggle_wifi,
    "restart":             restart_computer,
    "shutdown":            shutdown_computer,
}

# ── What needs a human, and what just needs an undo ──────────────────────────
#
# The old gate was `_DANGEROUS_ACTIONS = {"restart", "shutdown"}` checked against
# a `confirmed` parameter the MODEL filled in — so the model confirmed its own
# shutdowns, and everything else (switching off the WiFi this assistant is
# talking over) had no gate at all.
#
# Two lists now, and the split is about reversibility, not about how alarming
# the word sounds:
#
#   _IRREVERSIBLE — a human presses a button on the HUD. Nothing else.
#   everything else — done immediately, with an undo pushed if it can be undone.
#
# Asking before every action is what makes an assistant unusable, and every
# question costs a round trip. Undo is both faster and safer than a prompt.
_IRREVERSIBLE = {
    "restart":     ("Restart this computer",
                    "Anything unsaved will be lost. The computer restarts in 10 seconds."),
    "shutdown":    ("Shut this computer down",
                    "Anything unsaved will be lost. The computer powers off in 10 seconds."),
    # Not obviously destructive, and that is exactly why it was missed: turning
    # the WiFi off cuts the assistant's own connection to the Live API, so it
    # cannot be asked to turn it back on.
    "toggle_wifi": ("Switch WiFi off or on",
                    "If this switches WiFi off, VISION loses its connection and "
                    "cannot switch it back on by voice."),
}

# Kept so anything still importing the old name keeps working.
_DANGEROUS_ACTIONS = set(_IRREVERSIBLE)


# ── Local intent resolution ──────────────────────────────────────────────────
#
# This used to be an entire extra Gemini call, made INSIDE the tool: the Live
# model called computer_settings, and computer_settings then asked a second
# model which action was meant. Every "turn the volume down" paid for two round
# trips — and when that second call failed, the fallback was
# `description.lower().replace(" ", "_")`, which turns the Turkish for "turn it
# down" into `sesi_kis`, i.e. straight to "Unknown action".
#
# Nothing here needs a language model. The Live model already understands the
# sentence; it only needed the vocabulary, which the tool declaration now spells
# out in full. What is left is spelling tolerance, and difflib does that in
# microseconds instead of ~600 ms and a quota unit.
_ALIASES = {
    "volume_up":       ("louder", "raise volume", "turn it up", "increase volume"),
    "volume_down":     ("quieter", "lower volume", "turn it down", "decrease volume"),
    "mute":            ("silence", "sound off", "no sound"),
    "brightness_up":   ("brighter", "raise brightness", "increase brightness"),
    "brightness_down": ("dimmer", "dim", "lower brightness", "decrease brightness"),
    "close_window":    ("close this", "close it"),
    "full_screen":     ("fullscreen", "maximise screen"),
    "show_desktop":    ("minimise everything", "go to desktop"),
    "lock_screen":     ("lock", "lock the pc", "lock computer"),
    "sleep_display":   ("screen off", "turn off the screen", "display off"),
    "dark_mode":       ("night mode", "light mode", "toggle theme"),
    "toggle_wifi":     ("wifi", "wi-fi", "internet off", "internet on"),
    "task_manager":    ("processes", "task list"),
    "screenshot":      ("capture screen", "take a screenshot", "snip"),
    "refresh_page":    ("refresh", "reload page"),
    "new_tab":         ("open a tab", "open new tab"),
    "shutdown":        ("power off", "turn off the computer", "switch off the pc"),
    "restart":         ("reboot", "restart the pc"),
    "unmute":          ("sound on", "unmute"),
    "media_next":      ("next song", "next track", "skip song"),
    "media_previous":  ("previous song", "previous track"),
}

_VALUE_ACTIONS = {"volume_set", "type_text", "press_key", "reload_n",
                  "scroll_up", "scroll_down", "brightness_set",
                  "seek_forward", "seek_backward"}

# `value` is how far these move, not how many times to call them.
_STEP_ACTIONS = {"volume_up", "volume_down", "brightness_up", "brightness_down"}

# A key press worth repeating: "zoom in three times", "back two pages".
_REPEATABLE = {"zoom_in", "zoom_out", "next_tab", "prev_tab", "go_back", "go_forward",
               "page_up", "page_down", "undo", "redo", "enter", "escape", "close_tab",
               "new_tab", "media_next", "media_previous"}


def _normalise(text: str) -> str:
    return (text or "").strip().lower().replace("-", "_").replace(" ", "_")


def _detect_action(description: str) -> dict:
    """Resolve a free-text description to an action name, locally.

    Returns {"action": name, "value": ...}; `action` is "" when nothing matched,
    which the caller turns into a message naming real candidates — one round
    trip, and only in the case that used to cost one anyway."""
    raw  = (description or "").strip()
    norm = _normalise(raw)
    if not norm:
        return {"action": "", "value": None}

    known = set(ACTION_MAP) | _VALUE_ACTIONS

    # 1. Already an action name.
    if norm in known:
        return {"action": norm, "value": None}

    low = raw.lower()

    # 2. "set volume to 30", "sesi 30 yap" — a number next to a volume word.
    num = re.search(r"(\d{1,3})\s*%?", low)
    if num and any(w in low for w in ("volume", "ses", "sound", "lautstark", "громкость")):
        return {"action": "volume_set", "value": max(0, min(100, int(num.group(1))))}

    # 3. Alias phrases.
    for action, phrases in _ALIASES.items():
        if any(_normalise(p) == norm or p in low for p in phrases):
            return {"action": action, "value": None}

    # 4. Fuzzy match on the action names — catches "fullscren", "volumeup".
    import difflib
    close = difflib.get_close_matches(norm, sorted(known), n=1, cutoff=0.72)
    if close:
        return {"action": close[0], "value": None}

    # 5. Substring: "increase_the_brightness" contains "brightness".
    for action in sorted(known, key=len, reverse=True):
        if len(action) > 4 and (action in norm or norm in action):
            return {"action": action, "value": None}

    return {"action": "", "value": None}


def _suggest(description: str) -> str:
    """What to tell the model when nothing matched. Names real actions so its
    retry lands, instead of the old 'Unknown action' dead end."""
    import difflib
    near = difflib.get_close_matches(_normalise(description),
                                     sorted(ACTION_MAP), n=5, cutoff=0.3)
    hint = ", ".join(near) if near else ", ".join(sorted(ACTION_MAP)[:12])
    return outcome.ask(
        "computer_settings",
        f"'{description}' does not match any computer action - the closest "
        f"are: {hint}. Which was meant?",
        said=description)

def computer_settings(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    if not _PYAUTOGUI:
        return outcome.failed(
            "computer_settings",
            "pyautogui is not installed, so the keyboard cannot be driven - "
            "run: pip install pyautogui")

    params      = parameters or {}
    raw_action  = params.get("action", "").strip()
    description = params.get("description", "").strip()
    value       = params.get("value", None)

    if not raw_action and description:
        detected   = _detect_action(description)
        raw_action = detected.get("action", "")
        if value is None:
            value = detected.get("value")

    action = raw_action.lower().strip().replace(" ", "_").replace("-", "_")

    if not action:
        return _suggest(description or raw_action)

    print(f"[Settings] Action: {action}  Value: {value}  OS: {_OS}")
    if player:
        print(f"[Settings] {action}")            # terminal, not the HUD

    # ── The gate ─────────────────────────────────────────────────────────────
    # A human presses a button, or this does not happen. The model can no longer
    # write its own permission slip, and the action itself is handed to the UI
    # rather than performed here — so returning early is not "declining", it is
    # "parked until someone says yes".
    if action in _IRREVERSIBLE:
        title, detail = _IRREVERSIBLE[action]
        func = ACTION_MAP.get(action)
        if func is None:
            return outcome.failed("computer_settings",
                                  "that is not an action this tool has",
                                  wanted=raw_action)
        if confirm.pending_title():
            return outcome.ask(
                "computer_settings",
                "there is already a confirmation waiting on screen - the user "
                "has to answer that one first",
                wanted=action)
        return confirm.request(
            key=action, title=title, detail=detail,
            run=lambda f=func, a=action: (f(), f"{a} done.")[1],
        )

    if action in ("volume_set", "set_volume"):
        # No level means no guess: the old default of 50 turned "set the volume"
        # with a lost value into a volume change nobody asked for.
        target = _number(value)
        if target is None:
            return outcome.ask("computer_settings",
                               "what level should the volume be, from 0 to 100?",
                               wanted="volume_set")
        target = max(0, min(100, target))
        try:
            before = volume_get()
            volume_set(target)
            if before is not None:
                push_undo(f"volume {before}% → {target}%",
                          lambda b=before: (volume_set(b), f"Back to {b}%.")[1])
            now = volume_get()
            if now is None:
                return outcome.unverified(
                    "computer_settings",
                    "this system does not report its volume, so the new "
                    "level could not be read back", wanted=action, asked=target)
            return outcome.done("computer_settings", wanted=action,
                                volume=now, verified=True)
        except Exception as e:
            return outcome.failed("computer_settings",
                                  f"the volume could not be set: {e}", wanted=action)

    if action in ("type_text", "write_on_screen", "type", "write"):
        text = str(value or params.get("text", "")).strip()
        if not text:
            return outcome.failed("computer_settings",
                                  "no text was given to type", wanted=action)
        enter_after = str(params.get("press_enter", "false")).lower() in ("true", "1", "yes")
        type_text(text, press_enter_after=enter_after)
        return outcome.done("computer_settings", wanted=action,
                            typed=text[:60])

    if action == "press_key":
        key = str(value or params.get("key", "")).strip()
        if not key:
            return outcome.failed("computer_settings",
                                  "no key was given to press", wanted=action)
        times = max(1, min(20, _number(params.get("times")) or 1))
        for _ in range(times):
            press_key(key)
        return outcome.done("computer_settings", wanted=action, key=key,
                            times=times if times > 1 else None)

    if action in ("reload_n", "refresh_n", "reload_page_n"):
        try:
            reload_page_n(int(value or 1))
            return outcome.done("computer_settings", wanted=action,
                                times=value or 1)
        except Exception as e:
            return outcome.failed("computer_settings",
                                  f"the reload failed: {e}", wanted=action)

    if action in ("scroll_up", "scroll_down"):
        notches = _number(value)
        notches = 5 if notches is None else abs(notches)
        if notches > 60:
            # An amount in pyautogui's Windows units, as the old default (500) was.
            notches = max(1, round(notches / 120))
        notches = max(1, min(60, notches))
        (scroll_up if action == "scroll_up" else scroll_down)(notches)
        return outcome.done("computer_settings", wanted=action, notches=notches)

    if action in ("brightness_set", "set_brightness"):
        target = _number(value)
        if target is None:
            return outcome.ask("computer_settings",
                               "what brightness, from 0 to 100?", wanted="brightness_set")
        target = max(0, min(100, target))
        before = brightness_get()
        try:
            brightness_set(target)
        except Exception as e:
            return outcome.failed("computer_settings",
                                  f"the brightness could not be set: {e}", wanted=action)
        if before is not None:
            push_undo(f"brightness {before}% → {target}%",
                      lambda b=before: (brightness_set(b), f"Brightness back to {b}%.")[1])
        now = brightness_get()
        if now is None:
            return outcome.unverified(
                "computer_settings",
                "this display does not report its brightness, so the new level could "
                "not be read back - an external monitor usually cannot be set from the "
                "computer at all", wanted=action, asked=target)
        return outcome.done("computer_settings", wanted=action, brightness=now, verified=True)

    if action in ("seek_forward", "seek_backward", "fast_forward", "rewind"):
        seconds = _number(value)
        seconds = 10 if seconds is None else max(1, abs(seconds))
        forward = action in ("seek_forward", "fast_forward")
        if shutil.which("playerctl"):
            before = _playerctl_position()
            r = subprocess.run(["playerctl", "position", f"{seconds}{'+' if forward else '-'}"],
                               capture_output=True, timeout=5)
            after = _playerctl_position()
            if r.returncode == 0 and before is not None and after is not None:
                moved = after - before
                if abs(moved - (seconds if forward else -seconds)) <= 3:
                    return outcome.done("computer_settings", wanted=action,
                                        seconds=seconds, position=round(after), verified=True)
        try:
            presses = seek_presses(seconds if forward else -seconds)
        except Exception as e:
            return outcome.failed("computer_settings", f"it failed: {e}", wanted=action)
        return outcome.unverified(
            "computer_settings",
            f"sent {presses} arrow-key presses to the window in front - most web players "
            f"move {SEEK_PER_PRESS} seconds a press, but players differ and none reports "
            f"its position. If the video is on the assistant's own display, use "
            f"video_player action='seek' instead, which is exact",
            wanted=action, seconds=seconds)

    func = ACTION_MAP.get(action)
    if not func:
        return _suggest(raw_action or description)

    # ── Capture "before" so the change can be taken back ─────────────────────
    # Read-then-write is the whole mechanism for settings: there is no clever
    # inverse to compute, just a value to remember. Where the platform will not
    # tell us the current value, nothing is registered — an undo that restores
    # a guess is worse than no undo at all.
    _before = None
    if action in ("volume_up", "volume_down"):
        _before = ("volume", volume_get())
    elif action in ("mute", "unmute", "toggle_mute"):
        _before = ("mute", mute_get())
    elif action in ("brightness_up", "brightness_down"):
        _before = ("brightness", brightness_get())

    # How far, and how many times. "Louder by 30" is a step of 30; "zoom in three
    # times" is three presses. Neither number used to reach the action at all.
    step = _number(value)
    step = 10 if step is None else max(1, min(100, abs(step)))
    times = 1
    if action in _REPEATABLE:
        times = _number(params.get("times"))
        if times is None:
            times = _number(value)
        times = max(1, min(20, times or 1))

    try:
        if action in _STEP_ACTIONS:
            func(step)
        else:
            for i in range(times):
                func()
                if i + 1 < times:
                    time.sleep(0.08)
    except Exception as e:
        print(f"[Settings] Action failed ({action}): {e}")
        return outcome.failed("computer_settings", f"it failed: {e}",
                              wanted=action)

    if _before:
        kind, old = _before
        if old is not None:
            if kind == "mute":
                push_undo(f"sound ({action})",
                          lambda b=old: (mute_set(b), "Sound back as it was.")[1])
            elif kind == "volume":
                push_undo(f"volume ({action})",
                          lambda b=old: (volume_set(b), f"Volume back to {b}%.")[1])
            elif kind == "brightness":
                push_undo(f"brightness ({action})",
                          lambda b=old: (brightness_set(b), f"Brightness back to {b}%.")[1])
    elif action == "dark_mode":
        # A pure toggle: calling it again is the undo.
        push_undo("dark mode toggled",
                  lambda: (dark_mode(), "Theme switched back.")[1])

    # ── Say what is known, and only that ─────────────────────────────────────
    #
    # This used to be `return f"Done: {action}."`, sent whether or not anything
    # had changed, because `func()` returns nothing and nobody looked.
    #
    # WHERE THE LINE IS, AND WHY IT IS NOT "VERIFY EVERYTHING"
    #     Volume and brightness are a state the machine will read back, and the
    #     "before" value has ALREADY been read two blocks above for undo — so
    #     checking costs one more call and turns a claim into a fact.
    #
    #     Most of the other fifty-odd actions are a keystroke sent to whatever
    #     has focus, and there is nothing to read. But they are also things the
    #     user is looking straight at: scrolling, switching tabs, snapping a
    #     window. Hedging on those would have the assistant say "I sent a scroll
    #     and cannot confirm it" about something the person just watched happen,
    #     which is noise dressed as honesty.
    #
    #     So the rule is not "verify everything", it is: claim an OUTCOME only
    #     where the outcome was observed, and otherwise claim only what was
    #     actually done. `open_app` lied because it reported an application
    #     opening from a shell starting — two different events. Reporting a
    #     keystroke as a keystroke is not that.
    if _before and _before[0] == "mute":
        old_state = _before[1]
        now_muted = mute_get()
        if now_muted is None:
            return outcome.unverified(
                "computer_settings",
                "this system does not report whether its sound is muted, so the "
                "change could not be read back", wanted=action)
        wanted_state = {"mute": True, "unmute": False}.get(
            action, (not old_state) if old_state is not None else now_muted)
        if now_muted != wanted_state:
            return outcome.unverified(
                "computer_settings",
                f"the sound still reads {'muted' if now_muted else 'not muted'}",
                wanted=action, muted=now_muted)
        return outcome.done("computer_settings", wanted=action, muted=now_muted, verified=True)

    if _before:
        kind, old_value = _before
        now = volume_get() if kind == "volume" else brightness_get()
        if old_value is None or now is None:
            return outcome.unverified(
                "computer_settings",
                f"this system does not report its {kind}, so the change could "
                f"not be read back",
                wanted=action)
        if now == old_value:
            return outcome.unverified(
                "computer_settings",
                f"the {kind} still reads {now}% - it may already have been at "
                f"the limit, or this system may not have accepted the change",
                wanted=action, value=now)
        return outcome.done("computer_settings", wanted=action,
                            **{kind: now}, changed_by=step, verified=True)

    return outcome.done("computer_settings", wanted=action,
                        times=times if times > 1 else None)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "computer_settings",
    "description": "Controls the computer: volume, mute, brightness, media playback (play/pause, next, previous, seek), window management, keyboard shortcuts, typing text on screen, fullscreen, dark mode, WiFi, restart, shutdown, scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. Use for ANY single computer control command. NUMBERS MATTER: a level the user names is volume_set / brightness_set with value = that level ('volume to 100', 'max' = 100, 'half' = 50); an amount to change by is volume_up / volume_down / brightness_up / brightness_down with value = the amount ('30 louder' = 30; no amount = 10). Never approximate a level with repeated up/down calls. For a key worth repeating (zoom_in, next_tab, go_back, page_down, undo...) pass `times`. Media keys reach music and video apps without focusing them; seek_forward / seek_backward take value = seconds and act on the app in front (for a video on the assistant's own display use video_player instead). restart, shutdown and toggle_wifi put a confirmation on the user's screen and do NOT happen until they press it — never claim they are done. Volume, mute, brightness and dark mode can be reversed with the `undo` tool.",
    # Used in place of `description` only when a small model is on a tool
    # budget - Gemini still gets the full text. See providers/schema.py.
    "brief": (
        "Changes a computer setting: volume, brightness, WiFi, window layout, power. Name the action in 'action'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            # The exact vocabulary, spelled out.
            #
            # This used to say only "The action to perform", so the model
            # usually filled `description` instead — and computer_settings then
            # made a SECOND Gemini call, inside the tool, purely to translate
            # that sentence into one of these names. Every "turn the volume
            # down" cost two model round trips.
            #
            # Naming the actions here costs ~600 characters once per session and
            # removes a whole round trip from every computer command.
            "action": {
                "type": "STRING",
                "description": (
                    "The exact action. Prefer this over `description` — pick one of: "
                    "volume_set | volume_up | volume_down | mute | unmute | toggle_mute | "
                    "brightness_set | brightness_up | brightness_down | sleep_display | "
                    "media_play_pause | media_next | media_previous | media_stop | "
                    "seek_forward | seek_backward | "
                    "pause_video | close_window | full_screen | "
                    "minimize | maximize | snap_left | snap_right | "
                    "switch_window | show_desktop | task_manager | focus_search | "
                    "refresh_page | close_tab | new_tab | next_tab | prev_tab | "
                    "go_back | go_forward | zoom_in | zoom_out | zoom_reset | "
                    "find_on_page | scroll_up | scroll_down | scroll_top | "
                    "scroll_bottom | page_up | page_down | copy | paste | cut | "
                    "undo | redo | select_all | save | enter | escape | press_key | "
                    "type_text | screenshot | lock_screen | open_settings | "
                    "file_explorer | open_run | dark_mode | toggle_wifi | "
                    "restart | shutdown"
                )
            },
            "description": {
                "type": "STRING",
                "description": (
                    "Fallback only, when no action name above fits. "
                    "Resolved locally — no extra model call."
                )
            },
            "value": {
                "type": "STRING",
                "description": (
                    "volume_set / brightness_set: the level, 0-100. volume_up/down, "
                    "brightness_up/down: how much to change by (default 10). "
                    "seek_forward/backward: seconds. scroll_up/down: wheel notches. "
                    "type_text: the text. press_key: the key name."
                )
            },
            "times": {
                "type": "INTEGER",
                "description": "How many times to repeat a key action (zoom_in, next_tab, go_back, press_key...). Default 1."
            }
        },
        "required": []
    },
    "handler": computer_settings,
}
