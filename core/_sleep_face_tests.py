"""
Tests for the face looking asleep while Vision sleeps.
Run with:  python -m core._sleep_face_tests

Before this, a sleeping Vision kept its eyes open and kept swaying: the lids
were lowered in the animation state but never drawn, and the sway, the darting
gaze, the scan line and the glow all carried on. These check that once asleep
the head shuts its eyes, holds still and stops repainting several times a
second, that it wakes back to normal, and that every way of entering or
leaving sleep leaves the HUD in the right state (so nothing reopens the eyes
while Vision is still asleep). They draw offscreen; no window is shown.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from core.avatar import HoloAvatar  # noqa: E402

_passed = 0
_failed = 0
DT = 1 / 60


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


def run(av: HoloAvatar, state: str, seconds: float, amp: float = 0.0,
        speaking: bool = False) -> None:
    for _ in range(int(seconds / DT)):
        av.step(DT, amp, speaking=speaking, state=state)


def pose(av: HoloAvatar):
    return (av._yaw, av._pitch, av._gaze[0], av._gaze[1], av._scan, av._glow,
            av._lids, av._mouth)


def render(av: HoloAvatar) -> QImage:
    img = QImage(320, 380, QImage.Format.Format_RGB32)
    img.fill(QColor(6, 12, 20))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QColor(60, 200, 255)
    av.paint(p, 160, 160, 120, c, c, QColor(6, 12, 20))
    p.end()
    return img


# ── the head itself ──────────────────────────────────────────────────────────

def test_eyes_shut_and_still() -> None:
    av = HoloAvatar()
    run(av, "LISTENING", 3.0, amp=0.2)
    check("awake: eyes open", av._lids > 0.95, str(av._lids))
    check("awake: not settled asleep", not av.settled_asleep())
    run(av, "SLEEPING", 1.5, amp=0.2)
    check("eyes shut within 1.5 s", av._lids < 0.01, str(av._lids))
    run(av, "SLEEPING", 8.0, amp=0.3)
    check("fully asleep", av.sleep_level == 1.0, str(av.sleep_level))
    check("eyes fully shut", av._lids == 0.0)
    check("settled asleep", av.settled_asleep())
    check("head faces ahead (no sway)", abs(av._yaw) < 1e-6, str(av._yaw))
    a = pose(av)
    run(av, "SLEEPING", 5.0, amp=0.4)
    b = pose(av)
    check("nothing moves while asleep", all(abs(x - y) < 1e-6 for x, y in zip(a, b)),
          f"{a} -> {b}")
    check("room noise does not light the head", av._glow == 0.0, str(av._glow))
    check("mouth shut", av._mouth == 0.0)


def test_glance_ignored_while_asleep() -> None:
    av = HoloAvatar()
    run(av, "SLEEPING", 8.0)
    av.glance(0.9, 0.4)
    check("glance ignored asleep", av._glance is None)
    run(av, "SLEEPING", 0.5)
    check("still settled after a glance", av.settled_asleep())


def test_wakes_back_to_normal() -> None:
    av = HoloAvatar()
    run(av, "SLEEPING", 8.0)
    run(av, "LISTENING", 1.5)
    check("awake again within 1.5 s", av.sleep_level < 0.01, str(av.sleep_level))
    check("eyes open again", av._lids > 0.95, str(av._lids))
    y0 = av._yaw
    run(av, "LISTENING", 2.0)
    check("sway back", abs(av._yaw - y0) > 1e-3, f"{y0} -> {av._yaw}")
    av.glance(0.9, 0.4)
    check("glances again once awake", av._glance is not None)


def test_speaking_opens_eyes() -> None:
    """Something said while the gate is closed (a timer) wakes the face for it."""
    av = HoloAvatar()
    run(av, "SLEEPING", 8.0)
    run(av, "SLEEPING", 1.5, amp=0.5, speaking=True)
    check("eyes open to speak", av._lids > 0.9, str(av._lids))


def test_other_asleep_states() -> None:
    for st in ("STANDBY", "OFFLINE", "sleeping"):
        av = HoloAvatar()
        run(av, st, 8.0)
        check(f"{st} looks asleep", av.settled_asleep())


def test_drawn_closed_and_unchanging() -> None:
    awake = HoloAvatar()
    run(awake, "LISTENING", 3.0)
    asleep = HoloAvatar()
    run(asleep, "SLEEPING", 8.0)
    i1 = render(asleep)
    run(asleep, "SLEEPING", 3.0, amp=0.3)
    i2 = render(asleep)
    check("asleep picture does not change", i1 == i2)
    check("asleep picture differs from awake", i1 != render(awake))


# ── the HUD around it ────────────────────────────────────────────────────────

class FakeClock:
    """The HUD steps by wall clock; this lets a test run 10 s of it at once."""

    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def __getattr__(self, name):
        return getattr(time, name)


_clock = FakeClock()


def make_hud():
    import gui.hud as hud_mod
    from gui.hud import HudCanvas
    hud_mod.time = _clock
    h = HudCanvas("", "VISION")
    h.hud_style = "face"
    h.resize(480, 540)
    paints = []
    h.update = lambda *a: paints.append(1)
    h._on_screen = lambda: True
    return h, paints


def hud_run(h, state: str, seconds: float, amp: float = 0.0) -> None:
    h.state = state
    for _ in range(int(seconds / DT)):
        h._live_amp = amp
        _clock.t += DT
        h._step()


def test_hud_paint_rate() -> None:
    if not _have_avatar():
        return
    h, paints = make_hud()
    hud_run(h, "LISTENING", 3.0, amp=0.2)
    paints.clear()
    hud_run(h, "LISTENING", 10.0)
    awake = len(paints)
    hud_run(h, "SLEEPING", 10.0, amp=0.3)
    paints.clear()
    hud_run(h, "SLEEPING", 10.0, amp=0.3)
    asleep = len(paints)
    print(f"  paints in 10 s: awake {awake}, asleep {asleep}")
    check("asleep paints about once a second", asleep <= 12, str(asleep))
    check("awake paints far more", awake > 5 * asleep, f"{awake} vs {asleep}")
    check("heartbeat pulse held", h._blink is False)
    hud_run(h, "LISTENING", 0.5)
    paints.clear()
    hud_run(h, "LISTENING", 1.0)
    check("normal paint rate on waking", len(paints) >= 10, str(len(paints)))


def test_hud_draws_asleep() -> None:
    if not _have_avatar():
        return
    h, _ = make_hud()
    hud_run(h, "SLEEPING", 8.0)
    h.show()
    a = h.grab().toImage()
    hud_run(h, "SLEEPING", 2.0, amp=0.4)
    b = h.grab().toImage()
    check("sleeping HUD is a still picture (waveform flat too)", a == b)
    h.hide()


def _have_avatar() -> bool:
    from gui.hud import HudCanvas
    ok = HudCanvas("", "VISION")._avatar is not None
    check("HUD built the face", ok)
    return ok


# ── every way in and out of sleep leaves the right state ─────────────────────

class FakeUI:
    muted = False

    def __init__(self):
        self.states, self.log = [], []

    def set_state(self, s):
        self.states.append(s)

    def write_log(self, t):
        self.log.append(t)


def make_vision():
    import main
    v = main.VisionLive.__new__(main.VisionLive)
    v.ui = FakeUI()
    v._wake_enabled = True
    v._awake = True
    v._wake_for_one_sleep = False
    v._wake_detector = None
    v._speaking_lock = threading.Lock()
    v._is_speaking = False
    v._out_latency = 0.0
    v._out_level = 0.0
    v._last_user_speech = 0.0
    return v


def test_main_states() -> None:
    v = make_vision()
    v.sleep(reason="test")
    check("sleep shows SLEEPING", v.ui.states[-1] == "SLEEPING", str(v.ui.states))
    v.set_speaking(True)
    v.set_speaking(False)
    check("speech while asleep hands back to SLEEPING",
          v.ui.states[-2:] == ["SPEAKING", "SLEEPING"], str(v.ui.states))
    check("rest state asleep", v._rest_state() == "SLEEPING")
    v.ui.muted = True
    v.wake(reason="test")
    check("wake while muted still tells the face it is awake",
          v.ui.states[-1] == "LISTENING", str(v.ui.states))
    v.ui.muted = False
    v.set_speaking(False)
    check("awake rest state is LISTENING", v.ui.states[-1] == "LISTENING")
    v._wake_enabled = False
    check("wake word off: never asleep", v._rest_state() == "LISTENING")


def test_mute_keeps_sleep() -> None:
    from gui.window import MainWindow

    class Fake:
        _muted = False

        def __init__(self, asleep):
            self.applied = []
            self.hud = type("H", (), {})()
            self._log = type("L", (), {"append_log": lambda s, t: None})()
            self.wake_get_state = lambda: {"enabled": True, "awake": not asleep,
                                           "ready": True}

        def _style_mute_btn(self):
            pass

        def _apply_state(self, s):
            self.applied.append(s)

        def _asleep_now(self):
            return MainWindow._asleep_now(self)

    f = Fake(asleep=True)
    MainWindow._set_muted(f, True)
    MainWindow._set_muted(f, False)
    check("mute/unmute while asleep stays SLEEPING",
          f.applied == ["SLEEPING", "SLEEPING"], str(f.applied))
    f = Fake(asleep=False)
    MainWindow._set_muted(f, True)
    MainWindow._set_muted(f, False)
    check("mute/unmute while awake unchanged",
          f.applied == ["MUTED", "LISTENING"], str(f.applied))
    f = Fake(asleep=True)
    f.wake_get_state = None
    check("no wake word hookup: not asleep", MainWindow._asleep_now(f) is False)


def main() -> int:
    t0 = time.monotonic()
    for t in (test_eyes_shut_and_still, test_glance_ignored_while_asleep,
              test_wakes_back_to_normal, test_speaking_opens_eyes,
              test_other_asleep_states, test_drawn_closed_and_unchanging,
              test_hud_paint_rate, test_hud_draws_asleep,
              test_main_states, test_mute_keeps_sleep):
        try:
            t()
        except Exception as e:
            check(t.__name__, False, f"raised {e!r}")
    print(f"\n{_passed} passed, {_failed} failed  ({time.monotonic() - t0:.1f}s)")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
