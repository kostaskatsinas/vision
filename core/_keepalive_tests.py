"""
Tests for keeping the Gemini Live session alive while Vision is quiet.
Run with:  python -m core._keepalive_tests

Kostas's log showed the server closing the session with websocket close 1008
"Session closed due to client inactivity", raised out of the receive loop as a
two-page ExceptionGroup traceback, then a 3 second wait before reconnecting.
Vision sends nothing to Gemini while asleep, muted, with push-to-talk up, while
it talks and while a tool runs, so the server decided the client had gone.

These check that:
- a short block of silence goes out after a few seconds with nothing sent, and
  never while the microphone is streaming or on a local engine;
- the 1008 inactivity close is recognised however it arrives (SDK APIError,
  websockets error, wrapped in the TaskGroup's group, chained);
- the receive loop says one line instead of a traceback, and the run loop
  reconnects at once keeping the conversation.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
from core import live_keepalive
from google.genai import errors as genai_errors
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

_passed = 0
_failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


INACTIVITY = "Session closed due to client inactivity."


def ws_close(code: int = 1008, reason: str = INACTIVITY) -> ConnectionClosedError:
    return ConnectionClosedError(Close(code, reason), Close(code, reason), True)


def api_error(code: int = 1008, reason: str = INACTIVITY) -> Exception:
    """The error exactly as google-genai raises it from live.py: an APIError
    raised while handling the websockets close."""
    try:
        try:
            raise ws_close(code, reason)
        except ConnectionClosedError:
            genai_errors.APIError.raise_error(code, reason, None)
    except Exception as e:  # noqa: BLE001
        return e
    raise AssertionError("raise_error did not raise")


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeUI:
    muted = False

    def __init__(self):
        self.log = []

    def write_log(self, t):
        self.log.append(t)

    def set_state(self, _s):
        pass


class FakeSession:
    def __init__(self, receive_error=None):
        self.sent = []
        self._receive_error = receive_error

    async def send_realtime_input(self, *, audio):
        self.sent.append(bytes(audio.data))

    async def receive(self):
        if self._receive_error is not None:
            raise self._receive_error
        if False:
            yield None


def make() -> main.VisionLive:
    v = main.VisionLive.__new__(main.VisionLive)
    v.ui = FakeUI()
    v._engine = None
    v._resume_handle = "handle-1"
    v._conn_backoff = 3
    v._speaking_lock = threading.Lock()
    v._is_speaking = False
    return v


# ── 1. silence block ─────────────────────────────────────────────────────────

def test_silence():
    s = live_keepalive.silence(16000)
    check("silence: 100 ms at 16 kHz is 3200 bytes", len(s) == 3200, str(len(s)))
    check("silence: all zeros", not any(s))
    check("silence: other rate", len(live_keepalive.silence(24000, 50)) == 2400)


# ── 2. recognising the close ─────────────────────────────────────────────────

def test_detection():
    f = live_keepalive.is_inactivity_close
    e = api_error()
    check("detect: SDK APIError 1008 inactivity", f(e), repr(e))
    check("detect: websockets close 1008 inactivity", f(ws_close()))
    check("detect: inside TaskGroup's ExceptionGroup",
          f(ExceptionGroup("unhandled errors in a TaskGroup", [e])))
    check("detect: nested BaseExceptionGroup",
          f(BaseExceptionGroup("outer", [ExceptionGroup("inner", [ValueError("x"), e])])))
    try:
        try:
            raise e
        except Exception as inner:
            raise RuntimeError("wrapper") from inner
    except RuntimeError as wrapped:
        check("detect: through __cause__", f(wrapped))

    check("ignore: 1008 for another reason",
          not f(api_error(1008, "Operation is not implemented, or supported, or enabled.")))
    check("ignore: 1011 internal error",
          not f(api_error(1011, "Internal error encountered.")))
    check("ignore: plain error", not f(ValueError("boom")))
    check("ignore: None", not f(None))
    check("ignore: group of other errors",
          not f(ExceptionGroup("g", [ValueError("a"), KeyError("b")])))


# ── 3. keepalive in _send_realtime ───────────────────────────────────────────

async def _run_sender(v, seconds, feed=None):
    v.out_queue = asyncio.Queue()
    task = asyncio.create_task(v._send_realtime())
    if feed is not None:
        await feed(v.out_queue)
    else:
        await asyncio.sleep(seconds)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_keepalive():
    old = live_keepalive.KEEPALIVE_AFTER
    live_keepalive.KEEPALIVE_AFTER = 0.05
    try:
        # Quiet: silence goes out on its own.
        v = make()
        v.session = FakeSession()
        asyncio.run(_run_sender(v, 0.28))
        n = len(v.session.sent)
        check("keepalive: quiet session gets silence", 3 <= n <= 7, str(n))
        check("keepalive: what goes out is silence",
              all(not any(b) and len(b) == 3200 for b in v.session.sent))

        # Microphone streaming every 20 ms: only the mic goes out.
        v = make()
        v.session = FakeSession()
        mic = b"\x01\x02" * 512

        async def feed(q):
            for _ in range(15):
                q.put_nowait({"data": mic, "mime_type": "audio/pcm"})
                await asyncio.sleep(0.02)

        asyncio.run(_run_sender(v, 0, feed))
        check("keepalive: no silence while the mic streams",
              v.session.sent and all(b == mic for b in v.session.sent),
              str([len(b) for b in v.session.sent]))

        # Mic stops: silence follows after the limit.
        v = make()
        v.session = FakeSession()

        async def feed_then_stop(q):
            q.put_nowait({"data": mic, "mime_type": "audio/pcm"})
            await asyncio.sleep(0.2)

        asyncio.run(_run_sender(v, 0, feed_then_stop))
        sent = v.session.sent
        check("keepalive: mic first, then silence",
              sent and sent[0] == mic and len(sent) >= 2
              and all(not any(b) for b in sent[1:]), str(len(sent)))

        # Local engine: nothing extra.
        v = make()
        v._engine = object()
        v.session = FakeSession()
        asyncio.run(_run_sender(v, 0.2))
        check("keepalive: local engine gets no silence", v.session.sent == [])
    finally:
        live_keepalive.KEEPALIVE_AFTER = old


# ── 4. receive loop and run loop ─────────────────────────────────────────────

def test_receive_and_recover():
    v = make()
    v.session = FakeSession(receive_error=api_error())
    out, err = io.StringIO(), io.StringIO()
    raised = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            asyncio.run(v._receive_audio())
        except Exception as e:  # noqa: BLE001
            raised = e
    check("recv: still raises so the session is rebuilt", raised is not None)
    check("recv: one line, no traceback",
          "Traceback" not in err.getvalue() and "Traceback" not in out.getvalue()
          and "inactivity" in out.getvalue(), out.getvalue() + err.getvalue())

    # Another error still gets its traceback.
    v = make()
    v.session = FakeSession(receive_error=api_error(1011, "Internal error encountered."))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        with contextlib.suppress(Exception):
            asyncio.run(v._receive_audio())
    check("recv: other errors keep their traceback", "Traceback" in err.getvalue())

    # Run loop: reconnect now, keep the conversation, no traceback.
    v = make()
    group = ExceptionGroup("unhandled errors in a TaskGroup", [api_error()])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        again = asyncio.run(v._recover(group, lambda: "models/x", lambda: True))
    check("recover: reconnect at once", again is True and v._conn_backoff == 0,
          f"{again} {v._conn_backoff}")
    check("recover: resumption handle kept", v._resume_handle == "handle-1")
    check("recover: no traceback", "Traceback" not in err.getvalue(), err.getvalue())


def main_() -> int:
    test_silence()
    test_detection()
    test_keepalive()
    test_receive_and_recover()
    print(f"keepalive tests: {_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main_())
