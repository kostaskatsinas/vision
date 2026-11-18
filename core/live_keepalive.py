"""core/live_keepalive.py - keep the Gemini Live session open: quiet stretches and GoAway.

What went wrong
---------------
Gemini Live closes a session the client has not sent anything on for a while,
with websocket close 1008 "Session closed due to client inactivity". Vision
often sends nothing for long stretches, on purpose:

- asleep (wake word, auto-sleep, "go to sleep"): the microphone never goes to
  Gemini, so nothing leaves the machine;
- push-to-talk with the key up, the mute button, the phone relay taking over;
- while Vision itself talks, and while a tool runs.

When a routine, a timer or a background check then made Vision speak or call a
tool, the server had already given up on the session and the next read raised
the 1008 out of the receive loop as a two-page traceback.

What this does
--------------
`_send_realtime` in main.py waits for the next microphone block with a time
limit. When nothing has gone out for `KEEPALIVE_AFTER` seconds it sends
`silence()` instead: a tenth of a second of digital zeros. That is not the
microphone, so the privacy promise of sleep holds, and voice activity detection
never hears speech in it, so it starts no turn and interrupts nothing.

If the server closes for inactivity anyway, `is_inactivity_close` lets the
receive loop and the run loop recognise it and reconnect at once with one line
in the log, keeping the conversation through the resumption handle.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

# Seconds without anything sent before a silent block goes out. The server's
# limit is not documented; this is far below anything it could reasonably be,
# and costs ~2.5 audio tokens every 5 seconds of quiet.
KEEPALIVE_AFTER = 5.0

# Length of one silent block in milliseconds.
SILENCE_MS = 100


def silence(rate: int = 16000, ms: int = SILENCE_MS) -> bytes:
    """`ms` milliseconds of 16-bit mono PCM silence at `rate` Hz."""
    return bytes(2 * (rate * ms // 1000))


async def next_or_none(queue: "asyncio.Queue[Any]", timeout: float) -> Optional[Any]:
    """The next item of `queue`, or None if none arrives within `timeout`."""
    try:
        return await asyncio.wait_for(queue.get(), timeout=max(0.0, timeout))
    except asyncio.TimeoutError:
        return None


def _close_code(exc: BaseException) -> Optional[int]:
    """The websocket close code an exception carries, if any."""
    val = getattr(exc, "code", None)        # google.genai APIError
    if isinstance(val, int):
        return val
    for attr in ("rcvd", "sent"):           # websockets.ConnectionClosed
        frame = getattr(exc, attr, None)
        val = getattr(frame, "code", None)
        if isinstance(val, int):
            return val
    return None


def _closed_with(exc: BaseException, words: tuple[str, ...],
                 _seen: Optional[set] = None) -> bool:
    """True if `exc` is, wraps or was caused by a 1008 close whose reason holds
    every one of `words`. Looks inside exception groups (the session's
    TaskGroup bundles the receive loop's error into one) and along
    __cause__ / __context__ (the SDK re-raises the websocket error as an
    APIError)."""
    if exc is None:
        return False
    seen = _seen if _seen is not None else set()
    if id(exc) in seen:
        return False
    seen.add(id(exc))

    if isinstance(exc, BaseExceptionGroup):
        if any(_closed_with(e, words, seen) for e in exc.exceptions):
            return True
    else:
        text = str(exc).lower()
        if all(w in text for w in words) and (
                _close_code(exc) == 1008 or "1008" in text):
            return True

    return (_closed_with(exc.__cause__, words, seen)
            or _closed_with(exc.__context__, words, seen))


def is_inactivity_close(exc: BaseException) -> bool:
    """The server closed the session for client inactivity (1008)."""
    return _closed_with(exc, ("inactivity",))


def is_goaway_close(exc: BaseException) -> bool:
    """The server ended the connection because its time was up and the client
    had not reconnected after the GoAway warning (1008)."""
    return _closed_with(exc, ("goaway",))


def is_expected_close(exc: BaseException) -> bool:
    """A server close that is routine, not a fault: reconnect at once, keep the
    conversation, no traceback."""
    return is_inactivity_close(exc) or is_goaway_close(exc)


# ── GoAway: the connection's time is nearly up ──────────────────────────────
#
# A Live connection lasts about ten minutes. Context window compression and
# the resumption handle let the *conversation* go on for hours, but each
# connection still ends: the server sends GoAway with the time left, and a
# client that has not reconnected by then is cut off with 1008. Vision used to
# ignore GoAway, so every long session ended in that error.

# Seconds kept in hand before the server's deadline when Vision is busy and
# waits for a quiet moment to reconnect.
GOAWAY_MARGIN = 5.0

# Used when the server gives no usable time.
GOAWAY_DEFAULT = 10.0


def parse_time_left(value: Any) -> float:
    """Seconds from GoAway's time_left: a protobuf Duration as JSON ("50s",
    "1.5s"), a number, or a timedelta. GOAWAY_DEFAULT when unreadable."""
    if value is None:
        return GOAWAY_DEFAULT
    secs = getattr(value, "total_seconds", None)
    if callable(secs):
        try:
            return max(0.0, float(secs()))
        except Exception:  # noqa: BLE001
            return GOAWAY_DEFAULT
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    text = str(value).strip().lower()
    if text.endswith("s"):
        text = text[:-1]
    try:
        return max(0.0, float(text))
    except ValueError:
        return GOAWAY_DEFAULT
