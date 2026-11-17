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


async def amain() -> int:
    saved = (main.save_wake_word_enabled, main.wake_is_ready)
    try:
        for t in (test_declared, test_wake_word_on, test_waits_for_speech,
                  test_wake_word_off_but_installed, test_push_to_talk_ends_one_sleep,
                  test_not_set_up, test_detector_fails, test_enable_while_one_sleep):
            await t()
    finally:
        main.save_wake_word_enabled, main.wake_is_ready = saved
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
