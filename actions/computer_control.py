#computer_control.py
import io
import json
import re
import string
import subprocess
import sys
import time
import random
from pathlib import Path

from core import appidentity, outcome
from core.lazyimport import LazyModule, installed

# pyautogui and pyperclip are imported on first USE, not at launch — see
# core/lazyimport.py for why and for what stays the same. The flags still mean
# "is it installed?" and still answer without importing anything.
_PYAUTOGUI = installed("pyautogui")
_PYPERCLIP = installed("pyperclip")


def _configure_pyautogui(module) -> None:
    """Settings that used to sit beside the import, run once when it happens."""
    module.FAILSAFE = True
    module.PAUSE    = 0.05


pyautogui = LazyModule("pyautogui", _configure_pyautogui)
pyperclip = LazyModule("pyperclip")

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE         = _base_dir()
_CONFIG_PATH  = _BASE / "config" / "api_keys.json"
_MEMORY_PATH  = _BASE / "memory" / "long_term.json"

def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _platform_os() -> str:
    return "linux"

def _get_os() -> str:
    return _load_config().get("os_system", _platform_os()).lower()


def _get_api_key() -> str:
    return _load_config().get("gemini_api_key", "")

_SAFE_SCREENSHOT_ROOTS = (
    Path.home(),
)

def _safe_screenshot_path(requested: str | None) -> Path:
    fallback = Path.home() / "Desktop" / "vision_screenshot.png"
    if not requested:
        return fallback
    try:
        p = Path(requested).expanduser().resolve()
        for root in _SAFE_SCREENSHOT_ROOTS:
            if p.is_relative_to(root.resolve()):
                p.parent.mkdir(parents=True, exist_ok=True)
                return p
    except Exception:
        pass
    return fallback

def _require_pyautogui():
    """Refuse early, with the sentence that says what to install.

    The import itself is the module `__getattr__`'s job; this stays exactly
    what it was — the check that turns a missing optional dependency into an
    instruction rather than a traceback.
    """
    if not _PYAUTOGUI:
        raise RuntimeError("PyAutoGUI not installed. Run: pip install pyautogui")


def _require_pyperclip() -> bool:
    """Is the clipboard usable? Returns a bool rather than raising, because
    every caller already has a hotkey fallback written for the case where it
    is not — turning that into an exception would break the path built to
    survive it."""
    return _PYPERCLIP

_FIRST_NAMES = [
    "Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Drew", "Quinn",
    "Avery", "Blake", "Cameron", "Dakota", "Emerson", "Finley", "Harper",
]
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Wilson", "Moore", "Taylor", "Anderson", "Thomas", "Jackson",
]
_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "proton.me", "mail.com"]


def _random_data(data_type: str) -> str:
    dt = data_type.lower().strip()

    if dt == "first_name":
        return random.choice(_FIRST_NAMES)

    if dt == "last_name":
        return random.choice(_LAST_NAMES)

    if dt == "name":
        return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"

    if dt == "email":
        first = random.choice(_FIRST_NAMES).lower()
        last  = random.choice(_LAST_NAMES).lower()
        num   = random.randint(10, 999)
        return f"{first}.{last}{num}@{random.choice(_DOMAINS)}"

    if dt == "username":
        return f"{random.choice(_FIRST_NAMES).lower()}{random.randint(100, 9999)}"

    if dt == "password":
        chars = string.ascii_letters + string.digits + "!@#$%"
        raw   = (
            random.choice(string.ascii_uppercase)
            + random.choice(string.digits)
            + random.choice("!@#$%")
            + "".join(random.choices(chars, k=9))
        )
        return "".join(random.sample(raw, len(raw)))

    if dt == "phone":
        return f"+1{random.randint(200,999)}{random.randint(1_000_000, 9_999_999)}"

    if dt == "birthday":
        y = random.randint(1980, 2000)
        m = random.randint(1, 12)
        d = random.randint(1, 28)
        return f"{m:02d}/{d:02d}/{y}"

    if dt == "address":
        num    = random.randint(100, 9999)
        street = random.choice(["Main St", "Oak Ave", "Park Blvd", "Elm St", "Cedar Ln"])
        return f"{num} {street}"

    if dt == "zip_code":
        return str(random.randint(10000, 99999))

    if dt == "city":
        return random.choice(["New York", "Los Angeles", "Chicago", "Houston", "Phoenix"])

    return f"random_{data_type}_{random.randint(1000, 9999)}"

def _user_profile() -> dict:
    """Read identity fields from long-term memory."""
    try:
        if _MEMORY_PATH.exists():
            data     = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
            identity = data.get("identity", {})
            return {k: v.get("value", "") for k, v in identity.items()}
    except Exception:
        pass
    return {}

def _on_wayland() -> bool:
    """True when input and capture go through core/wayland_desktop.py."""
    try:
        from core import wayland_desktop
        return wayland_desktop.available()
    except Exception:
        return False


def _use_screen(which: str) -> str:
    if not _on_wayland():
        return "Choosing a screen is only needed on Wayland; nothing changed."
    from core import wayland_desktop
    chosen = wayland_desktop.use_monitor(which or "next")
    return (f"Now working on screen {chosen['number']} "
            f"({chosen['width']}x{chosen['height']}). "
            f"All screens: {wayland_desktop.describe_monitors()}")


def _list_screens() -> str:
    if not _on_wayland():
        w, h = pyautogui.size()
        return f"One working screen, {w}x{h}."
    from core import wayland_desktop
    return wayland_desktop.describe_monitors()


def _release() -> str:
    if not _on_wayland():
        return "Nothing to release on this system."
    from core import wayland_desktop
    return f"Mouse, keyboard and screen: {wayland_desktop.release()}."


def _type(text: str, interval: float = 0.03) -> str:
    _require_pyautogui()
    time.sleep(0.3)
    pyautogui.typewrite(text, interval=interval)
    return f"Typed: {text[:60]}{'…' if len(text) > 60 else ''}"


def _smart_type(text: str, clear_first: bool = True) -> str:
    _require_pyautogui()
    if clear_first:
        _clear_field()
        time.sleep(0.1)

    # Vision: on Wayland the text is typed directly. The clipboard route needs
    # extra tools there and would overwrite whatever the user had copied.
    if len(text) > 20 and _require_pyperclip() and not _on_wayland():
        pyperclip.copy(text)
        time.sleep(0.1)
        paste_key = "ctrl"
        pyautogui.hotkey(paste_key, "v")
        return f"Smart-typed (clipboard): {text[:60]}{'…' if len(text) > 60 else ''}"

    pyautogui.typewrite(text, interval=0.04)
    return f"Smart-typed: {text[:60]}{'…' if len(text) > 60 else ''}"


def _click(x=None, y=None, button: str = "left", clicks: int = 1) -> str:
    _require_pyautogui()
    if x is not None and y is not None:
        pyautogui.click(x, y, button=button, clicks=clicks)
        return f"{'Double-c' if clicks == 2 else 'C'}licked ({x}, {y}) [{button}]"
    pyautogui.click(button=button, clicks=clicks)
    return f"Clicked at current position [{button}]"


def _hotkey(*keys) -> str:
    _require_pyautogui()
    pyautogui.hotkey(*keys)
    return f"Hotkey: {'+'.join(keys)}"


def _press(key: str) -> str:
    _require_pyautogui()
    pyautogui.press(key)
    return f"Pressed: {key}"


def _scroll(direction: str = "down", amount: int = 3) -> str:
    _require_pyautogui()
    vertical   = direction in ("up", "down")
    amount     = max(1, min(60, abs(int(amount))))
    # `amount` is wheel notches. pyautogui counts 1/120 of a notch on Windows and
    # whole notches elsewhere, so "scroll 3" moved the page by nothing on Windows.
    units      = amount
    clicks     = units if direction in ("up", "right") else -units
    pyautogui.scroll(clicks) if vertical else pyautogui.hscroll(clicks)
    return f"Scrolled {direction} ×{amount}"


def _move(x: int, y: int, duration: float = 0.3) -> str:
    _require_pyautogui()
    pyautogui.moveTo(x, y, duration=duration)
    return f"Mouse → ({x}, {y})"


def _drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> str:
    _require_pyautogui()
    pyautogui.moveTo(x1, y1, duration=0.2)
    pyautogui.dragTo(x2, y2, duration=duration, button="left")
    return f"Dragged ({x1},{y1}) → ({x2},{y2})"


def _clipboard_get() -> str:
    if _require_pyperclip():
        return pyperclip.paste()
    _hotkey("ctrl", "c")
    time.sleep(0.2)
    return "(copied — pyperclip unavailable for read)"


def _clipboard_paste(text: str) -> str:
    if _require_pyperclip():
        pyperclip.copy(text)
        time.sleep(0.1)
        _require_pyautogui()
        paste_key = "ctrl"
        pyautogui.hotkey(paste_key, "v")
        return f"Pasted: {text[:60]}{'…' if len(text) > 60 else ''}"
    return "pyperclip not available"


def _screenshot(save_path: str | None = None) -> str:
    _require_pyautogui()
    path = _safe_screenshot_path(save_path)
    img  = pyautogui.screenshot()
    img.save(str(path))
    return f"Screenshot saved: {path}"


def _clear_field() -> str:
    _require_pyautogui()
    select_key = "ctrl"
    pyautogui.hotkey(select_key, "a")
    time.sleep(0.1)
    pyautogui.press("delete")
    return "Field cleared"

def _focus_window(title: str) -> str:
    os_name = _get_os()

    try:
        result = subprocess.run(
            ["wmctrl", "-a", title],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0:
            time.sleep(0.3)
            return f"Focused window: {title}"
    except FileNotFoundError:
        pass
    try:
        result = subprocess.run(
            ["xdotool", "search", "--name", title, "windowactivate"],
            capture_output=True, timeout=5,
        )
        time.sleep(0.3)
        return f"Focused window: {title}"
    except FileNotFoundError:
        return "focus_window (Linux) requires wmctrl or xdotool"
    except Exception as e:
        return f"focus_window (Linux) failed: {e}"

    return f"focus_window: unknown OS '{os_name}'"

def _stop_app(app_name: str, confirmed_index: int | None = None,
              force: bool = False) -> str:
    """Stop or force-kill every process belonging to the application the user
    named — never the assistant itself."""
    action_label = "kill_app" if force else "close_app"
    app_name = (app_name or "").strip()
    if not app_name:
        return outcome.failed(action_label, "no application name was given")

    if not appidentity._PSUTIL:
        return outcome.failed(
            action_label,
            "psutil is not installed, so running applications cannot be found "
            "- run: pip install psutil",
            app=app_name,
        )

    matches = appidentity.running(app_name)

    if not matches:
        # Nothing, or nothing that is not us. Telling those two apart matters:
        # "it is not running" and "the only thing by that name is me" need
        # completely different sentences.
        including_self = appidentity.running(app_name, include_self=True)
        if any(m.is_self for m in including_self):
            return outcome.failed(
                action_label,
                "the only thing matching that name is the assistant itself, "
                "and it will not close itself - if the user really wants the "
                "assistant closed they should close its window themselves",
                app=app_name,
            )
        return outcome.failed(
            action_label,
            "no running application matches that name - it may already be "
            "closed, or be running under a different name",
            app=app_name,
        )

    # Group by process NAME, because twenty `opera.exe` are one application
    # with nineteen helpers, not twenty applications — and closing/killing
    # "Opera" means all of them. `matches` is already best-first, so insertion
    # order here ranks the groups.
    by_name: dict[str, list] = {}
    for match in matches:
        by_name.setdefault(match.label, []).append(match)

    ranked = list(by_name.items())          # [(name, [matches...]), ...]
    target_name, group = ranked[0]

    # Two DIFFERENT applications that answer equally well is a question, not a
    # coin toss. Only when they tie: "discord" beating "discordcanary" on an
    # exact match is a clear winner and interrupting the user to confirm it
    # would be the assistant refusing to do an unambiguous thing.
    if len(ranked) > 1 and not confirmed_index:
        best_score = group[0].score
        tied = [name for name, ms in ranked if ms[0].score == best_score]
        if len(tied) > 1:
            verb = "killed" if force else "closed"
            return outcome.ask(
                action_label,
                f"more than one application matches '{app_name}' equally well: "
                f"{', '.join(tied[:4])} - which one should be {verb}?",
                app=app_name, candidates=len(tied),
            )
        print(f"[ComputerControl] {action_label}: '{app_name}' -> {target_name} "
              f"(also matched: {', '.join(n for n, _ in ranked[1:5])})")

    if force:
        stopped, still_open, reason = appidentity.kill_app_processes(group)
    else:
        stopped, still_open, reason = appidentity.close_app_processes(
            group, force_after=True
        )

    result_key = "killed" if force else "closed"
    if stopped and not still_open:
        return outcome.done(
            action_label, app=app_name, **{result_key: target_name},
            processes=stopped, verified=True,
        )

    if stopped and still_open:
        return outcome.unverified(
            action_label,
            f"{stopped} of {stopped + still_open} processes {result_key}; the "
            f"rest are still running - {reason}",
            app=app_name, **{result_key: target_name},
        )

    return outcome.failed(
        action_label,
        reason or f"the application could not be {result_key}",
        app=app_name, matched=target_name,
    )


def _close_app(app_name: str, confirmed_index: int | None = None) -> str:
    """Find the application in the process table and terminate/kill all
    processes belonging to it."""
    return _stop_app(app_name, confirmed_index=confirmed_index, force=False)


def _kill_app(app_name: str, confirmed_index: int | None = None) -> str:
    """Find the application in the process table and immediately force-kill
    (`SIGKILL`) all processes belonging to it."""
    return _stop_app(app_name, confirmed_index=confirmed_index, force=True)


def _focus_app_if_named(app_name: str) -> str | None:
    """If `app_name` is given, bring that running application's window to the
    front before sending window-management or tiling shortcuts."""
    app_name = (app_name or "").strip()
    if not app_name:
        return None
    for candidate in appidentity.running(app_name, include_self=True)[:8]:
        if appidentity.focus(candidate):
            time.sleep(0.2)
            return candidate.label
    # Fallback to gtk-launch for single-instance desktop apps on GNOME Wayland,
    # or wmctrl/xdotool by title.
    launcher = appidentity.launcher_name(app_name)
    for name in (launcher.lower(), app_name.lower(), app_name.lower().replace(" ", "-")):
        try:
            r = subprocess.run(["gtk-launch", name], capture_output=True, timeout=3)
            if r.returncode == 0:
                time.sleep(0.3)
                return app_name
        except Exception:
            break
    _focus_window(app_name)
    return app_name


def _tiling_shell(
    tiling_action: str,
    direction: str = "",
    monitor: str = "",
    app_name: str = "",
    steps: int = 1,
) -> str:
    """Move, tile, span, center, untile or move a window across monitors using
    GNOME Tiling Shell (`tilingshell@ferrarodomenico.com`) and GNOME monitor
    shortcuts."""
    focused = _focus_app_if_named(app_name)
    if _on_wayland():
        from core import wayland_desktop
        info = wayland_desktop.tiling_shell_action(
            tiling_action,
            direction=direction,
            monitor=monitor if monitor else None,
            steps=steps,
        )
        return outcome.done(
            "tiling_shell",
            window_action=info.get("action", tiling_action),
            app=focused or (app_name or None),
            direction=direction or None,
            monitor=info.get("monitor") or (monitor or None),
            steps=info.get("steps"),
            verified=True,
        )

    # Non-Wayland fallback via pyautogui hotkeys
    _require_pyautogui()
    act = (tiling_action or "tile").strip().lower().replace("-", "_")
    d = (direction or "").strip().lower()
    if act in ("move_to_monitor", "move_window_to_monitor", "monitor"):
        move_dir = d if d in ("left", "right", "up", "down") else "right"
        pyautogui.hotkey("super", "shift", move_dir)
    elif act in ("span", "span_window") and d in ("left", "right", "up", "down"):
        pyautogui.hotkey("ctrl", "super", d)
    elif act in ("span_all", "span_all_tiles"):
        pyautogui.hotkey("ctrl", "super", "a")
    elif act in ("untile", "untile_window"):
        pyautogui.hotkey("super", "u")
    elif act in ("untile_all", "untile_all_windows"):
        pyautogui.hotkey("shift", "super", "u")
    elif act in ("center", "center_window"):
        pyautogui.hotkey("ctrl", "alt", "c")
    else:
        tile_dir = d if d in ("left", "right", "up", "down") else "left"
        for _ in range(max(1, int(steps or 1))):
            pyautogui.hotkey("super", tile_dir)
    return outcome.done(
        "tiling_shell",
        window_action=act,
        app=focused or (app_name or None),
        direction=direction or None,
        monitor=monitor or None,
    )


def _parse_box(text: str, full_w: int, full_h: int) -> tuple[int, int] | None:
    """Centre, in screen pixels, of the box a model gave as
    [ymin, xmin, ymax, xmax] on a 0-1000 scale. None for an answer that is not
    such a box or lies off the screen."""
    inside = re.search(r"\[([^\]]*)\]", text)          # "box_2d: [..]" has a 2 outside
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", inside.group(1) if inside else text)[:4]]
    if len(nums) < 4:
        return None
    ymin, xmin, ymax, xmax = nums
    if not (0 <= ymin <= ymax <= 1000 and 0 <= xmin <= xmax <= 1000):
        return None
    x = round((xmin + xmax) / 2 / 1000 * full_w)
    y = round((ymin + ymax) / 2 / 1000 * full_h)
    return min(max(x, 0), full_w - 1), min(max(y, 0), full_h - 1)


def _locate(description: str):
    """((x, y), screenshot) for the element, or (None, screenshot)."""
    api_key = _get_api_key()
    if not api_key:
        print("[ComputerControl] ⚠️ No API key for screen_find")
        return None, None

    try:
        from google.genai import types as gtypes

        _require_pyautogui()
        full = pyautogui.screenshot()
        full_w, full_h = full.size
        img = full
        # Vision: a very wide screen (5120 px here) is shrunk before it is
        # sent. The answer is asked for on Gemini's own 0-1000 scale, the
        # way it is trained to give boxes: asked for pixels, it answered
        # with x in pixels and y on that scale, 370 px off on the 5120x1440
        # screen.
        if full_w > 2560:
            ratio = 2560 / full_w
            img = full.resize((2560, max(1, round(full_h * ratio))))
        buf = io.BytesIO()
        img.save(buf, format="PNG")

        prompt = (
            f"Find the UI element described as: '{description}'. "
            "Reply with ONLY its bounding box as [ymin, xmin, ymax, xmax], "
            "each value normalised to 0-1000 over the whole image. "
            "If the element is not visible, reply: NOT_FOUND"
        )

        from core import gemini
        response = gemini.call(
            [gtypes.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"), prompt],
            tier=gemini.FAST, timeout_ms=20_000,
        )
        if response is None:
            return None, full
        text = (response.text or "").strip()
        if "NOT_FOUND" in text.upper():
            return None, full
        coords = _parse_box(text, full_w, full_h)
        if coords is None:
            print(f"[ComputerControl] ⚠️ screen_find: unusable answer {text[:80]!r}")
        return coords, full

    except Exception as e:
        print(f"[ComputerControl] ⚠️ screen_find failed: {e}")
    return None, None


def _screen_find(description: str) -> tuple[int, int] | None:
    return _locate(description)[0]


def _cursor_box(x: int, y: int) -> tuple[int, int, int, int]:
    # The pointer is drawn from its tip down and to the right; 72 px covers a
    # large cursor theme.
    return (x - 16, y - 16, x + 72, y + 72)


def _changed_near(before, x: int, y: int, radius: int = 120) -> bool | None:
    """Whether the screen around (x, y) looks different from `before`, which
    was taken with the pointer already resting at (x, y). None when it cannot
    be told.

    Screenshots include the mouse pointer (on Wayland the portal draws it into
    the frame), and its shape can change on a click, so the area under it is
    left out."""
    try:
        from PIL import ImageChops, ImageDraw
        after = pyautogui.screenshot()
        if before is None or after.size != before.size:
            return None
        imgs = [before.convert("RGB"), after.convert("RGB")]
        for img in imgs:
            draw = ImageDraw.Draw(img)
            draw.rectangle(_cursor_box(x, y), fill=(0, 0, 0))
        box = (max(0, x - radius), max(0, y - radius),
               min(after.size[0], x + radius), min(after.size[1], y + radius))
        diff = ImageChops.difference(imgs[0].crop(box), imgs[1].crop(box))
        return diff.getbbox() is not None
    except Exception as e:
        print(f"[ComputerControl] ⚠️ could not compare screenshots: {e}")
        return None


def computer_control(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Dispatch table for all computer control actions.

    parameters keys (all optional unless noted):
      action        : (required) one of the actions listed below
      text          : text to type or paste
      x, y          : screen coordinates
      button        : 'left' | 'right' (default: left)
      keys          : hotkey string, e.g. 'ctrl+c'
      key           : single key name, e.g. 'enter'
      direction     : 'up' | 'down' | 'left' | 'right'
      amount        : scroll amount (default: 3)
      seconds       : wait duration
      title         : window title fragment for focus_window
      description   : natural-language element description for screen_find/click
      type          : data type for random_data
      field         : memory field name for user_data
      clear_first   : bool, clear field before typing (default: true)
      path          : save path for screenshot (must be inside home dir)

    Actions:
      type          — type text at cursor
      smart_type    — clear field + type (clipboard-backed)
      click         — left click
      double_click  — double left click
      right_click   — right click
      move          — move mouse
      drag          — click-drag between two points
      hotkey        — key combination
      press         — single key
      scroll        — scroll the wheel
      copy          — read clipboard
      paste         — write + paste clipboard
      screenshot    — capture screen (safe path only)
      wait          — sleep N seconds
      clear_field   — select-all + delete
      focus_window  — bring window to foreground
      screen_find   — AI element finder (returns x,y)
      screen_click  — AI element finder + click
      random_data   — generate fake form data
      user_data     — pull real data from memory
    """
    params = parameters or {}
    action = params.get("action", "").lower().strip()

    if not action:
        return "No action specified for computer_control."

    if player:
        # Terminal, not the HUD: the activity log is for the conversation and
        # for problems, not a receipt for every keystroke. See
        # core/outcome.is_problem().
        print(f"[ComputerControl] {action}")

    print(f"[ComputerControl] ▶ {action}  {params}")

    try:

        if action == "type":
            return _type(params.get("text", ""))

        if action == "smart_type":
            return _smart_type(
                params.get("text", ""),
                clear_first=params.get("clear_first", True),
            )

        if action in ("click", "left_click"):
            return _click(params.get("x"), params.get("y"), "left", 1)

        if action == "double_click":
            return _click(params.get("x"), params.get("y"), "left", 2)

        if action == "right_click":
            return _click(params.get("x"), params.get("y"), "right", 1)

        if action == "move":
            return _move(int(params.get("x", 0)), int(params.get("y", 0)))

        if action == "drag":
            return _drag(
                int(params.get("x1", 0)), int(params.get("y1", 0)),
                int(params.get("x2", 0)), int(params.get("y2", 0)),
            )

        if action == "hotkey":
            raw  = params.get("keys", "")
            keys = [k.strip() for k in raw.split("+")] if isinstance(raw, str) else raw
            return _hotkey(*keys)

        if action == "press":
            return _press(params.get("key", "enter"))

        if action == "scroll":
            return _scroll(
                direction=params.get("direction", "down"),
                amount=int(params.get("amount", 3)),
            )

        if action == "copy":
            return _clipboard_get()

        if action == "paste":
            return _clipboard_paste(params.get("text", ""))

        if action == "screenshot":
            return _screenshot(params.get("path"))

        if action in ("use_screen", "switch_screen", "use_monitor"):
            return _use_screen(str(params.get("screen") or params.get("text") or ""))

        if action in ("screens", "list_screens"):
            return _list_screens()

        if action in ("release", "hands_off", "stop_control"):
            return _release()

        if action == "screen_find":
            coords = _screen_find(params.get("description", ""))
            return f"{coords[0]},{coords[1]}" if coords else "NOT_FOUND"

        if action == "screen_click":
            desc   = params.get("description", "")
            coords, _ = _locate(desc)
            if coords:
                # The baseline for the missed-click check is taken with the
                # pointer already on the spot, so the pointer and any hover
                # effect are the same in both pictures. Wayland cannot say
                # where the real pointer is, so an earlier picture could not
                # be trusted.
                before = None
                try:
                    pyautogui.moveTo(coords[0], coords[1])
                    time.sleep(0.3)
                    before = pyautogui.screenshot()
                except Exception as e:
                    print(f"[ComputerControl] ⚠️ no picture before the click: {e}")
                _click(x=coords[0], y=coords[1])
                time.sleep(0.6)
                changed = _changed_near(before, coords[0], coords[1])
                if changed is False:
                    return (f"Clicked where '{desc}' seemed to be, at {coords}, but "
                            f"nothing on screen changed there, so it may have missed. "
                            f"Check before going on.")
                return f"Clicked '{desc}' at {coords}"
            return f"Element not found on screen: '{desc}'"

        if action == "wait":
            secs = float(params.get("seconds", 1.0))
            secs = min(secs, 30.0)
            time.sleep(secs)
            return f"Waited {secs}s"

        if action == "clear_field":
            return _clear_field()

        if action == "focus_window":
            return _focus_window(params.get("title", ""))

        if action in ("close_app", "close_application", "quit_app"):
            return _close_app(
                params.get("app_name") or params.get("text") or "",
                confirmed_index=params.get("confirmed"),
            )

        if action in ("kill_app", "kill_application", "force_close", "force_quit", "kill"):
            return _kill_app(
                params.get("app_name") or params.get("text") or "",
                confirmed_index=params.get("confirmed"),
            )

        if action in (
            "move_window_to_monitor", "move_to_monitor", "window_to_monitor",
            "send_to_monitor", "move_app_to_monitor",
        ):
            target_mon = (
                params.get("monitor")
                or params.get("screen")
                or params.get("direction")
                or params.get("text")
                or ""
            )
            return _tiling_shell(
                "move_to_monitor",
                direction=str(params.get("direction") or ""),
                monitor=str(target_mon),
                app_name=str(params.get("app_name") or params.get("title") or ""),
                steps=int(params.get("amount") or 1),
            )

        if action in (
            "tiling_shell", "tile_window", "tile", "move_tile", "move_window",
            "snap_window", "span_window", "span_tile", "span", "span_all_tiles",
            "span_all", "untile_window", "untile", "untile_all_windows",
            "untile_all", "center_window", "center", "focus_tile",
            "highlight_window", "cycle_layout", "next_layout", "prev_layout",
            "layout_editor",
        ):
            sub_action = action
            if action == "tiling_shell" and params.get("type"):
                sub_action = str(params.get("type"))
            elif action in ("tile_window", "move_window", "snap_window"):
                sub_action = "tile"
            elif action in ("span_window", "span_tile"):
                sub_action = "span"
            target_mon = params.get("monitor") or params.get("screen") or ""
            return _tiling_shell(
                sub_action,
                direction=str(params.get("direction") or params.get("text") or ""),
                monitor=str(target_mon),
                app_name=str(params.get("app_name") or params.get("title") or ""),
                steps=int(params.get("amount") or 1),
            )

        if action == "random_data":
            dt     = params.get("type", "name")
            result = _random_data(dt)
            print(f"[ComputerControl] 🎲 random {dt} → {result}")
            return result

        if action == "user_data":
            field   = params.get("field", "name")
            profile = _user_profile()
            value   = profile.get(field, "")
            if not value:
                value = _random_data(field)
                print(f"[ComputerControl] ⚠️ No '{field}' in memory, using random: {value}")
            return value

        return f"Unknown action: '{action}'"

    except Exception as e:
        print(f"[ComputerControl] ❌ {action}: {e}")
        return f"computer_control '{action}' failed: {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "computer_control",
    "description": (
        "Direct computer control: type, click, hotkeys, scroll, move mouse, "
        "screenshots, find elements on screen, CLOSE or KILL an application, "
        "and MOVE / TILE windows with GNOME Tiling Shell or across monitors. "
        "Use action='kill_app' or action='close_app' with 'app_name' whenever "
        "the user asks to kill, close, quit or exit a program ('kill Spotify', "
        "'close Chrome') - both find the application in the process table and "
        "terminate/kill all processes belonging to that app. Always name the "
        "application in 'app_name'. "
        "To move or tile windows with Tiling Shell, use action='tile_window' "
        "(with direction='left'|'right'|'up'|'down'|'center'), "
        "action='span_window' (extend across tiles in direction='left'|'right'|'up'|'down'|'all'), "
        "action='span_all_tiles', action='center_window', action='untile_window', "
        "action='untile_all_windows', action='focus_tile', action='cycle_layout', "
        "or action='layout_editor'. To move a window to another monitor, use "
        "action='move_window_to_monitor' with 'monitor' (or 'screen') set to "
        "the monitor number (1, 2, 3, 4) or position (left, right, top, bottom, "
        "main), and optionally 'app_name' to focus that app first. "
        "To OPEN an application (including on a specific monitor) use the "
        "open_app tool instead. "
        "Read the status the tool returns: 'done' means it was confirmed, "
        "'unverified' means it was requested and could NOT be confirmed - say "
        "so rather than claiming success - and 'ask' means nothing was done "
        "and the question must go to the user first. "
        "MOUSE AND KEYBOARD RULES: you may click, move, drag, scroll, press "
        "keys or type ONLY when the user asked for it in what they just said "
        "('take the mouse', 'click ...', 'type ...', 'scroll down'). Never do "
        "it on your own initiative, and never to finish a task they did not "
        "ask you to drive. When asked to type, type exactly the words they "
        "dictated and do NOT press Enter afterwards unless they said to. When "
        "the user says stop, hands off, let go or release, call "
        "action='release' at once and do nothing else. With several screens, "
        "x and y are positions on the working screen: use action='screens' to "
        "list them and action='use_screen' with 'screen' to change it."
    ),
    # Used in place of `description` only when a small model is on a tool
    # budget - Gemini still gets the full text. See providers/schema.py.
    "brief": (
        "Controls the computer: type, click, hotkeys, scroll, screenshots, "
        "close_app / kill_app to kill all processes of a named application "
        "('app_name'), tile_window / span_window / untile_window / "
        "move_window_to_monitor to tile and move windows across monitors. "
        "Use open_app to open an app (with optional 'monitor')."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | close_app | kill_app | tile_window | span_window | span_all_tiles | center_window | untile_window | untile_all_windows | focus_tile | cycle_layout | layout_editor | move_window_to_monitor | screen_find | screen_click | screens | use_screen | release | random_data | user_data"
            },
            "app_name": {
                "type": "STRING",
                "description": (
                    "For close_app / kill_app: the application to close or "
                    "kill, named as the user said it. Also optional for "
                    "tile_window / move_window_to_monitor to focus that "
                    "application before moving/tiling its window."
                )
            },
            "confirmed": {
                "type": "BOOLEAN",
                "description": (
                    "For close_app / kill_app: set true only after the tool "
                    "asked which application was meant AND the user answered. "
                    "Pass their answer in 'app_name'."
                )
            },
            "text": {
                "type": "STRING",
                "description": "Text to type or paste"
            },
            "x": {
                "type": "INTEGER",
                "description": "X coordinate"
            },
            "y": {
                "type": "INTEGER",
                "description": "Y coordinate"
            },
            "keys": {
                "type": "STRING",
                "description": "Key combination e.g. 'ctrl+c'"
            },
            "key": {
                "type": "STRING",
                "description": "Single key e.g. 'enter'"
            },
            "direction": {
                "type": "STRING",
                "description": "For scroll, tile_window, span_window, focus_tile: up | down | left | right | center | all | next | prev"
            },
            "amount": {
                "type": "INTEGER",
                "description": "Scroll amount (default: 3), or number of tile steps for tile_window (default: 1)"
            },
            "seconds": {
                "type": "NUMBER",
                "description": "Seconds to wait"
            },
            "title": {
                "type": "STRING",
                "description": "Window title for focus_window"
            },
            "description": {
                "type": "STRING",
                "description": "Element description for screen_find/screen_click"
            },
            "type": {
                "type": "STRING",
                "description": "Data type for random_data"
            },
            "field": {
                "type": "STRING",
                "description": "Field for user_data: name|email|city"
            },
            "clear_first": {
                "type": "BOOLEAN",
                "description": "Clear field before typing (default: true)"
            },
            "path": {
                "type": "STRING",
                "description": "Save path for screenshot"
            },
            "screen": {
                "type": "STRING",
                "description": "For use_screen or move_window_to_monitor: monitor number (1, 2, 3, 4) or left | right | top | bottom | main | next"
            },
            "monitor": {
                "type": "STRING",
                "description": "Target monitor for move_window_to_monitor or tile_window: monitor number (1, 2, 3, 4) or left | right | top | bottom | center | main"
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": computer_control,
}
