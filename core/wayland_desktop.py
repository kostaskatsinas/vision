"""
core/wayland_desktop.py - see the screen, move the mouse and type on Wayland.

WHY THIS EXISTS
    On a Wayland session the usual tools do nothing: pyautogui cannot inject
    input, mss cannot capture, xdotool is ignored. Upstream therefore switches
    all three abilities off (core/platform_support.py). Wayland does have an
    official door for this, the xdg-desktop-portal RemoteDesktop and ScreenCast
    interfaces, and GNOME implements both. One approved session gives:

      - one PipeWire stream per monitor, to look at the screen,
      - NotifyPointerMotionAbsolute / NotifyPointerButton, to point and click,
      - NotifyKeyboardKeysym / NotifyKeyboardKeycode, to type.

    The user approves it once in a GNOME dialog. The portal then hands back a
    restore token, kept in ~/.config/vision/portal_token, so later sessions
    start without a dialog. While a session is open GNOME shows its orange
    screen-sharing indicator in the top bar, with a stop button.

HOW THE REST OF THE APP USES IT
    `install()` registers `pyautogui_shim()` as the `pyautogui` module, so
    every existing `pyautogui.click(...)` / `.write(...)` / `.screenshot()` in
    actions/ works unchanged. Screen capture callers use `screenshot()`.

COORDINATES
    Everything is relative to the WORKING MONITOR (see `use_monitor`), which is
    the largest screen unless another was chosen. `size()` and `screenshot()`
    describe that monitor and `click(x, y)` lands on it, so a position read off
    a screenshot is the position to click. A frame from a monitor's stream has
    exactly that monitor's logical size, already rotated upright.

MEASURED ON THE TARGET MACHINE (GNOME Shell 50.1, portal 1.21.1, 4 monitors)
    Clicks landed 0 px off on every monitor. Text matched exactly, Greek
    included, PROVIDED the layout holding the character is the active one:
    Mutter looks a keysym up in the active layout only and silently drops the
    rest. Hence the layout switching in `type_text`.

Needs: the `jeepney` package (pure Python D-Bus) and `gst-launch-1.0` with the
pipewire plugin, which GNOME desktops ship.
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import threading
import time
import types
import unicodedata
from pathlib import Path
from typing import Any, Optional

from core.lazyimport import installed

BUS = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
TOKEN_FILE = Path.home() / ".config" / "vision" / "portal_token"

KEYBOARD, POINTER = 1, 2
MONITOR = 1
CURSOR_HIDDEN, CURSOR_EMBEDDED = 1, 2
BTN = {"left": 0x110, "right": 0x111, "middle": 0x112}

# The session is closed after this long without use, which also removes
# GNOME's screen-sharing indicator. Reopening with the saved token takes about
# a tenth of a second, so nothing is lost by letting go.
IDLE_CLOSE_S = 60.0
# How long to wait for the user to answer GNOME's permission dialog.
DIALOG_TIMEOUT_S = 90.0

DEAD_ACUTE, DEAD_DIAERESIS = 0xFE51, 0xFE57
_COMBINING_ACUTE, _COMBINING_DIAERESIS = "́", "̈"

# evdev keycodes, by pyautogui key name. Keycodes are physical keys, so
# shortcuts work whichever layout is active.
_KEYCODES = {
    "esc": 1, "escape": 1, "backspace": 14, "tab": 15, "enter": 28, "return": 28,
    "ctrl": 29, "ctrlleft": 29, "control": 29, "ctrlright": 97,
    "shift": 42, "shiftleft": 42, "shiftright": 54,
    "alt": 56, "altleft": 56, "option": 56, "altright": 100,
    "win": 125, "winleft": 125, "super": 125, "meta": 125, "cmd": 125,
    "command": 125, "winright": 126,
    "space": 57, " ": 57, "capslock": 58,
    "home": 102, "up": 103, "pageup": 104, "pgup": 104, "left": 105,
    "right": 106, "end": 107, "down": 108, "pagedown": 109, "pgdn": 109,
    "insert": 110, "delete": 111, "del": 111,
    "printscreen": 99, "prtsc": 99, "prtscr": 99, "prntscrn": 99, "menu": 127, "apps": 127,
    "volumemute": 113, "volumedown": 114, "volumeup": 115,
    "nexttrack": 163, "playpause": 164, "prevtrack": 165,
    "-": 12, "=": 13, "[": 26, "]": 27, ";": 39, "'": 40, "`": 41,
    "\\": 43, ",": 51, ".": 52, "/": 53,
}
_KEYCODES.update({c: k for c, k in zip("qwertyuiop", range(16, 26))})
_KEYCODES.update({c: k for c, k in zip("asdfghjkl", range(30, 39))})
_KEYCODES.update({c: k for c, k in zip("zxcvbnm", range(44, 51))})
_KEYCODES.update({str(d): 1 + d for d in range(1, 10)})
_KEYCODES["0"] = 11
_KEYCODES.update({f"f{n}": 58 + n for n in range(1, 11)})
_KEYCODES.update({"f11": 87, "f12": 88})
KEY_LEFTMETA, KEY_SPACE = 125, 57


class DesktopError(RuntimeError):
    """A sentence the assistant can pass on to the user as it stands."""


# ── Is this the right machine? ───────────────────────────────────────────────

def is_wayland() -> bool:
    session = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    if session:
        return session == "wayland"
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def missing() -> list[str]:
    """What is needed and absent, as instructions. Empty when all is there."""
    out = []
    if not installed("jeepney"):
        out.append("the jeepney package (run: pip install jeepney)")
    if not shutil.which("gst-launch-1.0"):
        out.append("gst-launch-1.0 (run: sudo apt install gstreamer1.0-tools "
                   "gstreamer1.0-pipewire)")
    if not installed("PIL"):
        out.append("the pillow package (run: pip install pillow)")
    return out


def available() -> bool:
    """True when this is a Wayland session and the portal route can be used.
    Cheap: environment variables and lookups only, safe to call at startup."""
    if os.environ.get("VISION_NO_WAYLAND_DESKTOP"):
        return False
    return is_wayland() and not missing()


# ── Keyboard planning (pure functions, unit-tested) ──────────────────────────

def greek_keysym(cp: int) -> Optional[int]:
    """Legacy X11 keysym for a plain Greek letter, which is what the `gr`
    layout holds. Mutter matches keysyms by exact value, so the Unicode form
    (0x01000000 | codepoint) is not found; the old 0x7xx ones are."""
    if 0x391 <= cp <= 0x3A1:
        return 0x7C1 + cp - 0x391          # Α..Ρ
    if cp == 0x3A3:
        return 0x7D2                       # Σ
    if 0x3A4 <= cp <= 0x3A9:
        return 0x7D4 + cp - 0x3A4          # Τ..Ω
    if 0x3B1 <= cp <= 0x3C1:
        return 0x7E1 + cp - 0x3B1          # α..ρ
    if cp == 0x3C2:
        return 0x7F3                       # ς
    if cp == 0x3C3:
        return 0x7F2                       # σ
    if 0x3C4 <= cp <= 0x3C9:
        return 0x7F4 + cp - 0x3C4          # τ..ω
    return None


def plan_char(ch: str) -> tuple[Optional[str], list[int]]:
    """(layout that must be active or None for any, keysyms to press).

    Raises DesktopError for a character there is no key plan for."""
    cp = ord(ch)
    if ch in ("\n", "\r"):
        return None, [0xFF0D]
    if ch == "\t":
        return None, [0xFF09]
    if ch == " " or ("0" <= ch <= "9"):
        return None, [cp]                  # the same key in every layout
    if 0x20 <= cp <= 0x7E:
        return "latin", [cp]
    base = unicodedata.normalize("NFD", ch)
    greek = greek_keysym(ord(base[0]))
    if greek is not None:
        dead = []
        for mark in base[1:]:
            if mark == _COMBINING_ACUTE:
                dead.append(DEAD_ACUTE)
            elif mark == _COMBINING_DIAERESIS:
                dead.append(DEAD_DIAERESIS)
            else:
                raise DesktopError(f"there is no key for {ch!r}")
        return "gr", dead + [greek]
    raise DesktopError(f"there is no key for {ch!r}")


def plan_text(text: str) -> tuple[list[tuple[Optional[str], list[int]]], str]:
    """Plan every character; characters with no key are left out and returned
    as the second value so the caller can say which ones were skipped."""
    plan, skipped = [], ""
    for ch in text:
        try:
            plan.append(plan_char(ch))
        except DesktopError:
            skipped += ch
    return plan, skipped


def _gsettings_sources(key: str) -> list[str]:
    try:
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.input-sources", key],
            capture_output=True, text=True, timeout=3).stdout.strip()
        out = out.replace("@a(ss) ", "")
        return [pair[1] for pair in ast.literal_eval(out)]
    except Exception:
        return []


def configured_layouts() -> list[str]:
    return _gsettings_sources("sources")


def current_layout() -> Optional[str]:
    """The active GNOME input source: the first entry of mru-sources. That is
    GNOME behaviour, not an API, but it held on every run measured."""
    for key in ("mru-sources", "sources"):
        got = _gsettings_sources(key)
        if got:
            return got[0]
    return None


def _layout_for(kind: str, layouts: list[str]) -> Optional[str]:
    """Which configured layout serves 'latin' or 'gr' characters."""
    if kind == "gr":
        return next((name for name in layouts if name.split("+")[0] == "gr"), None)
    non_latin = {"gr", "ru", "ua", "bg", "rs", "il", "ara", "ir", "th", "kr",
                 "jp", "cn", "in", "am", "ge", "by", "kz", "mk"}
    return next((name for name in layouts
                 if name.split("+")[0] not in non_latin), None)


# ── The portal session ───────────────────────────────────────────────────────

class _Portal:
    """One RemoteDesktop session with a ScreenCast attached. Blocking D-Bus;
    callers hold `_lock`."""

    def __init__(self) -> None:
        from jeepney import DBusAddress
        from jeepney.io.blocking import open_dbus_connection
        self._rd = DBusAddress(PATH, bus_name=BUS,
                               interface="org.freedesktop.portal.RemoteDesktop")
        self._sc = DBusAddress(PATH, bus_name=BUS,
                               interface="org.freedesktop.portal.ScreenCast")
        self.conn = open_dbus_connection("SESSION", enable_fds=True)
        self.sender = self.conn.unique_name[1:].replace(".", "_")
        self._n = 0
        self.session: Optional[str] = None
        self.streams: list[dict] = []

    def _call(self, addr, method, sig, body, timeout=10):
        from jeepney import MessageType, new_method_call
        reply = self.conn.send_and_get_reply(
            new_method_call(addr, method, sig, body), timeout=timeout)
        if reply.header.message_type == MessageType.error:
            raise DesktopError(f"the desktop portal refused {method}: {reply.body}")
        return reply.body

    def _request(self, addr, method, sig, args_before, options, timeout=30):
        """Call a portal method that answers through a Request's Response signal."""
        from jeepney import MatchRule, message_bus
        self._n += 1
        token = f"vision{os.getpid()}_{self._n}"
        req_path = f"{PATH}/request/{self.sender}/{token}"
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request",
                         member="Response", path=req_path)
        self._call(message_bus, "AddMatch", "s", (rule.serialise(),))
        options = dict(options, handle_token=("s", token))
        try:
            with self.conn.filter(rule) as queue:
                self._call(addr, method, sig, tuple(args_before) + (options,))
                msg = self.conn.recv_until_filtered(queue, timeout=timeout)
        finally:
            try:
                self._call(message_bus, "RemoveMatch", "s", (rule.serialise(),))
            except Exception:
                pass
        code, results = msg.body
        if code == 1:
            raise DesktopError("the permission dialog was cancelled, so screen "
                               "and input control is not available")
        if code != 0:
            raise DesktopError(f"the desktop portal ended {method} with code {code}")
        return {k: v[1] for k, v in results.items()}

    def open(self) -> None:
        res = self._request(self._rd, "CreateSession", "a{sv}", (),
                            {"session_handle_token": ("s", f"visionsess{os.getpid()}_{self._n}")})
        self.session = res["session_handle"]
        dev_opts = {"types": ("u", KEYBOARD | POINTER), "persist_mode": ("u", 2)}
        saved = ""
        try:
            saved = TOKEN_FILE.read_text().strip()
        except OSError:
            pass
        if saved:
            dev_opts["restore_token"] = ("s", saved)
        self._request(self._rd, "SelectDevices", "oa{sv}", (self.session,), dev_opts)
        self._request(self._sc, "SelectSources", "oa{sv}", (self.session,),
                      {"types": ("u", MONITOR), "multiple": ("b", True),
                       "cursor_mode": ("u", CURSOR_EMBEDDED)})
        try:
            res = self._request(self._rd, "Start", "osa{sv}", (self.session, ""), {},
                                timeout=DIALOG_TIMEOUT_S)
        except TimeoutError:
            raise DesktopError(
                "GNOME is asking for permission to share the screen and allow "
                "remote control, and nobody answered the dialog in time - ask "
                "the user to click Share on it and try again")
        self.streams = []
        for node, props in res.get("streams", []):
            p = {k: v[1] for k, v in props.items()}
            pos, size = p.get("position"), p.get("size")
            if not pos or not size:
                continue
            self.streams.append({"node": int(node), "left": int(pos[0]), "top": int(pos[1]),
                                 "width": int(size[0]), "height": int(size[1])})
        if not self.streams:
            raise DesktopError("the permission dialog was answered without "
                               "sharing a monitor, so there is nothing to see or control")
        token = res.get("restore_token")
        if token:
            try:
                TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
                TOKEN_FILE.write_text(token)
                TOKEN_FILE.chmod(0o600)
            except OSError:
                pass

    def close(self) -> None:
        from jeepney import DBusAddress
        try:
            if self.session:
                addr = DBusAddress(self.session, bus_name=BUS,
                                   interface="org.freedesktop.portal.Session")
                self._call(addr, "Close", "", (), timeout=3)
        except Exception:
            pass
        finally:
            self.session = None
            try:
                self.conn.close()
            except Exception:
                pass

    # input
    def move(self, node: int, x: float, y: float) -> None:
        self._call(self._rd, "NotifyPointerMotionAbsolute", "oa{sv}udd",
                   (self.session, {}, node, float(x), float(y)))

    def button(self, code: int, pressed: bool) -> None:
        self._call(self._rd, "NotifyPointerButton", "oa{sv}iu",
                   (self.session, {}, code, 1 if pressed else 0))

    def axis(self, axis: int, steps: int) -> None:
        self._call(self._rd, "NotifyPointerAxisDiscrete", "oa{sv}ui",
                   (self.session, {}, axis, int(steps)))

    def keysym(self, sym: int, pressed: bool) -> None:
        self._call(self._rd, "NotifyKeyboardKeysym", "oa{sv}iu",
                   (self.session, {}, sym, 1 if pressed else 0))

    def keycode(self, code: int, pressed: bool) -> None:
        self._call(self._rd, "NotifyKeyboardKeycode", "oa{sv}iu",
                   (self.session, {}, code, 1 if pressed else 0))

    def pipewire_fd(self) -> int:
        fd_obj = self._call(self._sc, "OpenPipeWireRemote", "oa{sv}",
                            (self.session, {}))[0]
        return fd_obj.to_raw_fd()


_lock = threading.RLock()
_portal: Optional[_Portal] = None
_idle_timer: Optional[threading.Timer] = None
_monitor_key: Optional[tuple[int, int]] = None      # (left, top) of the working monitor
_last_pos: Optional[tuple[int, int]] = None         # last pointer position we set
_factory = _Portal                                  # replaced by tests


def _touch() -> None:
    """Restart the idle countdown."""
    global _idle_timer
    if _idle_timer is not None:
        _idle_timer.cancel()
    _idle_timer = threading.Timer(IDLE_CLOSE_S, release)
    _idle_timer.daemon = True
    _idle_timer.start()


def _session() -> _Portal:
    """The open session, opening one if needed. Call with `_lock` held."""
    global _portal
    if _portal is None:
        gaps = missing()
        if gaps:
            raise DesktopError("screen and input control on Wayland needs "
                               + " and ".join(gaps))
        portal = _factory()
        try:
            portal.open()
        except DesktopError:
            portal.close()
            raise
        except Exception as e:
            portal.close()
            raise DesktopError(f"could not open a desktop session through the "
                               f"portal: {e}")
        _portal = portal
    _touch()
    return _portal


def _run(fn):
    """Run `fn(portal)` on the session. A session that has gone away (the user
    pressed GNOME's stop button, or the screen locked) is dropped, so the next
    request starts a fresh one instead of failing forever."""
    with _lock:
        portal = _session()
        try:
            return fn(portal)
        except DesktopError:
            _drop()
            raise
        except Exception as e:
            _drop()
            raise DesktopError(
                f"the desktop session ended ({type(e).__name__}: {e}) - screen "
                f"sharing may have been stopped from the top bar")


def _drop() -> None:
    global _portal
    if _portal is not None:
        _portal.close()
        _portal = None


def release() -> str:
    """Let go of the screen, mouse and keyboard. GNOME's indicator disappears."""
    global _idle_timer
    with _lock:
        if _idle_timer is not None:
            _idle_timer.cancel()
            _idle_timer = None
        was_open = _portal is not None
        _drop()
    return "released" if was_open else "nothing was being controlled"


def is_active() -> bool:
    return _portal is not None


# ── Monitors ─────────────────────────────────────────────────────────────────

def monitors() -> list[dict]:
    """Every shared monitor, left to right then top to bottom, numbered from 1.
    Identified by position: PipeWire node ids change with every session."""
    streams = _run(lambda p: list(p.streams))
    ordered = sorted(streams, key=lambda s: (s["left"], s["top"]))
    return [dict(s, number=i + 1) for i, s in enumerate(ordered)]


def _working(mons: list[dict]) -> dict:
    if _monitor_key is not None:
        for m in mons:
            if (m["left"], m["top"]) == _monitor_key:
                return m
    return max(mons, key=lambda m: m["width"] * m["height"])


def working_monitor() -> dict:
    return _working(monitors())


def use_monitor(which: Any) -> dict:
    """Choose the working monitor: a number from `monitors()`, or one of
    left / right / top / bottom / main / largest / next."""
    global _monitor_key, _last_pos
    mons = monitors()
    word = str(which).strip().lower()
    chosen = None
    if word.isdigit():
        chosen = next((m for m in mons if m["number"] == int(word)), None)
    elif word in ("main", "primary", "largest", "biggest", "default"):
        chosen = max(mons, key=lambda m: m["width"] * m["height"])
    elif word == "left":
        chosen = min(mons, key=lambda m: m["left"])
    elif word == "right":
        chosen = max(mons, key=lambda m: m["left"] + m["width"])
    elif word in ("top", "upper", "up"):
        chosen = min(mons, key=lambda m: m["top"])
    elif word in ("bottom", "lower", "down"):
        chosen = max(mons, key=lambda m: m["top"] + m["height"])
    elif word == "next":
        current = _working(mons)
        chosen = mons[current["number"] % len(mons)]
    if chosen is None:
        raise DesktopError(
            f"there is no screen '{which}' - choose a number from 1 to "
            f"{len(mons)}, or left, right, top, bottom or main")
    _monitor_key = (chosen["left"], chosen["top"])
    _last_pos = None
    return chosen


def describe_monitors() -> str:
    mons = monitors()
    current = _working(mons)
    parts = []
    for m in mons:
        mark = " (in use)" if m is current or m["number"] == current["number"] else ""
        parts.append(f"screen {m['number']}: {m['width']}x{m['height']} at "
                     f"{m['left']},{m['top']}{mark}")
    return "; ".join(parts)


# ── Seeing ───────────────────────────────────────────────────────────────────

def _grab(portal: _Portal, mon: dict):
    """One frame of one monitor as a PIL image."""
    import PIL.Image
    fd = portal.pipewire_fd()
    w, h = mon["width"], mon["height"]
    try:
        cmd = ["gst-launch-1.0", "-q", "pipewiresrc", f"fd={fd}",
               f"path={mon['node']}", "num-buffers=1", "!", "videoconvert", "!",
               "video/x-raw,format=RGBx", "!", "fdsink", "fd=1"]
        proc = subprocess.run(cmd, pass_fds=(fd,), capture_output=True, timeout=10)
    finally:
        os.close(fd)
    data = proc.stdout
    if proc.returncode != 0 or len(data) < w * h * 4:
        err = proc.stderr.decode("utf-8", "replace").strip()[-300:]
        raise DesktopError(f"could not capture the screen "
                           f"(got {len(data)} of {w * h * 4} bytes) {err}")
    # frombytes, not frombuffer: frombuffer maps the bytes and hands back a
    # read-only image in mode RGBX, which cannot be saved as JPEG or PNG.
    return PIL.Image.frombytes("RGB", (w, h), data[: w * h * 4], "raw", "RGBX")


def screenshot(region: Optional[tuple[int, int, int, int]] = None):
    """The working monitor as a PIL image. `region` is (left, top, width,
    height) on that monitor."""
    def work(portal):
        return _grab(portal, _working(_ordered(portal)))
    image = _run(work)
    if region:
        left, top, width, height = (int(v) for v in region)
        image = image.crop((left, top, left + width, top + height))
    return image


def _ordered(portal: _Portal) -> list[dict]:
    ordered = sorted(portal.streams, key=lambda s: (s["left"], s["top"]))
    return [dict(s, number=i + 1) for i, s in enumerate(ordered)]


def size() -> tuple[int, int]:
    mon = working_monitor()
    return mon["width"], mon["height"]


# ── Pointing ─────────────────────────────────────────────────────────────────

def _clamp(mon: dict, x: Any, y: Any) -> tuple[int, int]:
    x = max(0, min(mon["width"] - 1, int(round(float(x)))))
    y = max(0, min(mon["height"] - 1, int(round(float(y)))))
    return x, y


def move_to(x: Any, y: Any) -> tuple[int, int]:
    """Pointer to (x, y) on the working monitor."""
    global _last_pos

    def work(portal):
        mon = _working(_ordered(portal))
        px, py = _clamp(mon, x, y)
        portal.move(mon["node"], px, py)
        return px, py
    _last_pos = _run(work)
    return _last_pos


def position() -> tuple[int, int]:
    """Where we last put the pointer. Wayland does not let a program read the
    real position, so after the user moves the mouse this is out of date."""
    if _last_pos is not None:
        return _last_pos
    w, h = size()
    return w // 2, h // 2


def click(x: Any = None, y: Any = None, button: str = "left", clicks: int = 1,
          interval: float = 0.08) -> None:
    """Click on the working monitor; without coordinates, where the pointer is.

    With coordinates the pointer is put there again immediately before the
    button goes down: a hand on the real mouse moves the same pointer, and the
    gap between arriving and pressing is where a click goes astray."""
    code = BTN.get(str(button).lower(), BTN["left"])

    def work(portal):
        global _last_pos
        if x is not None and y is not None:
            mon = _working(_ordered(portal))
            px, py = _clamp(mon, x, y)
            portal.move(mon["node"], px, py)
            time.sleep(0.03)
            portal.move(mon["node"], px, py)
            _last_pos = (px, py)
        for i in range(max(1, int(clicks))):
            portal.button(code, True)
            time.sleep(0.04)
            portal.button(code, False)
            if i + 1 < clicks:
                time.sleep(max(0.02, float(interval)))
    _run(work)


def drag_to(x: Any, y: Any, duration: float = 0.4, button: str = "left") -> None:
    """Press where the pointer is, glide to (x, y), release."""
    code = BTN.get(str(button).lower(), BTN["left"])
    start = position()

    def work(portal):
        global _last_pos
        mon = _working(_ordered(portal))
        ex, ey = _clamp(mon, x, y)
        portal.move(mon["node"], *start)
        time.sleep(0.03)
        portal.button(code, True)
        try:
            steps = 12
            for i in range(1, steps + 1):
                portal.move(mon["node"],
                            start[0] + (ex - start[0]) * i / steps,
                            start[1] + (ey - start[1]) * i / steps)
                time.sleep(max(0.005, float(duration) / steps))
        finally:
            portal.button(code, False)
        _last_pos = (ex, ey)
    _run(work)


def scroll(clicks: int, horizontal: bool = False) -> None:
    """Positive scrolls up (or right), as pyautogui does. The portal counts the
    other way on the vertical axis."""
    steps = int(clicks)
    if not steps:
        return
    _run(lambda p: p.axis(1 if horizontal else 0, steps if horizontal else -steps))


# ── Typing ───────────────────────────────────────────────────────────────────

def _keycode_for(name: str) -> int:
    key = str(name).strip().lower() if len(str(name)) > 1 else str(name).lower()
    code = _KEYCODES.get(key)
    if code is None:
        raise DesktopError(f"there is no key called '{name}'")
    return code


def key_down(name: str) -> None:
    code = _keycode_for(name)
    _run(lambda p: p.keycode(code, True))


def key_up(name: str) -> None:
    code = _keycode_for(name)
    _run(lambda p: p.keycode(code, False))


def press(name: str, presses: int = 1, interval: float = 0.03) -> None:
    code = _keycode_for(name)

    def work(portal):
        for _ in range(max(1, int(presses))):
            portal.keycode(code, True)
            time.sleep(0.015)
            portal.keycode(code, False)
            time.sleep(max(0.015, float(interval)))
    _run(work)


def hotkey(*names: str) -> None:
    codes = [_keycode_for(n) for n in names]

    def work(portal):
        down = []
        try:
            for code in codes:
                portal.keycode(code, True)
                down.append(code)
                time.sleep(0.03)
        finally:
            for code in reversed(down):
                portal.keycode(code, False)
                time.sleep(0.03)
    _run(work)


def _switch_layout(portal: _Portal, want: str, tries: int = 4) -> bool:
    """Press Super+Space until GNOME's active input source is `want`."""
    for _ in range(tries):
        if current_layout() == want:
            return True
        for code in (KEY_LEFTMETA, KEY_SPACE):
            portal.keycode(code, True)
            time.sleep(0.03)
        for code in (KEY_SPACE, KEY_LEFTMETA):
            portal.keycode(code, False)
            time.sleep(0.03)
        for _ in range(20):                 # mru-sources updates a moment later
            time.sleep(0.05)
            if current_layout() == want:
                return True
    return current_layout() == want


def type_text(text: str, interval: float = 0.012) -> str:
    """Type `text` into whatever has the keyboard. Returns '' or a note about
    characters that could not be typed.

    A keysym only registers if the ACTIVE layout holds it, so the layout is
    switched (Super+Space) when a character needs another one, and put back at
    the end."""
    plan, skipped = plan_text(str(text))
    layouts = configured_layouts()
    gap = max(0.008, min(0.05, float(interval or 0.012)))

    def work(portal):
        start = current_layout()
        active = start
        try:
            for kind, syms in plan:
                if kind:
                    want = _layout_for(kind, layouts)
                    if want is None:
                        name = "Greek" if kind == "gr" else "Latin"
                        raise DesktopError(
                            f"no {name} keyboard layout is set up in GNOME, so "
                            f"those letters cannot be typed")
                    if want != active:
                        if not _switch_layout(portal, want):
                            raise DesktopError(
                                f"could not switch the keyboard layout to {want}")
                        active = want
                        time.sleep(0.15)    # let applications receive the keymap
                for sym in syms:
                    portal.keysym(sym, True)
                    time.sleep(gap)
                    portal.keysym(sym, False)
                    time.sleep(gap)
        finally:
            if start and active != start:
                try:
                    _switch_layout(portal, start)
                except Exception:
                    pass
    _run(work)
    if skipped:
        shown = "".join(dict.fromkeys(skipped))[:20]
        return f"these characters have no key and were left out: {shown}"
    return ""


# ── pyautogui stand-in ───────────────────────────────────────────────────────

def pyautogui_shim() -> types.ModuleType:
    """A module with the part of pyautogui's interface this app uses, backed by
    the portal. Coordinates are on the working monitor."""
    import importlib.machinery
    mod = types.ModuleType("pyautogui")
    # `installed("pyautogui")` asks find_spec, which raises on a module in
    # sys.modules that has no spec.
    mod.__spec__ = importlib.machinery.ModuleSpec("pyautogui", None)
    mod.__doc__ = "Vision's Wayland stand-in for pyautogui (core/wayland_desktop.py)."
    mod.FAILSAFE = False
    mod.PAUSE = 0.05
    mod.VISION_WAYLAND = True

    class FailSafeException(Exception):
        pass

    def _pause():
        try:
            time.sleep(max(0.0, min(0.5, float(mod.PAUSE))))
        except (TypeError, ValueError):
            pass

    def moveTo(x=None, y=None, duration=0.0, *a, **kw):
        if x is None or y is None:
            return
        if isinstance(x, (tuple, list)):
            x, y = x[0], x[1]
        move_to(x, y)
        _pause()

    def moveRel(xOffset=0, yOffset=0, duration=0.0, *a, **kw):
        px, py = position()
        move_to(px + (xOffset or 0), py + (yOffset or 0))
        _pause()

    def _click(x=None, y=None, clicks=1, interval=0.0, button="left", *a, **kw):
        if isinstance(x, (tuple, list)):
            x, y = x[0], x[1]
        click(x, y, button=button, clicks=clicks, interval=interval or 0.08)
        _pause()

    def doubleClick(x=None, y=None, interval=0.0, button="left", *a, **kw):
        _click(x, y, clicks=2, interval=interval, button=button)

    def rightClick(x=None, y=None, *a, **kw):
        _click(x, y, button="right")

    def middleClick(x=None, y=None, *a, **kw):
        _click(x, y, button="middle")

    def mouseDown(x=None, y=None, button="left", *a, **kw):
        if x is not None and y is not None:
            move_to(x, y)
        code = BTN.get(str(button).lower(), BTN["left"])
        _run(lambda p: p.button(code, True))

    def mouseUp(x=None, y=None, button="left", *a, **kw):
        if x is not None and y is not None:
            move_to(x, y)
        code = BTN.get(str(button).lower(), BTN["left"])
        _run(lambda p: p.button(code, False))

    def dragTo(x=None, y=None, duration=0.4, button="left", *a, **kw):
        drag_to(x, y, duration=duration or 0.4, button=button)
        _pause()

    def dragRel(xOffset=0, yOffset=0, duration=0.4, button="left", *a, **kw):
        px, py = position()
        dragTo(px + xOffset, py + yOffset, duration=duration, button=button)

    def _scroll(clicks, x=None, y=None, *a, **kw):
        if x is not None and y is not None:
            move_to(x, y)
        scroll(clicks)
        _pause()

    def hscroll(clicks, x=None, y=None, *a, **kw):
        if x is not None and y is not None:
            move_to(x, y)
        scroll(clicks, horizontal=True)
        _pause()

    def _press(keys, presses=1, interval=0.0, *a, **kw):
        for key in ([keys] if isinstance(keys, str) else list(keys)):
            press(key, presses=presses, interval=interval or 0.03)
        _pause()

    def _hotkey(*keys, **kw):
        if len(keys) == 1 and isinstance(keys[0], (list, tuple)):
            keys = tuple(keys[0])
        hotkey(*keys)
        _pause()

    def write(message, interval=0.0, *a, **kw):
        if isinstance(message, (list, tuple)):      # a list of key names
            for key in message:
                press(key)
        else:
            note = type_text(str(message), interval=interval or 0.012)
            if note:
                print(f"[Wayland] ⚠️ {note}")
        _pause()

    def _screenshot(imageFilename=None, region=None, *a, **kw):
        image = screenshot(region=region)
        if imageFilename:
            image.save(str(imageFilename))
        return image

    def onScreen(x, y=None):
        if isinstance(x, (tuple, list)):
            x, y = x[0], x[1]
        w, h = size()
        return 0 <= x < w and 0 <= y < h

    mod.FailSafeException = FailSafeException
    mod.moveTo, mod.moveRel, mod.move = moveTo, moveRel, moveRel
    mod.click, mod.doubleClick = _click, doubleClick
    mod.rightClick, mod.middleClick = rightClick, middleClick
    mod.mouseDown, mod.mouseUp = mouseDown, mouseUp
    mod.dragTo, mod.dragRel, mod.drag = dragTo, dragRel, dragRel
    mod.scroll, mod.vscroll, mod.hscroll = _scroll, _scroll, hscroll
    mod.press, mod.hotkey = _press, _hotkey
    mod.keyDown, mod.keyUp = key_down, key_up
    mod.write, mod.typewrite = write, write
    mod.screenshot = _screenshot
    mod.size, mod.position, mod.onScreen = size, position, onScreen
    mod.KEYBOARD_KEYS = sorted(_KEYCODES)
    return mod


def install() -> bool:
    """On a capable Wayland session, make `import pyautogui` give the stand-in
    everywhere. Returns whether it did. Call once, before actions are loaded."""
    import sys
    if not available():
        return False
    if not getattr(sys.modules.get("pyautogui"), "VISION_WAYLAND", False):
        sys.modules["pyautogui"] = pyautogui_shim()
    return True
