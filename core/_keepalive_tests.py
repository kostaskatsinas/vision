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


# ── 5. GoAway: the connection's time limit ──────────────────────────────────

GOAWAY = ("Connection aborted because the client failed to close the connection "
          "after receiving a GoAway signal once the session durat")


class GoAwayMsg:
    """A server message that carries only GoAway, like the SDK's."""
    def __init__(self, time_left="50s"):
        self.go_away = type("GA", (), {"time_left": time_left})()
        self.server_content = None
        self.tool_call = None
        self.session_resumption_update = None
        self.data = None


class GoAwaySession(FakeSession):
    def __init__(self, msg):
        super().__init__()
        self._msg = msg

    async def receive(self):
        yield self._msg
        raise asyncio.CancelledError


def make_goaway() -> main.VisionLive:
    v = make()
    v._reply_open = False
    v._tool_busy = False
    v._goaway_deadline = None
    v.audio_in_queue = None
    v.reconnects = []
    v.request_reconnect = lambda keep_context=True, reason="": v.reconnects.append(
        (keep_context, reason, main.time.monotonic()))
    return v


def test_goaway():
    p = live_keepalive.parse_time_left
    check("time_left: '50s'", p("50s") == 50.0)
    check("time_left: '1.5s'", p("1.5s") == 1.5)
    check("time_left: number", p(7) == 7.0)
    import datetime as _dt
    check("time_left: timedelta", p(_dt.timedelta(seconds=12)) == 12.0)
    check("time_left: missing -> default", p(None) == live_keepalive.GOAWAY_DEFAULT)
    check("time_left: junk -> default", p("soon") == live_keepalive.GOAWAY_DEFAULT)

    e = api_error(1008, GOAWAY)
    check("detect: GoAway 1008 close (Kostas's log)", live_keepalive.is_goaway_close(e))
    check("detect: GoAway inside TaskGroup group",
          live_keepalive.is_expected_close(ExceptionGroup("g", [e])))
    check("detect: GoAway is not inactivity", not live_keepalive.is_inactivity_close(e))
    check("detect: inactivity still expected",
          live_keepalive.is_expected_close(api_error()))

    # The receive loop notices GoAway.
    v = make_goaway()
    v.session = GoAwaySession(GoAwayMsg("50s"))
    v._clear_stall = lambda: None
    v._note_resumption = lambda r: None
    v._queue_reply_audio = lambda r: None
    with contextlib.redirect_stdout(io.StringIO()):
        with contextlib.suppress(BaseException):
            asyncio.run(v._receive_audio())
    left = (v._goaway_deadline or 0) - main.time.monotonic()
    check("recv: GoAway sets a deadline ~50 s out", 45 < left <= 50, f"{left:.1f}")

    # Quiet: reconnect almost at once, keeping the conversation.
    v = make_goaway()
    with contextlib.redirect_stdout(io.StringIO()):
        v._on_go_away(GoAwayMsg("50s").go_away)

    async def run_watch(v, limit):
        try:
            await asyncio.wait_for(v._watch_goaway(), limit)
        except asyncio.TimeoutError:
            pass

    t0 = main.time.monotonic()
    asyncio.run(run_watch(v, 2.0))
    check("goaway: quiet -> reconnect within a second",
          len(v.reconnects) == 1 and v.reconnects[0][2] - t0 < 1.0, str(v.reconnects))
    check("goaway: keeps the conversation, renewal reason",
          v.reconnects and v.reconnects[0][:2] == (True, main._RENEWAL))

    # Busy (tool running, Vision talking): wait, but not past the deadline.
    for busy in ("tool", "speaking", "reply"):
        v = make_goaway()
        if busy == "tool":
            v._tool_busy = True
        elif busy == "speaking":
            v._is_speaking = True
        else:
            v._reply_open = True
        with contextlib.redirect_stdout(io.StringIO()):
            v._on_go_away(GoAwayMsg(f"{live_keepalive.GOAWAY_MARGIN + 1.0}s").go_away)
        t0 = main.time.monotonic()
        asyncio.run(run_watch(v, 3.0))
        took = v.reconnects[0][2] - t0 if v.reconnects else -1
        check(f"goaway: busy ({busy}) waits, then goes before the deadline",
              len(v.reconnects) == 1 and 0.6 < took < 1.6, f"{took:.2f}")

    # A second GoAway does not push the deadline back.
    v = make_goaway()
    with contextlib.redirect_stdout(io.StringIO()):
        v._on_go_away(GoAwayMsg("10s").go_away)
        first = v._goaway_deadline
        v._on_go_away(GoAwayMsg("50s").go_away)
    check("goaway: first deadline kept", v._goaway_deadline == first)

    # The renewal itself is quiet in the activity log.
    v = make_goaway()
    v._reconnect_event = None

    async def renew():
        v._reconnect_event = asyncio.Event()
        v._reconnect_keep = True
        v._reconnect_reason = main._RENEWAL
        v._reconnect_event.set()
        try:
            await v._watch_reconnect()
        except main._ReconnectSignal as sig:
            return sig

    with contextlib.redirect_stdout(io.StringIO()):
        sig = asyncio.run(renew())
    check("renewal: raises a keep-context reconnect",
          isinstance(sig, main._ReconnectSignal) and sig.keep_context)
    check("renewal: nothing in the activity log", v.ui.log == [], str(v.ui.log))
    check("renewal: next connect is quiet", getattr(v, "_quiet_reconnect", False))

    # If the deadline is missed anyway: one line, reconnect at once.
    v = make()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        again = asyncio.run(v._recover(ExceptionGroup("g", [e]),
                                       lambda: "models/x", lambda: True))
    check("recover: GoAway close reconnects at once, no traceback",
          again is True and v._conn_backoff == 0 and "Traceback" not in err.getvalue())


def main_() -> int:
    test_silence()
    test_detection()
    test_keepalive()
    test_receive_and_recover()
    test_goaway()
    print(f"keepalive tests: {_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main_())
