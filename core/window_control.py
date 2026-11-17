"""
core/window_control.py - minimize, maximize or restore ONE named app's window.

WHY THIS EXISTS
    "Minimize Spotify" used to press a key chord, or run `wmctrl -r :ACTIVE:`,
    and both act on whatever window happens to have focus. On GNOME Wayland it
    was worse than that: wmctrl only sees XWayland windows, so ":ACTIVE:" meant
    "the last X11 window that had focus" - often Chrome or Steam while a native
    Wayland app was in front. The user named one app and another one changed.

    No outside program may touch another app's window on Wayland, so the
    acting is done inside GNOME Shell by a tiny extension shipped with Vision
    (gnome-extension/vision-windows@vision). This module asks it for the window
    list, picks the windows belonging to the app that was NAMED, and asks it to
    act on those window ids. Focus never decides which window changes.

    `./vision.sh` copies the extension into place and enables it. GNOME on
    Wayland only discovers a NEW extension at login, so the very first time it
    needs one log out and back in; until then every call here says so instead
    of falling back to a guess.

    On an X11 session the same requests go through xdotool / wmctrl by window
    id, found by the app's process ids - never ":ACTIVE:".
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from core.lazyimport import installed

EXT_UUID = "vision-windows@vision"
BUS_NAME = "org.gnome.Shell"
OBJECT_PATH = "/org/gnome/Shell/Extensions/VisionWindows"
INTERFACE = "org.gnome.Shell.Extensions.VisionWindows"

ACTIONS = ("minimize", "maximize", "unmaximize", "restore", "activate")

# Words that are not part of an app's name. The model normally passes a clean
# name, but "the spotify window" should still find Spotify.
_FILLER = {"the", "app", "application", "window", "windows", "program", "my"}

NEEDS_LOGIN = (
    "Vision's window helper for GNOME is installed but not running yet. Log "
    "out and back in once so GNOME loads it, then ask again"
)
NOT_INSTALLED = (
    "Vision's window helper for GNOME is not installed. Start Vision with "
    "./vision.sh (it installs it), then log out and back in once"
)


class WindowError(RuntimeError):
    """A sentence the assistant can say as it is."""


class HelperMissing(WindowError):
    """The GNOME extension is not running, so no window was looked at."""


# ── Talking to the extension ─────────────────────────────────────────────────

def _call_jeepney(method: str, signature: str = "", body: tuple = ()) -> tuple:
    from jeepney import DBusAddress, new_method_call
    from jeepney.io.blocking import open_dbus_connection
    addr = DBusAddress(OBJECT_PATH, bus_name=BUS_NAME, interface=INTERFACE)
    conn = open_dbus_connection("SESSION")
    try:
        msg = new_method_call(addr, method, signature, body)
        reply = conn.send_and_get_reply(msg, timeout=3.0)
    finally:
        conn.close()
    # jeepney hands an error reply back as a message whose body is the text.
    if getattr(reply.header, "message_type", None) is not None and \
            getattr(reply.header.message_type, "name", "") == "error":
        raise WindowError(" ".join(str(b) for b in reply.body) or "D-Bus error")
    return tuple(reply.body)


def _call_gdbus(method: str, args: list[str]) -> str:
    r = subprocess.run(
        ["gdbus", "call", "--session", "--dest", BUS_NAME,
         "--object-path", OBJECT_PATH, "--method", f"{INTERFACE}.{method}", *args],
        capture_output=True, text=True, timeout=3.0)
    if r.returncode != 0:
        raise WindowError(r.stderr.strip() or "gdbus call failed")
    return r.stdout.strip()


def _default_call(method: str, *args: Any) -> Any:
    """Call the extension. -> the single return value. Raises WindowError."""
    if installed("jeepney"):
        try:
            if method == "Act":
                return _call_jeepney("Act", "ts", (int(args[0]), str(args[1])))[0]
            return _call_jeepney(method)[0]
        except WindowError:
            raise
        except Exception:
            pass        # no bus library, odd bus: try the command line tool
    if not shutil.which("gdbus"):
        raise WindowError("neither jeepney nor gdbus can reach the session bus")
    if method == "Act":
        out = _call_gdbus("Act", [str(int(args[0])), str(args[1])])
        return "true" in out
    out = _call_gdbus(method, [])
    # gdbus prints a GVariant tuple: ('[{"id": ...}]',)
    try:
        return ast.literal_eval(out)[0]
    except Exception as exc:
        raise WindowError(f"could not read the window list ({exc})") from exc


# Replaced by tests. Takes (method, *args) like _default_call.
_call: Callable[..., Any] = _default_call


def extension_installed() -> bool:
    return (_ext_root() / EXT_UUID / "extension.js").exists()


def helper_running() -> bool:
    try:
        _call("List")
        return True
    except Exception:
        return False


def list_windows() -> list[dict]:
    """Every normal window GNOME has, as the extension describes it."""
    try:
        raw = _call("List")
    except WindowError:
        raise
    except Exception as exc:
        raise WindowError(str(exc)) from exc
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise WindowError(f"the window list was not readable ({exc})") from exc
    return [w for w in data if isinstance(w, dict) and "id" in w]


def _act(window_id: int, what: str) -> bool:
    try:
        return bool(_call("Act", int(window_id), what))
    except Exception:
        return False


# ── Which windows belong to the app that was named ───────────────────────────

def _fold(text: str) -> str:
    from core.appidentity import fold
    return fold(text)


def _tokens(text: str) -> list[str]:
    return [t for t in (_fold(p) for p in re.split(r"[^\w]+", str(text or ""))) if t]


def _name_fields(win: dict) -> list[str]:
    fields = []
    for key in ("app_name", "app_id", "wm_class", "wm_class_instance",
                "gtk_app_id", "sandboxed_app_id"):
        value = str(win.get(key) or "").strip()
        if not value:
            continue
        if value.endswith(".desktop"):
            value = value[: -len(".desktop")]
        fields.append(value)
        # org.gnome.Nautilus, com.spotify.Client: the last dotted part is the
        # name people say.
        if "." in value:
            fields.append(value.rsplit(".", 1)[-1])
    return fields


def _queries(app_name: str) -> list[str]:
    from core.appidentity import spelling_variants
    words = [w for w in re.split(r"\s+", app_name.strip()) if w.lower() not in _FILLER]
    cleaned = " ".join(words) or app_name
    out: list[str] = []
    for q in spelling_variants(cleaned) + spelling_variants(app_name):
        if q and q not in out:
            out.append(q)
    return out


def _name_score(win: dict, queries: list[str]) -> int:
    best = 0
    for field in _name_fields(win):
        whole = _fold(field)
        toks = _tokens(field)
        for q in queries:
            if whole == q:
                best = max(best, 100)
            elif q in toks:
                best = max(best, 90)
            elif len(q) >= 4 and whole.startswith(q):
                best = max(best, 70)
    return best


def match_windows(app_name: str, windows: list[dict],
                  app_pids: Optional[set[int]] = None,
                  own_pid: Optional[int] = None) -> list[dict]:
    """The windows of `app_name`, best first. Never the assistant's own window.

    Names decide first: the app's name as GNOME knows it, its desktop id and
    its window class. Process ids (from the process table) only count when no
    window matched by name, because one process can own windows that people
    think of as different apps. Window TITLES never count: a browser tab
    titled "Spotify - Web Player" is not the Spotify app.
    """
    own_pid = os.getpid() if own_pid is None else own_pid
    queries = _queries(app_name)
    if not queries:
        return []
    scored = []
    for win in windows:
        if int(win.get("pid") or -1) == own_pid:
            continue
        score = _name_score(win, queries)
        if not score and app_pids and int(win.get("pid") or -1) in app_pids:
            score = 60
        if score:
            scored.append((score, win))
    if not scored:
        return []
    top = max(s for s, _ in scored)
    best = [w for s, w in scored if s == top]
    # The window in front first, then the most recently used.
    best.sort(key=lambda w: (not w.get("focused"), -int(w.get("user_time") or 0)))
    return best


def _app_pids(app_name: str) -> set[int]:
    try:
        from core import appidentity
        return {m.pid for m in appidentity.running(app_name)}
    except Exception:
        return set()


def _label(win: dict) -> str:
    return (win.get("app_name") or win.get("wm_class") or win.get("title")
            or "the window")


# ── The actions ──────────────────────────────────────────────────────────────

def _targets(what: str, wins: list[dict]) -> list[dict]:
    """Minimize takes every window of the app; the rest take one window."""
    if what == "minimize":
        return [w for w in wins if not w.get("minimized")] or wins[:1]
    if what == "restore":
        hidden = [w for w in wins if w.get("minimized")]
        return (hidden or wins)[:1]
    return wins[:1]


def _done(what: str, win: dict) -> Optional[bool]:
    if what == "minimize":
        return bool(win.get("minimized"))
    if what == "maximize":
        return bool(win.get("maximized")) and not win.get("minimized")
    if what == "unmaximize":
        return not win.get("maximized") and not win.get("minimized")
    if what == "restore":
        return not win.get("minimized")
    return None


def _verify(what: str, ids: list[int]) -> bool:
    try:
        now = {int(w["id"]): w for w in list_windows()}
    except Exception:
        return False
    return all(_done(what, now[i]) for i in ids if i in now) and \
        all(i in now for i in ids)


def _require_helper() -> None:
    if helper_running():
        return
    raise HelperMissing(NEEDS_LOGIN if extension_installed() else NOT_INSTALLED)


def act_on_app(app_name: str, what: str) -> dict:
    """Minimize / maximize / unmaximize / restore / activate the named app.

    -> {"app", "windows", "verified"}. Raises WindowError with a sentence when
    nothing was changed. Nothing else on screen is touched.
    """
    what = (what or "").strip().lower()
    if what not in ACTIONS:
        raise WindowError(f"unknown window action '{what}'")
    app_name = (app_name or "").strip()
    if not app_name:
        raise WindowError("no application was named")

    if not _on_wayland():
        return _x11_act_on_app(app_name, what)

    _require_helper()
    windows = list_windows()
    wins = match_windows(app_name, windows, _app_pids(app_name))
    if not wins:
        raise WindowError(f"no open window belongs to '{app_name}'")
    targets = _targets(what, wins)
    acted = [int(w["id"]) for w in targets if _act(int(w["id"]), what)]
    if not acted:
        raise WindowError(f"GNOME refused to {what} {_label(targets[0])}")
    return {"app": _label(targets[0]), "windows": len(acted),
            "verified": _verify(what, acted)}


def act_on_focused(what: str) -> dict:
    """The same, for "minimize this" with no app named: the window in front,
    unless that is the assistant's own window."""
    what = (what or "").strip().lower()
    if what not in ACTIONS:
        raise WindowError(f"unknown window action '{what}'")
    if not _on_wayland():
        raise HelperMissing("no window helper is needed on X11")
    _require_helper()
    front = [w for w in list_windows() if w.get("focused")]
    if not front:
        raise WindowError("no window has focus - say which application")
    win = front[0]
    if int(win.get("pid") or -1) == os.getpid():
        raise WindowError("the window in front is Vision itself - say which "
                          "application's window to change")
    if not _act(int(win["id"]), what):
        raise WindowError(f"GNOME refused to {what} {_label(win)}")
    return {"app": _label(win), "windows": 1,
            "verified": _verify(what, [int(win["id"])])}


def activate_app(app_name: str) -> Optional[str]:
    """Raise the named app's window. -> its label, or None when the helper is
    not running or no window matched (callers then try their other ways)."""
    if not _on_wayland() or not helper_running():
        return None
    try:
        wins = match_windows(app_name, list_windows(), _app_pids(app_name))
    except Exception:
        return None
    if wins and _act(int(wins[0]["id"]), "activate"):
        return _label(wins[0])
    return None


def _on_wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or \
        bool(os.environ.get("WAYLAND_DISPLAY"))


# ── X11: by window id, found from the app's process ids ──────────────────────

def _x11_act_on_app(app_name: str, what: str) -> dict:
    pids = _app_pids(app_name)
    if not pids:
        raise WindowError(f"no running application matches '{app_name}'")
    ids: list[str] = []
    if shutil.which("xdotool"):
        for pid in sorted(pids):
            # A minimized (iconified) window is not "visible", and restore
            # exists exactly to reach those.
            search = ["xdotool", "search", "--pid", str(pid)]
            if what != "restore":
                search[2:2] = ["--onlyvisible"]
            r = subprocess.run(search, capture_output=True, text=True, timeout=5)
            ids += [i for i in r.stdout.split() if i not in ids]
    if not ids:
        raise WindowError(f"'{app_name}' is running but no window of it was found "
                          "(needs xdotool)")
    targets = ids if what == "minimize" else ids[-1:]
    for wid in targets:
        if what == "minimize":
            subprocess.run(["xdotool", "windowminimize", wid], capture_output=True, timeout=5)
        elif what == "activate":
            subprocess.run(["xdotool", "windowactivate", wid], capture_output=True, timeout=5)
        elif shutil.which("wmctrl"):
            op = "add" if what == "maximize" else "remove"
            subprocess.run(["wmctrl", "-i", "-r", wid, "-b",
                            f"{op},maximized_vert,maximized_horz"],
                           capture_output=True, timeout=5)
            subprocess.run(["xdotool", "windowactivate", wid], capture_output=True, timeout=5)
        else:
            raise WindowError("maximizing a window on X11 needs wmctrl")
    return {"app": app_name, "windows": len(targets), "verified": False}


# ── Installing the extension (run by vision.sh) ──────────────────────────────

def _ext_root() -> Path:
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data) / "gnome-shell" / "extensions"


def _source_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "gnome-extension" / EXT_UUID


def _gsettings_list(key: str) -> Optional[list[str]]:
    try:
        out = subprocess.run(["gsettings", "get", "org.gnome.shell", key],
                             capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return None
    if not out:
        return None
    out = out.removeprefix("@as").strip()
    try:
        value = ast.literal_eval(out)
    except Exception:
        return None
    return [str(v) for v in value]


def _gsettings_set(key: str, value: list[str]) -> None:
    subprocess.run(["gsettings", "set", "org.gnome.shell", key, repr(value)],
                   capture_output=True, timeout=3)


def install_extension(src: Optional[Path] = None, root: Optional[Path] = None) -> str:
    """Copy the extension into the user's GNOME extensions folder and enable it.

    -> "unchanged" | "installed" | "updated" | "skipped". Safe to run on every
    start: it copies only when a file differs and only adds itself to the
    enabled list.
    """
    src = src or _source_dir()
    root = root or _ext_root()
    if not src.is_dir():
        return "skipped"
    dst = root / EXT_UUID
    existed = dst.exists()
    changed = False
    dst.mkdir(parents=True, exist_ok=True)
    for f in sorted(src.iterdir()):
        if not f.is_file():
            continue
        target = dst / f.name
        if not target.exists() or target.read_bytes() != f.read_bytes():
            shutil.copyfile(f, target)
            changed = True

    if shutil.which("gsettings"):
        enabled = _gsettings_list("enabled-extensions")
        if enabled is not None and EXT_UUID not in enabled:
            _gsettings_set("enabled-extensions", enabled + [EXT_UUID])
            changed = True
        disabled = _gsettings_list("disabled-extensions")
        if disabled and EXT_UUID in disabled:
            _gsettings_set("disabled-extensions", [d for d in disabled if d != EXT_UUID])
        if shutil.which("gnome-extensions"):
            # Works when GNOME already knows the extension; harmless otherwise.
            subprocess.run(["gnome-extensions", "enable", EXT_UUID],
                           capture_output=True, timeout=5)
    if not changed:
        return "unchanged"
    return "updated" if existed else "installed"


def _main(argv: list[str]) -> int:
    if "--install" in argv:
        if not shutil.which("gnome-shell"):
            return 0
        try:
            state = install_extension()
        except Exception as exc:
            print(f"▶ Could not install Vision's GNOME window helper: {exc}")
            return 0
        if state in ("installed", "updated") and not helper_running():
            print("▶ Installed Vision's GNOME window helper. Log out and back in "
                  "once so GNOME loads it (needed to minimize or maximize a "
                  "named app).")
        return 0
    print(json.dumps(list_windows(), indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
