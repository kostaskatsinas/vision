"""
Tests for core/window_control.py and the minimize / maximize / restore actions
of computer_settings. No desktop is needed: GNOME's window helper is replaced
by a fake that holds a window list and records every Act call. Run with:

    python -m core._window_control_tests

The bug these guard: "minimize Spotify" changed some other app's window,
because the old code acted on whichever window had focus (`win+down`, and
`wmctrl -r :ACTIVE:`, which on Wayland is an arbitrary XWayland window).
What these cannot show is GNOME really minimizing the window; that needs the
extension loaded on the real desktop.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from core import window_control as wc

_passed = 0
_failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


OWN = os.getpid()


def desktop() -> list[dict]:
    """What a busy desktop looks like to the extension."""
    return [
        {"id": 11, "pid": 5001, "app_name": "Spotify", "app_id": "spotify.desktop",
         "wm_class": "Spotify", "title": "Spotify Premium", "focused": False,
         "minimized": False, "maximized": False, "user_time": 50},
        {"id": 12, "pid": 5002, "app_name": "Google Chrome",
         "app_id": "google-chrome.desktop", "wm_class": "Google-chrome",
         "title": "Spotify - Web Player", "focused": True,
         "minimized": False, "maximized": False, "user_time": 90},
        {"id": 13, "pid": 5002, "app_name": "Google Chrome",
         "app_id": "google-chrome.desktop", "wm_class": "Google-chrome",
         "title": "Inbox", "focused": False,
         "minimized": False, "maximized": False, "user_time": 70},
        {"id": 14, "pid": 5003, "app_name": "Files", "app_id": "org.gnome.Nautilus.desktop",
         "wm_class": "org.gnome.Nautilus", "title": "Home", "focused": False,
         "minimized": True, "maximized": False, "user_time": 10},
        {"id": 15, "pid": OWN, "app_name": "", "app_id": "",
         "wm_class": "python3", "title": "VISION", "focused": False,
         "minimized": False, "maximized": False, "user_time": 99},
        {"id": 16, "pid": 5004, "app_name": "Terminal", "app_id": "org.gnome.Ptyxis.desktop",
         "wm_class": "org.gnome.Ptyxis", "title": "kostas@pc: ~/vision",
         "focused": False, "minimized": False, "maximized": False, "user_time": 20},
    ]


class FakeHelper:
    """Stands in for the GNOME extension's D-Bus object."""

    def __init__(self, windows=None, running=True, obey=True):
        self.windows = windows if windows is not None else desktop()
        self.running = running
        self.obey = obey
        self.acts: list[tuple[int, str]] = []

    def __call__(self, method, *args):
        import json
        if not self.running:
            raise wc.WindowError("No such object path")
        if method == "List":
            return json.dumps(self.windows)
        if method == "Act":
            wid, what = int(args[0]), str(args[1])
            self.acts.append((wid, what))
            win = next((w for w in self.windows if w["id"] == wid), None)
            if win is None:
                return False
            if self.obey:
                if what == "minimize":
                    win["minimized"] = True
                elif what == "maximize":
                    win["minimized"], win["maximized"] = False, True
                elif what == "unmaximize":
                    win["minimized"], win["maximized"] = False, False
                elif what == "restore":
                    if win["minimized"]:
                        win["minimized"] = False
                    else:
                        win["maximized"] = False
            return True
        raise wc.WindowError(f"unknown method {method}")


def install(helper: FakeHelper, pids: dict | None = None) -> None:
    wc._call = helper
    pids = pids or {}
    wc._app_pids = lambda name: set(pids.get(name.lower(), ()))


def test_matching() -> None:
    wins = desktop()
    ids = lambda found: [w["id"] for w in found]

    check("Spotify matches the Spotify app, not a Chrome tab titled Spotify",
          ids(wc.match_windows("Spotify", wins)) == [11], str(ids(wc.match_windows("Spotify", wins))))
    check("chrome matches both Chrome windows, the one in front first",
          ids(wc.match_windows("chrome", wins)) == [12, 13])
    check("'google chrome' matches by desktop id",
          ids(wc.match_windows("google chrome", wins)) == [12, 13])
    check("'nautilus' matches Files by its GNOME app id",
          ids(wc.match_windows("nautilus", wins)) == [14])
    check("'files' matches Files by the name GNOME shows",
          ids(wc.match_windows("files", wins)) == [14])
    check("filler words are ignored ('the spotify window')",
          ids(wc.match_windows("the spotify window", wins)) == [11])
    check("an app with no window matches nothing",
          wc.match_windows("Discord", wins) == [])
    check("an empty name matches nothing", wc.match_windows("", wins) == [])
    check("Vision's own window is never a match, even by pid",
          15 not in ids(wc.match_windows("python3", wins, {OWN})))
    check("a short fragment does not match a longer name ('fi' is not Files)",
          wc.match_windows("fi", wins) == [])
    # Process ids count only when no window matched by name.
    check("pid fallback finds a window whose names say nothing",
          ids(wc.match_windows("ptyxis-agent", wins, {5004})) == [16])
    check("pid fallback is not used when a name already matched",
          ids(wc.match_windows("spotify", wins, {5002})) == [11])


def test_act_on_named_app() -> None:
    os.environ["XDG_SESSION_TYPE"] = "wayland"

    h = FakeHelper()
    install(h)
    info = wc.act_on_app("Spotify", "minimize")
    check("minimize Spotify acts on the Spotify window only",
          h.acts == [(11, "minimize")], str(h.acts))
    check("...and it is read back as minimized", info["verified"] is True, str(info))
    check("the focused Chrome window is untouched",
          not next(w for w in h.windows if w["id"] == 12)["minimized"])

    h = FakeHelper()
    install(h)
    wc.act_on_app("chrome", "minimize")
    check("minimize chrome minimizes every Chrome window and nothing else",
          sorted(h.acts) == [(12, "minimize"), (13, "minimize")], str(h.acts))

    h = FakeHelper()
    install(h)
    info = wc.act_on_app("Spotify", "maximize")
    check("maximize Spotify maximizes exactly one window, Spotify's",
          h.acts == [(11, "maximize")] and info["verified"], str(h.acts))

    h = FakeHelper()
    install(h)
    wc.act_on_app("chrome", "maximize")
    check("maximize chrome takes the Chrome window in front, only that one",
          h.acts == [(12, "maximize")], str(h.acts))

    h = FakeHelper()
    install(h)
    info = wc.act_on_app("files", "restore")
    check("restore brings back the minimized Files window",
          h.acts == [(14, "restore")] and info["verified"], str(h.acts))

    h = FakeHelper()
    install(h)
    try:
        wc.act_on_app("Discord", "minimize")
        raised = ""
    except wc.WindowError as e:
        raised = str(e)
    check("an app with no open window fails and nothing is touched",
          "Discord" in raised and h.acts == [], raised + str(h.acts))

    h = FakeHelper(obey=False)
    install(h)
    info = wc.act_on_app("Spotify", "minimize")
    check("a window that did not change is not reported as verified",
          info["verified"] is False, str(info))

    h = FakeHelper(running=False)
    install(h)
    try:
        wc.act_on_app("Spotify", "minimize")
        err = None
    except wc.WindowError as e:
        err = e
    check("without the GNOME helper it refuses instead of guessing",
          isinstance(err, wc.HelperMissing) and h.acts == [], repr(err))

    try:
        wc.act_on_app("Spotify", "explode")
        bad = ""
    except wc.WindowError as e:
        bad = str(e)
    check("an unknown action is refused", "unknown" in bad, bad)


def test_act_on_focused() -> None:
    os.environ["XDG_SESSION_TYPE"] = "wayland"
    h = FakeHelper()
    install(h)
    info = wc.act_on_focused("minimize")
    check("'minimize this' acts on the window really in front",
          h.acts == [(12, "minimize")] and info["verified"], str(h.acts))

    wins = desktop()
    for w in wins:
        w["focused"] = w["id"] == 15
    h = FakeHelper(wins)
    install(h)
    try:
        wc.act_on_focused("minimize")
        msg = ""
    except wc.WindowError as e:
        msg = str(e)
    check("Vision's own window in front is never minimized by 'minimize this'",
          "Vision" in msg and h.acts == [], msg + str(h.acts))


def test_computer_settings() -> None:
    """The tool the model calls. With an app named, nothing goes to the
    keyboard and nothing goes to wmctrl."""
    os.environ["XDG_SESSION_TYPE"] = "wayland"
    from actions import computer_settings as cs
    import subprocess

    pressed: list[tuple] = []
    ran: list[list] = []
    import types
    real_pyautogui = cs.pyautogui
    real_run = cs.subprocess.run
    cs._PYAUTOGUI = True

    def fake_run(cmd, *a, **k):
        ran.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="['<Super>h']", stderr="")

    # A stand-in keyboard: the real pyautogui needs a display.
    cs.pyautogui = types.SimpleNamespace(hotkey=lambda *keys: pressed.append(keys))
    cs.subprocess.run = fake_run
    try:
        h = FakeHelper()
        install(h)
        out = cs.computer_settings({"action": "minimize", "app_name": "Spotify"})
        check("computer_settings minimize Spotify -> only Spotify's window",
              h.acts == [(11, "minimize")] and "status=done" in out, out + str(h.acts))
        check("...without pressing a key or running wmctrl",
              not pressed and not any("wmctrl" in c[0] for c in ran),
              str(pressed) + str(ran))

        h = FakeHelper()
        install(h)
        out = cs.computer_settings({"action": "maximise", "app_name": "files"})
        check("British spelling 'maximise' with an app works the same",
              h.acts == [(14, "maximize")] and "status=done" in out, out)

        h = FakeHelper()
        install(h)
        out = cs.computer_settings({"action": "minimize", "app_name": "Discord"})
        check("a named app that is not open fails and changes nothing",
              "status=failed" in out and h.acts == [] and not pressed, out)

        h = FakeHelper(running=False)
        install(h)
        pressed.clear()
        ran.clear()
        out = cs.computer_settings({"action": "minimize", "app_name": "Spotify"})
        check("named app, helper not loaded: fails with what to do, presses nothing",
              "status=failed" in out and "log" in out.lower() and not pressed, out)

        # No app named and no helper: GNOME's own minimize shortcut, never
        # win+down (that un-maximizes) and never wmctrl :ACTIVE:.
        pressed.clear()
        ran.clear()
        out = cs.computer_settings({"action": "minimize"})
        check("'minimize' with no app falls back to GNOME's shortcut (super+h)",
              pressed == [("super", "h")], str(pressed) + out)
        check("...and never runs wmctrl -r :ACTIVE: on Wayland",
              not any(":ACTIVE:" in c for c in ran), str(ran))

        pressed.clear()
        ran.clear()
        out = cs.computer_settings({"action": "maximize"})
        check("'maximize' with no app on Wayland never runs wmctrl :ACTIVE:",
              not any(":ACTIVE:" in c for c in ran) and len(pressed) == 1,
              str(ran) + str(pressed))
    finally:
        cs.subprocess.run = real_run
        cs.pyautogui = real_pyautogui


def test_tiling_focuses_named_app() -> None:
    """Tile / move-to-monitor with an app named raise THAT app first."""
    os.environ["XDG_SESSION_TYPE"] = "wayland"
    h = FakeHelper()
    install(h)
    check("activate_app raises the named app's window",
          wc.activate_app("Spotify") == "Spotify" and h.acts == [(11, "activate")],
          str(h.acts))
    h = FakeHelper(running=False)
    install(h)
    check("activate_app gives up quietly without the helper",
          wc.activate_app("Spotify") is None and h.acts == [])


def test_install_extension() -> None:
    import shutil
    real_which = shutil.which
    wc.shutil.which = lambda name: None          # no gsettings in the test
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "extensions"
            src = wc._source_dir()
            check("the extension ships with the repo",
                  (src / "extension.js").exists() and (src / "metadata.json").exists())
            first = wc.install_extension(src, root)
            second = wc.install_extension(src, root)
            dst = root / wc.EXT_UUID
            check("first run installs the extension", first == "installed", first)
            check("second run copies nothing", second == "unchanged", second)
            check("the files are in place",
                  (dst / "extension.js").read_bytes() == (src / "extension.js").read_bytes())
            (dst / "extension.js").write_text("old")
            check("a changed file is updated",
                  wc.install_extension(src, root) == "updated")
            check("a missing source is skipped",
                  wc.install_extension(Path(tmp) / "nope", root) == "skipped")
    finally:
        wc.shutil.which = real_which

    import json
    meta = json.loads((wc._source_dir() / "metadata.json").read_text())
    check("metadata uuid matches", meta.get("uuid") == wc.EXT_UUID)
    check("metadata lists GNOME 50 (the target machine)",
          "50" in meta.get("shell-version", []))


def main() -> int:
    real_call, real_pids = wc._call, wc._app_pids
    try:
        for test in (
            test_matching,
            test_act_on_named_app,
            test_act_on_focused,
            test_computer_settings,
            test_tiling_focuses_named_app,
            test_install_extension,
        ):
            test()
    finally:
        wc._call, wc._app_pids = real_call, real_pids
        os.environ.pop("XDG_SESSION_TYPE", None)
    print(f"{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
