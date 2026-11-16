"""
Tests for core/wayland_desktop.py that need no desktop: the portal is replaced
by a recorder. Run with:  python -m core._wayland_tests

What these cannot show is that GNOME really moves the pointer or types the
letter. That was measured on the target machine; see docs/VISION.md.
"""
from __future__ import annotations

import os
import sys

from core import wayland_desktop as wd

_passed = 0
_failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


class FakePortal:
    """Records what would have been sent to the portal."""
    # The four monitors of the machine this was built for.
    LAYOUT = [
        {"node": 70, "left": 0, "top": 1080, "width": 1080, "height": 1920},
        {"node": 71, "left": 1080, "top": 1080, "width": 5120, "height": 1440},
        {"node": 72, "left": 2555, "top": 0, "width": 1920, "height": 1080},
        {"node": 73, "left": 6200, "top": 1080, "width": 1080, "height": 1920},
    ]
    opened = 0

    def __init__(self):
        self.session = None
        self.streams = []
        self.log = []
        self.fail_next = False

    def open(self):
        FakePortal.opened += 1
        self.session = "/fake/session"
        self.streams = [dict(s) for s in self.LAYOUT]

    def close(self):
        self.session = None

    def _rec(self, *entry):
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("session closed")
        self.log.append(entry)

    def move(self, node, x, y):
        self._rec("move", node, int(x), int(y))

    def button(self, code, pressed):
        self._rec("button", code, bool(pressed))

    def axis(self, axis, steps):
        self._rec("axis", axis, steps)

    def keysym(self, sym, pressed):
        self._rec("keysym", sym, bool(pressed))

    def keycode(self, code, pressed):
        self._rec("keycode", code, bool(pressed))

    def pipewire_fd(self):
        return os.open(os.devnull, os.O_RDONLY)


def _fresh() -> FakePortal:
    wd.release()
    wd._monitor_key = None
    wd._last_pos = None
    wd._factory = FakePortal
    wd.monitors()                       # opens the session
    return wd._portal


def test_keyboard_planning() -> None:
    check("ascii needs latin", wd.plan_char("a") == ("latin", [ord("a")]))
    check("digit any layout", wd.plan_char("7") == (None, [ord("7")]))
    check("space any layout", wd.plan_char(" ") == (None, [0x20]))
    check("newline is Return", wd.plan_char("\n") == (None, [0xFF0D]))
    check("alpha", wd.plan_char("α") == ("gr", [0x7E1]))
    check("omega", wd.plan_char("ω") == ("gr", [0x7F9]))
    check("capital omega", wd.plan_char("Ω") == ("gr", [0x7D9]))
    check("final sigma", wd.plan_char("ς") == ("gr", [0x7F3]))
    check("sigma", wd.plan_char("σ") == ("gr", [0x7F2]))
    check("capital sigma", wd.plan_char("Σ") == ("gr", [0x7D2]))
    check("accent is a dead key", wd.plan_char("ά") == ("gr", [wd.DEAD_ACUTE, 0x7E1]))
    check("diaeresis is a dead key", wd.plan_char("ϊ") == ("gr", [wd.DEAD_DIAERESIS, 0x7E9]))
    kind, syms = wd.plan_char("ΐ")
    check("both marks", kind == "gr" and sorted(syms[:2]) == sorted([wd.DEAD_ACUTE, wd.DEAD_DIAERESIS])
          and syms[-1] == 0x7E9, str(syms))
    plan, skipped = wd.plan_text("ok 你 ✓")
    check("unknown characters are skipped, not fatal", skipped == "你✓" and len(plan) == 4, skipped)
    check("latin layout picked", wd._layout_for("latin", ["gr", "us"]) == "us")
    check("greek layout picked", wd._layout_for("gr", ["us", "gr+polytonic"]) == "gr+polytonic")
    check("missing greek layout", wd._layout_for("gr", ["us", "de"]) is None)


def test_monitors() -> None:
    _fresh()
    mons = wd.monitors()
    check("four monitors", len(mons) == 4)
    check("numbered left to right", [m["left"] for m in mons] == [0, 1080, 2555, 6200])
    check("largest is the default", wd.working_monitor()["width"] == 5120)
    check("size is the working monitor", wd.size() == (5120, 1440))
    check("left", wd.use_monitor("left")["left"] == 0)
    check("right", wd.use_monitor("right")["left"] == 6200)
    check("top", wd.use_monitor("top")["top"] == 0)
    check("by number", wd.use_monitor("3")["width"] == 1920)
    check("size follows", wd.size() == (1920, 1080))
    check("next wraps", wd.use_monitor("next")["number"] == 4 and wd.use_monitor("next")["number"] == 1)
    check("main", wd.use_monitor("main")["width"] == 5120)
    try:
        wd.use_monitor("9")
        check("bad screen refused", False)
    except wd.DesktopError:
        check("bad screen refused", True)
    check("description marks the one in use", "(in use)" in wd.describe_monitors())


def test_pointer() -> None:
    portal = _fresh()
    wd.click(100, 200)
    check("click goes to the working monitor's node", portal.log[0] == ("move", 71, 100, 200), str(portal.log[:1]))
    check("position re-sent before pressing", portal.log[1] == ("move", 71, 100, 200))
    check("press then release", portal.log[2:] == [("button", 0x110, True), ("button", 0x110, False)])
    check("position remembered", wd.position() == (100, 200))

    portal.log.clear()
    wd.use_monitor("3")
    wd.click(99999, -5, button="right", clicks=2)
    check("clamped to the monitor", portal.log[0] == ("move", 72, 1919, 0), str(portal.log[:1]))
    check("double right click", [e for e in portal.log if e[0] == "button"]
          == [("button", 0x111, True), ("button", 0x111, False)] * 2)

    portal.log.clear()
    wd.click()
    check("click in place sends no move", all(e[0] == "button" for e in portal.log))

    portal.log.clear()
    wd.move_to(10, 10)
    wd.drag_to(110, 10, duration=0.02)
    kinds = [e[0] for e in portal.log]
    check("drag presses, moves, releases", kinds[2] == "button" and kinds[-1] == "button"
          and portal.log[-2] == ("move", 72, 110, 10), str(portal.log[-3:]))

    portal.log.clear()
    wd.scroll(3)
    wd.scroll(-2)
    wd.scroll(4, horizontal=True)
    check("scroll directions", portal.log == [("axis", 0, -3), ("axis", 0, 2), ("axis", 1, 4)], str(portal.log))


def test_keys() -> None:
    portal = _fresh()
    wd.hotkey("ctrl", "c")
    check("hotkey order", portal.log == [("keycode", 29, True), ("keycode", 46, True),
                                         ("keycode", 46, False), ("keycode", 29, False)], str(portal.log))
    portal.log.clear()
    wd.press("enter", presses=2)
    check("press twice", portal.log == [("keycode", 28, True), ("keycode", 28, False)] * 2)
    try:
        wd.press("nosuchkey")
        check("unknown key refused", False)
    except wd.DesktopError:
        check("unknown key refused", True)


def test_typing() -> None:
    portal = _fresh()
    state = {"layout": "us"}
    real_current, real_configured = wd.current_layout, wd.configured_layouts
    wd.configured_layouts = lambda: ["us", "gr"]
    wd.current_layout = lambda: state["layout"]
    original_keycode = portal.keycode

    def keycode(code, pressed):
        original_keycode(code, pressed)
        # Super+Space: flip on the Space press while Super is held.
        if code == wd.KEY_SPACE and pressed:
            state["layout"] = "gr" if state["layout"] == "us" else "us"
    portal.keycode = keycode
    try:
        note = wd.type_text("Hi α1")
        syms = [e[1] for e in portal.log if e[0] == "keysym" and e[2]]
        check("keysyms in order", syms == [ord("H"), ord("i"), 0x20, 0x7E1, ord("1")], str(syms))
        switches = sum(1 for e in portal.log if e == ("keycode", wd.KEY_SPACE, True))
        check("switched to greek and back", switches == 2, str(switches))
        check("layout restored", state["layout"] == "us")
        check("nothing skipped", note == "")
        first_greek = portal.log.index(("keysym", 0x7E1, True))
        first_switch = portal.log.index(("keycode", wd.KEY_SPACE, True))
        check("switch happens before the greek letter", first_switch < first_greek)

        portal.log.clear()
        note = wd.type_text("a✓b")
        check("skipped characters are reported", "✓" in note, note)

        wd.configured_layouts = lambda: ["us"]
        try:
            wd.type_text("α")
            check("missing layout is an error", False)
        except wd.DesktopError as e:
            check("missing layout is an error", "Greek" in str(e), str(e))
    finally:
        wd.current_layout, wd.configured_layouts = real_current, real_configured


def test_capture() -> None:
    import io
    import subprocess
    _fresh()
    wd.use_monitor("3")
    seen = {}
    real_run = wd.subprocess.run

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, bytes([10, 20, 30, 0]) * (1920 * 1080), b"")
    wd.subprocess.run = run
    try:
        image = wd.screenshot()
        check("frame scaled to the logical size",
              "video/x-raw,format=RGBx,width=1920,height=1080" in seen["cmd"]
              and "videoscale" in seen["cmd"], str(seen.get("cmd")))
        check("image size", image.size == (1920, 1080), str(image.size))
        check("colours decoded", image.getpixel((5, 5)) == (10, 20, 30))
        buf = io.BytesIO()
        image.save(buf, "PNG")
        image.save(io.BytesIO(), "JPEG")
        check("image can be saved", buf.tell() > 0)
        check("region crop", wd.screenshot(region=(0, 0, 100, 50)).size == (100, 50))
    finally:
        wd.subprocess.run = real_run


def test_session_lifecycle() -> None:
    portal = _fresh()
    before = FakePortal.opened
    wd.click(1, 1)
    check("session reused", FakePortal.opened == before)
    portal.fail_next = True
    try:
        wd.click(1, 1)
        check("a dead session is reported", False)
    except wd.DesktopError as e:
        check("a dead session is reported", "ended" in str(e), str(e))
    check("dead session dropped", not wd.is_active())
    wd.click(2, 2)
    check("next request opens a new one", FakePortal.opened == before + 1 and wd.is_active())
    check("release closes", wd.release() == "released" and not wd.is_active())
    check("release twice is harmless", wd.release() == "nothing was being controlled")


def test_shim() -> None:
    portal = _fresh()
    os.environ["XDG_SESSION_TYPE"] = "wayland"
    try:
        check("available on wayland", wd.available())
        saved = sys.modules.pop("pyautogui", None)
        check("install registers the stand-in", wd.install())
        import importlib
        import pyautogui
        from core.lazyimport import installed
        check("import gives the stand-in", getattr(pyautogui, "VISION_WAYLAND", False))
        check("installed() still answers", installed("pyautogui"))
        check("importlib agrees", importlib.import_module("pyautogui") is pyautogui)
        pyautogui.PAUSE = 0
        portal.log.clear()
        pyautogui.click(5, 6, button="left", clicks=1)
        pyautogui.hotkey("ctrl", "v")
        pyautogui.press("enter")
        pyautogui.scroll(-3)
        pyautogui.moveTo(7, 8, duration=0.2)
        before = len(portal.log)
        try:
            pyautogui.write("caf\u00e9 \U0001F600")
            check("untypeable text refused", False)
        except wd.DesktopError as e:
            check("untypeable text refused", "nothing was typed" in str(e), str(e))
        check("nothing typed when refused", len(portal.log) == before)
        check("shim calls reach the portal", ("move", 71, 5, 6) in portal.log
              and ("keycode", 47, True) in portal.log and ("keycode", 28, True) in portal.log
              and ("axis", 0, 3) in portal.log and ("move", 71, 7, 8) in portal.log, str(portal.log))
        check("size and onScreen", pyautogui.size() == (5120, 1440)
              and pyautogui.onScreen(10, 10) and not pyautogui.onScreen(6000, 10))
        os.environ["VISION_NO_WAYLAND_DESKTOP"] = "1"
        check("can be switched off", not wd.available())
        del os.environ["VISION_NO_WAYLAND_DESKTOP"]
        sys.modules.pop("pyautogui", None)
        if saved is not None:
            sys.modules["pyautogui"] = saved
    finally:
        os.environ.pop("XDG_SESSION_TYPE", None)


def test_gnome_display_config_and_monitor_resolution() -> None:
    _fresh()
    # Natural monitor descriptors on the default 4-monitor layout
    check("monitor 1 phrase", wd.use_monitor("monitor 1")["number"] == 1)
    check("screen 2 phrase", wd.use_monitor("screen #2")["number"] == 2)
    check("ordinal third", wd.use_monitor("third")["number"] == 3)
    check("ordinal fourth", wd.use_monitor("fourth")["number"] == 4)
    check("center selects ultrawide", wd.use_monitor("center")["width"] == 5120)

    # Simulate GNOME Settings -> Displays (`org.gnome.Mutter.DisplayConfig.GetCurrentState`)
    # where the ultrawide (1080, 1080) is monitor 1 (primary), left portrait (0, 1080) is 2,
    # right portrait (6200, 1080) is 3, and top 1080p (2555, 0) is 4.
    fake_state = (
        1,
        [
            (("DP-1", "SAM", "Odyssey G9", "001"), [], {"is-builtin": ("b", False), "display-name": ("s", "Samsung 49")}),
            (("DP-2", "DEL", "Left Portrait", "002"), [], {"is-builtin": ("b", False), "display-name": ("s", "Dell Left")}),
            (("DP-3", "DEL", "Right Portrait", "003"), [], {"is-builtin": ("b", False), "display-name": ("s", "Dell Right")}),
            (("HDMI-1", "LG", "Top 1080p", "004"), [], {"is-builtin": ("b", False), "display-name": ("s", "LG Top")}),
        ],
        [
            (1080, 1080, 1.0, 0, True, [("DP-1", "SAM", "Odyssey G9", "001")], {}),
            (0, 1080, 1.0, 0, False, [("DP-2", "DEL", "Left Portrait", "002")], {}),
            (6200, 1080, 1.0, 0, False, [("DP-3", "DEL", "Right Portrait", "003")], {}),
            (2555, 0, 1.0, 0, False, [("HDMI-1", "LG", "Top 1080p", "004")], {}),
        ],
        {},
    )
    parsed = wd._parse_gnome_display_config(fake_state)
    check("parsed 4 logical monitors", len(parsed) == 4)
    check("DP-1 is ui_number 1 and primary",
          parsed[(1080, 1080)]["ui_number"] == 1 and parsed[(1080, 1080)]["primary"] is True)
    check("HDMI-1 is ui_number 4", parsed[(2555, 0)]["ui_number"] == 4)

    real_fetcher = wd._display_config_fetcher
    wd._display_config_fetcher = lambda _portal=None: parsed
    try:
        mons = wd.monitors()
        check("monitors ordered by GNOME DisplayConfig number",
              [m["number"] for m in mons] == [1, 2, 3, 4]
              and mons[0]["width"] == 5120
              and mons[1]["left"] == 0
              and mons[2]["left"] == 6200
              and mons[3]["top"] == 0,
              str(mons))
        check("resolve by connector name", wd.resolve_monitor("hdmi-1")["top"] == 0)
        check("resolve by display name", wd.resolve_monitor("Odyssey")["width"] == 5120)
    finally:
        wd._display_config_fetcher = real_fetcher


def test_monitor_path_and_window_movement() -> None:
    portal = _fresh()
    mons = wd.monitors()
    by_num = {m["number"]: m for m in mons}
    # 1: Left (0, 1080), 2: Center (1080, 1080), 3: Top (2555, 0), 4: Right (6200, 1080)
    check("path center -> left", wd.monitor_path(by_num[2], by_num[1], mons) == ["left"])
    check("path center -> top", wd.monitor_path(by_num[2], by_num[3], mons) == ["up"])
    check("path center -> right", wd.monitor_path(by_num[2], by_num[4], mons) == ["right"])
    check("path left -> right crosses center",
          wd.monitor_path(by_num[1], by_num[4], mons) == ["right", "right"])
    check("path top -> left goes down then left",
          wd.monitor_path(by_num[3], by_num[1], mons) == ["down", "left"])

    portal.log.clear()
    res = wd.move_window_to_monitor("monitor 1")
    check("move_window_to_monitor target is 1",
          res["monitor"]["number"] == 1 and res["steps"] == ["left"], str(res))
    # Super (125) + Shift (42) + Left (105) followed by pointer move to center of monitor 1 (node 70, 540, 960)
    check("sent Super+Shift+Left and centered pointer on monitor 1",
          ("keycode", 125, True) in portal.log
          and ("keycode", 42, True) in portal.log
          and ("keycode", 105, True) in portal.log
          and portal.log[-1] == ("move", 70, 540, 960),
          str(portal.log))

    portal.log.clear()
    res2 = wd.move_window_to_monitor("4", from_monitor="1")
    right_presses = sum(1 for e in portal.log if e == ("keycode", 106, True))
    check("moving from monitor 1 to 4 presses Super+Shift+Right twice",
          res2["steps"] == ["right", "right"]
          and right_presses == 2
          and portal.log[-1] == ("move", 73, 540, 960),
          str(portal.log))


def test_tiling_shell() -> None:
    portal = _fresh()
    check("parse <Super>Left", wd.parse_gtk_accelerator("<Super>Left") == ("super", "left"))
    check("parse <Control><Super>Right",
          wd.parse_gtk_accelerator("<Control><Super>Right") == ("ctrl", "super", "right"))
    check("parse <Control><Super>period",
          wd.parse_gtk_accelerator("<Control><Super>period") == ("ctrl", "super", "."))
    check("parse <Shift><Alt><Super>Tab",
          wd.parse_gtk_accelerator("<Shift><Alt><Super>Tab") == ("shift", "alt", "super", "tab"))

    portal.log.clear()
    r_tile = wd.tiling_shell_action("tile", "left")
    check("tile left sends Super+Left",
          r_tile["action"] == "move-window-left"
          and ("keycode", 125, True) in portal.log
          and ("keycode", 105, True) in portal.log,
          str(portal.log))

    portal.log.clear()
    r_span = wd.tiling_shell_action("span", "right")
    check("span right sends Ctrl+Super+Right",
          r_span["action"] == "span-window-right"
          and ("keycode", 29, True) in portal.log
          and ("keycode", 125, True) in portal.log
          and ("keycode", 106, True) in portal.log,
          str(portal.log))

    portal.log.clear()
    r_all = wd.tiling_shell_action("span_all")
    check("span_all sends Ctrl+Super+A",
          r_all["action"] == "span-window-all-tiles"
          and ("keycode", 30, True) in portal.log,
          str(portal.log))

    portal.log.clear()
    r_untile = wd.tiling_shell_action("untile")
    check("untile sends Super+U",
          r_untile["action"] == "untile-window"
          and ("keycode", 22, True) in portal.log,
          str(portal.log))

    portal.log.clear()
    r_center = wd.tiling_shell_action("center")
    check("center sends Ctrl+Alt+C",
          r_center["action"] == "move-window-center"
          and ("keycode", 46, True) in portal.log,
          str(portal.log))

    portal.log.clear()
    r_combo = wd.tiling_shell_action("tile", "right", monitor="4")
    check("tile with monitor moves to monitor 4 then tiles right",
          r_combo["action"] == "move-window-right"
          and r_combo.get("monitor") == 4
          and ("move", 73, 540, 960) in portal.log,
          str(portal.log))


def test_open_app_and_kill_app() -> None:
    import subprocess
    from core import appidentity
    from actions import computer_control as cc
    from actions import open_app as oa

    portal = _fresh()
    os.environ["XDG_SESSION_TYPE"] = "wayland"
    try:
        # 1. Test open_app on monitor 1 when app is already running
        fake_match = appidentity.Match(pid=999991, name="spotify", score=100, is_self=False)
        real_running = appidentity.running
        real_focus = appidentity.focus
        appidentity.running = lambda spoken, include_self=False: [fake_match] if "spotify" in spoken.lower() else []
        appidentity.focus = lambda m: True
        try:
            portal.log.clear()
            out = oa.open_app({"app_name": "Spotify", "monitor": "monitor 1", "tile": "left"})
            check("open_app on monitor 1 returns done with monitor=1",
                  "status=done" in out and "monitor=1" in out, out)
            check("open_app moved pointer to monitor 1 and tiled left",
                  ("move", 70, 540, 960) in portal.log
                  and ("keycode", 105, True) in portal.log,
                  str(portal.log))
        finally:
            appidentity.running = real_running
            appidentity.focus = real_focus

        # 2. Test kill_app and close_app on a real multi-process tree spawned by Python
        sleep_bin = "/bin/sleep" if os.path.exists("/bin/sleep") else "/usr/bin/sleep"
        if os.path.exists(sleep_bin):
            # Spawn a parent shell that spawns a child sleep process
            parent = subprocess.Popen(
                ["/bin/sh", "-c", f"{sleep_bin} 60 & wait"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            import time
            time.sleep(0.25)
            matches = appidentity.running("sleep")
            check("spawned sleep process is found by running('sleep') and not hidden as self",
                  any(not m.is_self for m in matches), str(matches))

            res_kill = cc.computer_control({"action": "kill_app", "app_name": "sleep"})
            parent.wait(timeout=2)
            check("kill_app killed all sleep processes",
                  "status=done" in res_kill and not appidentity.is_running(parent.pid),
                  res_kill)

            # Also test close_app on a process tree with children
            parent2 = subprocess.Popen(
                ["/bin/sh", "-c", f"{sleep_bin} 60 & wait"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(0.25)
            res_close = cc.computer_control({"action": "close_app", "app_name": "sleep"})
            parent2.wait(timeout=2)
            check("close_app closed all sleep processes",
                  "status=done" in res_close and not appidentity.is_running(parent2.pid),
                  res_close)

            # Also verify self-protection refuses to kill python
            res_self = cc.computer_control({"action": "kill_app", "app_name": "python"})
            check("kill_app refuses to kill the assistant itself",
                  "status=failed" in res_self and "assistant" in res_self,
                  res_self)

        # 3. Test computer_control tile_window and move_window_to_monitor
        portal.log.clear()
        res_tile_cc = cc.computer_control({"action": "tile_window", "direction": "right"})
        check("computer_control tile_window succeeds",
              "status=done" in res_tile_cc and "move-window-right" in res_tile_cc,
              res_tile_cc)

        portal.log.clear()
        res_mon_cc = cc.computer_control({"action": "move_window_to_monitor", "monitor": "3"})
        check("computer_control move_window_to_monitor succeeds",
              "status=done" in res_mon_cc and "monitor=3" in res_mon_cc,
              res_mon_cc)
    finally:
        os.environ.pop("XDG_SESSION_TYPE", None)


def test_window_actions_are_safe() -> None:
    import subprocess
    import time
    from core import appidentity
    from actions import computer_control as cc

    portal = _fresh()
    os.environ["XDG_SESSION_TYPE"] = "wayland"
    real_running, real_focus = appidentity.running, appidentity.focus
    try:
        # A named app that is not running: nothing is pressed, and it says so.
        appidentity.running = lambda spoken, include_self=False: []
        portal.log.clear()
        out = cc.computer_control({"action": "tile_window", "direction": "left",
                                   "app_name": "Spotify"})
        check("tiling a named app that is not running fails and presses nothing",
              "status=failed" in out and not any(e[0] == "keycode" for e in portal.log),
              out + str(portal.log))

        # Running but its window cannot be focused: same, never the focused window.
        fake = appidentity.Match(pid=999992, name="spotify", score=100, is_self=False)
        seen_self = []
        def running(spoken, include_self=False):
            seen_self.append(include_self)
            return [fake]
        appidentity.running = running
        appidentity.focus = lambda m: False
        portal.log.clear()
        out = cc.computer_control({"action": "move_window_to_monitor", "monitor": "1",
                                   "app_name": "Spotify"})
        check("an app whose window cannot be focused is not moved",
              "status=failed" in out and not any(e[0] == "keycode" for e in portal.log),
              out + str(portal.log))
        check("focusing a named app never considers the assistant itself",
              seen_self and not any(seen_self), str(seen_self))
        appidentity.running, appidentity.focus = real_running, real_focus

        # Window actions are not checked afterwards, so they are not "verified".
        out = cc.computer_control({"action": "tile_window", "direction": "right",
                                   "amount": "two"})
        check("a non-number amount does not crash and nothing claims verified",
              "status=done" in out and "verified=True" not in out, out)

        # Already on the target monitor: no keys, so the window stays put.
        wd.use_monitor(3)
        portal.log.clear()
        res = wd.move_window_to_monitor(3)
        check("moving to the monitor it is already on presses nothing",
              res["steps"] == [] and not any(e[0] == "keycode" for e in portal.log),
              str(res) + str(portal.log))

        # close_app never escalates to a kill; kill_app does.
        sleep_bin = "/bin/sleep" if os.path.exists("/bin/sleep") else "/usr/bin/sleep"
        if os.path.exists(sleep_bin):
            stubborn = subprocess.Popen(
                ["/bin/sh", "-c", f"trap '' TERM; exec {sleep_bin} 61"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.3)
            out = cc.computer_control({"action": "close_app", "app_name": "sleep"})
            alive = stubborn.poll() is None
            check("close_app leaves an app that will not quit running (no SIGKILL)",
                  alive and "status=done" not in out and "kill_app" in out, out)
            out = cc.computer_control({"action": "kill_app", "app_name": "sleep"})
            stubborn.wait(timeout=3)
            check("kill_app force-kills it", "status=done" in out, out)
    finally:
        appidentity.running, appidentity.focus = real_running, real_focus
        os.environ.pop("XDG_SESSION_TYPE", None)


def main() -> int:
    wd.IDLE_CLOSE_S = 3600
    wd.missing = lambda: []             # the recorder needs neither package
    for test in (
        test_keyboard_planning,
        test_monitors,
        test_pointer,
        test_keys,
        test_typing,
        test_capture,
        test_session_lifecycle,
        test_shim,
        test_gnome_display_config_and_monitor_resolution,
        test_monitor_path_and_window_movement,
        test_tiling_shell,
        test_open_app_and_kill_app,
        test_window_actions_are_safe,
    ):
        test()
    wd.release()
    wd._factory = wd._Portal
    print(f"{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
