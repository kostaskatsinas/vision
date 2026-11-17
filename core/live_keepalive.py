"""core/live_keepalive.py - keep the Gemini Live session from timing out.

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


def is_inactivity_close(exc: BaseException, _seen: Optional[set] = None) -> bool:
    """True if `exc` is, wraps or was caused by the server closing the session
    for client inactivity. Looks inside exception groups (the session's
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
        if any(is_inactivity_close(e, seen) for e in exc.exceptions):
            return True
    else:
        text = str(exc).lower()
        if "inactivity" in text and (_close_code(exc) == 1008 or "1008" in text):
            return True

    return (is_inactivity_close(exc.__cause__, seen)
            or is_inactivity_close(exc.__context__, seen))
