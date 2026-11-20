"""
Tests for going to sleep by voice (the sleep_vision tool).
Run with:  python -m core._sleep_tests

Saying "sleep" used to do nothing: there was no tool the model could call, so
it answered "okay" and went on listening. These build the assistant object
without starting it (no window, microphone, model or network) and replace the
UI and the wake word detector, then check that the tool closes the mic gate,
waits for the reply to be heard, and leaves the wake word setting as it was.
What these cannot show is the model choosing the tool when Kostas speaks;
that is checked on the PC.

The second half covers AUTO-SLEEP: with the wake word off, Vision puts itself
to sleep after an hour (the CONTROLS setting) with nobody talking to it.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
from memory import config_manager

_passed = 0
_failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


class FakeUI:
    muted = False

    def __init__(self):
        self.states, self.log = [], []

    def set_state(self, s):
        self.states.append(s)

    def write_log(self, t):
        self.log.append(t)


class FakeDetector:
    def __init__(self, starts=True):
        self.ready = False
        self.starts = starts
        self.resets = 0
        self.fed = 0

    def start(self):
        self.ready = self.starts
        return self.ready

    def reset(self):
        self.resets += 1

    def feed(self, frames):
        self.fed += 1


def make(wake_enabled: bool, detector: FakeDetector | None) -> main.VisionLive:
    v = main.VisionLive.__new__(main.VisionLive)
    v.ui = FakeUI()
    v._wake_enabled = wake_enabled
    v._awake = True
    v._wake_for_one_sleep = False
    v._wake_detector = detector
    v._wake_sleep_timeout = main.WAKE_SLEEP_TIMEOUT
    v._last_user_speech = 0.0
    v._last_user_activity = 0.0
    v._ptt_held = False
    v._turn_done_event = asyncio.Event()
    v._speaking_lock = threading.Lock()
    v._is_speaking = False
    v._out_latency = 0.0
    v._out_level = 0.0
    v._engine = None
    v._loop = None
    return v


async def reply_heard(v: main.VisionLive) -> None:
    """What the receive and playback loops do once the short reply is out."""
    v._is_speaking = True
    await asyncio.sleep(0.05)
    v._turn_done_event.set()
    await asyncio.sleep(0.05)
    v._is_speaking = False


async def run_tool(v: main.VisionLive) -> str:
    return await v._inline_tools()["sleep_vision"]({})


async def settle(v: main.VisionLive) -> None:
    for t in list(v.__dict__.get("_bg_tasks", ())):
        await t


async def test_declared() -> None:
    names = [t["name"] for t in main.TOOL_DECLARATIONS]
    check("sleep_vision is declared to the model", "sleep_vision" in names)
    decl = next(t for t in main.TOOL_DECLARATIONS if t["name"] == "sleep_vision")
    check("description tells it apart from shutdown",
          "shutdown_vision" in decl["description"])
    v = make(True, FakeDetector())
    check("sleep_vision has a handler", "sleep_vision" in v._inline_tools())


async def test_wake_word_on() -> None:
    det = FakeDetector()
    v = make(True, det)
    saved = []
    main.save_wake_word_enabled = lambda on: saved.append(on)
    out = await run_tool(v)
    check("still awake while the reply plays", v._awake, out)
    check("reply is told not to say the wake phrase", main.WAKE_PHRASE in out, out)
    await reply_heard(v)
    await settle(v)
    check("asleep once the reply was heard", not v._awake)
    check("UI shows SLEEPING", v.ui.states[-1:] == ["SLEEPING"], str(v.ui.states))
    check("detector reset on sleep", det.resets == 1)
    check("setting not touched", saved == [])
    v.wake(reason="wake word")
    check("wakes again on the wake word", v._awake)
    check("wake word stays on after waking", v._wake_enabled)


async def test_waits_for_speech() -> None:
    v = make(True, FakeDetector())
    await run_tool(v)
    v._is_speaking = True
    v._turn_done_event.set()
    await asyncio.sleep(0.5)
    check("not asleep while still speaking", v._awake)
    v._is_speaking = False
    await settle(v)
    check("asleep after speech ended", not v._awake)


async def test_wake_word_off_but_installed() -> None:
    det = FakeDetector()
    v = make(False, det)
    saved = []
    main.save_wake_word_enabled = lambda on: saved.append(on)
    main.wake_is_ready = lambda: True
    await run_tool(v)
    await reply_heard(v)
    await settle(v)
    check("sleeps with wake word off", not v._awake)
    check("detector started for this sleep", det.ready)
    check("mic gate closed", v._wake_enabled and not v._awake)
    check("nothing saved", saved == [])
    v.wake(reason="wake word")
    check("awake again", v._awake)
    check("wake word back off after waking", not v._wake_enabled)
    check("one-sleep flag cleared", not v._wake_for_one_sleep)


async def test_push_to_talk_ends_one_sleep() -> None:
    v = make(False, FakeDetector())
    main.wake_is_ready = lambda: True
    await run_tool(v)
    await reply_heard(v)
    await settle(v)
    v._on_ptt(True)
    check("push-to-talk wakes it", v._awake)
    check("push-to-talk turns the one-sleep wake word back off", not v._wake_enabled)


async def test_not_set_up() -> None:
    v = make(False, None)
    main.wake_is_ready = lambda: False
    out = await run_tool(v)
    await settle(v)
    check("stays awake when the wake word is not set up", v._awake and not v._wake_enabled)
    check("model is told it is still listening", "still listening" in out, out)
    check("no sleep task started", not v.__dict__.get("_bg_tasks"))


async def test_detector_fails() -> None:
    v = make(False, FakeDetector(starts=False))
    main.wake_is_ready = lambda: True
    out = await run_tool(v)
    await settle(v)
    check("stays awake when the detector cannot start", v._awake and not v._wake_enabled, out)

    v = make(True, FakeDetector(starts=False))
    out = await run_tool(v)
    await settle(v)
    check("wake word on but detector down: stays awake", v._awake, out)


async def test_enable_while_one_sleep() -> None:
    v = make(False, FakeDetector())
    main.wake_is_ready = lambda: True
    main.save_wake_word_enabled = lambda on: None
    await run_tool(v)
    await reply_heard(v)
    await settle(v)
    v._ui_wake_toggle(True)
    v.wake(reason="wake word")
    check("turning WAKE WORD on keeps it on after waking", v._wake_enabled)


# ── Auto-sleep after a quiet stretch ─────────────────────────────────────────

HOUR = 3600.0


def quiet_for(v: main.VisionLive, seconds: float) -> None:
    t = main.time.monotonic() - seconds
    v._last_user_speech = t
    v._last_user_activity = t


async def test_idle_sleeps_after_an_hour() -> None:
    det = FakeDetector()
    v = make(False, det)
    saved = []
    main.save_wake_word_enabled = lambda on: saved.append(on)
    main.wake_is_ready = lambda: True
    main.get_idle_sleep_minutes = lambda: 60
    quiet_for(v, 59 * 60)
    await v._sleep_watch_tick()
    check("awake after 59 quiet minutes", v._awake)
    quiet_for(v, HOUR + 5)
    await v._sleep_watch_tick()
    check("asleep after an hour of quiet", not v._awake)
    check("UI shows SLEEPING", v.ui.states[-1:] == ["SLEEPING"], str(v.ui.states))
    check("log says why", any("quiet for 1 hour" in l for l in v.ui.log), str(v.ui.log))
    check("detector started for this sleep", det.ready)
    check("wake word on for this one sleep only", v._wake_enabled and v._wake_for_one_sleep)
    check("auto-sleep saves nothing", saved == [])
    v.wake(reason="wake word")
    check("wakes on the wake word", v._awake)
    check("wake word back off after waking", not v._wake_enabled)
    await v._sleep_watch_tick()
    check("does not fall straight back asleep after waking", v._awake)


async def test_idle_other_lengths() -> None:
    main.wake_is_ready = lambda: True
    main.get_idle_sleep_minutes = lambda: 30
    v = make(False, FakeDetector())
    quiet_for(v, 31 * 60)
    await v._sleep_watch_tick()
    check("30-minute setting sleeps after 31 minutes", not v._awake)
    check("log names 30 minutes", any("quiet for 30 minutes" in l for l in v.ui.log))

    main.get_idle_sleep_minutes = lambda: 120
    v = make(False, FakeDetector())
    quiet_for(v, 90 * 60)
    await v._sleep_watch_tick()
    check("2-hour setting is still awake at 90 minutes", v._awake)

    main.get_idle_sleep_minutes = lambda: 0
    v = make(False, FakeDetector())
    quiet_for(v, 10 * HOUR)
    await v._sleep_watch_tick()
    check("OFF never sleeps", v._awake and not v._wake_enabled)


async def test_idle_what_counts() -> None:
    main.wake_is_ready = lambda: True
    main.get_idle_sleep_minutes = lambda: 60

    v = make(False, FakeDetector())
    quiet_for(v, 2 * HOUR)
    v._note_user_activity()
    await v._sleep_watch_tick()
    check("a typed command or key press restarts the clock", v._awake)

    v = make(False, FakeDetector())
    quiet_for(v, 2 * HOUR)
    v._last_user_speech = main.time.monotonic() - 60
    await v._sleep_watch_tick()
    check("speech a minute ago keeps it awake", v._awake)

    v = make(False, FakeDetector())
    quiet_for(v, 2 * HOUR)
    v._is_speaking = True
    await v._sleep_watch_tick()
    check("never sleeps in the middle of a reply", v._awake)

    v = make(False, FakeDetector())
    quiet_for(v, 2 * HOUR)
    v._ptt_held = True
    await v._sleep_watch_tick()
    check("never sleeps while the push-to-talk key is held", v._awake)

    v = make(False, FakeDetector())
    quiet_for(v, 2 * HOUR)
    v._on_ptt(True)
    v._ptt_held = False
    await v._sleep_watch_tick()
    check("pressing push-to-talk restarts the clock", v._awake and v._quiet_seconds() < 5)

    import inspect
    src = inspect.getsource(main.VisionLive._process_dashboard_commands)
    check("a phone dashboard command restarts the clock",
          src.find("_note_user_activity()") != -1
          and src.find("_note_user_activity()") < src.find("send_client_content"))


async def test_idle_not_set_up() -> None:
    main.wake_is_ready = lambda: False
    main.get_idle_sleep_minutes = lambda: 60
    v = make(False, None)
    quiet_for(v, 2 * HOUR)
    await v._sleep_watch_tick()
    await v._sleep_watch_tick()
    await v._sleep_watch_tick()
    check("stays awake without a wake word", v._awake and not v._wake_enabled)
    notes = [l for l in v.ui.log if "staying awake" in l]
    check("says why once, not every 5 seconds", len(notes) == 1, str(v.ui.log))
    v._note_user_activity()
    quiet_for(v, 2 * HOUR + 10)
    await v._sleep_watch_tick()
    notes = [l for l in v.ui.log if "staying awake" in l]
    check("says it again after the next quiet stretch", len(notes) == 2, str(v.ui.log))

    main.wake_is_ready = lambda: True
    v = make(False, FakeDetector(starts=False))
    quiet_for(v, 2 * HOUR)
    await v._sleep_watch_tick()
    check("stays awake when the detector cannot start", v._awake and not v._wake_enabled)


async def test_wake_word_on_keeps_two_minutes() -> None:
    main.get_idle_sleep_minutes = lambda: 60
    v = make(True, FakeDetector())
    v._wake_detector.start()
    quiet_for(v, 3 * 60)
    await v._sleep_watch_tick()
    check("wake word on: still sleeps after 2 minutes", not v._awake)
    check("wake word on: reason unchanged",
          any("no speech for 2 minutes" in l for l in v.ui.log), str(v.ui.log))
    check("wake word on: stays on", v._wake_enabled and not v._wake_for_one_sleep)

    main.get_idle_sleep_minutes = lambda: 0
    v = make(True, FakeDetector())
    v._wake_detector.start()
    quiet_for(v, 3 * 60)
    await v._sleep_watch_tick()
    check("AUTO-SLEEP OFF does not stop the wake word's 2 minutes", not v._awake)


async def test_idle_setting() -> None:
    real = config_manager.load_api_keys
    try:
        for stored, want in ((None, 60), (30, 30), (0, 0), (-5, 0), ("x", 60),
                             (10 ** 6, 24 * 60)):
            data = {} if stored is None else {"idle_sleep_minutes": stored}
            config_manager.load_api_keys = lambda d=data: d
            got = config_manager.get_idle_sleep_minutes()
            check(f"setting {stored!r} reads as {want}", got == want, str(got))
    finally:
        config_manager.load_api_keys = real
    check("default is one hour", config_manager.IDLE_SLEEP_DEFAULT_MINUTES == 60)
    check("one hour is a choice", 60 in config_manager.IDLE_SLEEP_CHOICES)
    from gui.window import MainWindow
    lab = MainWindow._idle_sleep_label
    check("button labels", [lab(m) for m in (0, 30, 60, 120)]
          == ["OFF", "30 MIN", "1 HOUR", "2 HOURS"])


async def test_muted_mic_does_not_wake() -> None:
    """Kostas, 2026-11-20: with the mic muted it still woke on "Vision"."""
    det = FakeDetector()
    v = make(True, det)
    det.ready = True
    v._awake = False
    v.ui.muted = True
    for _ in range(5):
        check("muted block stays out of the model", v._wake_gate(b"x"))
    check("muted mic feeds the wake word nothing", det.fed == 0, str(det.fed))
    check("detector forgets what it heard at the mute", det.resets == 1, str(det.resets))
    v._on_wake_detected()
    check("a wake heard as the mute was pressed is ignored", not v._awake)
    v.ui.muted = False
    check("unmuted block goes to the detector", v._wake_gate(b"x") and det.fed == 1,
          str(det.fed))
    check("detector starts fresh on unmute", det.resets == 2, str(det.resets))
    v._wake_gate(b"x")
    check("no reset on every block", det.resets == 2, str(det.resets))
    v._on_wake_detected()
    check("unmuted wake word wakes it", v._awake)
    check("awake blocks pass the gate", not v._wake_gate(b"x"))


async def amain() -> int:
    saved = (main.save_wake_word_enabled, main.wake_is_ready, main.get_idle_sleep_minutes)
    try:
        for t in (test_declared, test_wake_word_on, test_waits_for_speech,
                  test_wake_word_off_but_installed, test_push_to_talk_ends_one_sleep,
                  test_not_set_up, test_detector_fails, test_enable_while_one_sleep,
                  test_idle_sleeps_after_an_hour, test_idle_other_lengths,
                  test_idle_what_counts, test_idle_not_set_up,
                  test_wake_word_on_keeps_two_minutes, test_idle_setting,
                  test_muted_mic_does_not_wake):
            await t()
    finally:
        (main.save_wake_word_enabled, main.wake_is_ready,
         main.get_idle_sleep_minutes) = saved
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
