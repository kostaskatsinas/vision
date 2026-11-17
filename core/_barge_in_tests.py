"""
Tests for interrupting Vision by talking over it (barge-in).
Run with:  python -m core._barge_in_tests

Kostas asked: "when I start speaking he must stop and listen". Before this,
the microphone was ignored completely while Vision talked, so the only way to
stop it was the interrupt button.

These build the assistant object without starting it (no window, microphone,
model or network) and drive the microphone callback's barge-in check, the
interrupt itself and the receive loop's handling of what comes after.

The echo half uses synthetic voices: a vowel-like buzz through formant filters,
played "out of the speakers" and heard back by the "microphone" delayed, quieter
and with room noise, with or without a second, different voice on top. What
these cannot show is Kostas's real room, speakers and microphone; that is
checked on the PC, and CONTROLS -> INTERRUPT BY VOICE turns it off if Vision
keeps cutting itself off.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

import main
from core import identity
from core.echo import EchoGuard
from core.viseme import VisemeStream
from providers.base import Msg
from providers.engine_pipeline import PipelineEngine

_passed = 0
_failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeUI:
    muted = False

    def __init__(self):
        self.states, self.log = [], []

    def set_state(self, s):
        self.states.append(s)

    def write_log(self, t):
        self.log.append(t)

    def set_audio_level(self, _lvl):
        pass


class NowLoop:
    """Runs call_soon_threadsafe callbacks at once, like the loop would soon."""

    def __init__(self):
        self.calls = 0

    def call_soon_threadsafe(self, fn, *a):
        self.calls += 1
        fn(*a)


class FakeEngine:
    def __init__(self):
        self.interrupts = 0

    def interrupt(self):
        self.interrupts += 1


class NoAEC:
    def due(self, every=20.0):
        return False


def make(guard=None) -> main.VisionLive:
    v = main.VisionLive.__new__(main.VisionLive)
    v.ui = FakeUI()
    v._speaking_lock = threading.Lock()
    v._is_speaking = False
    v._interrupted = False
    v._reply_open = False
    v._engine = None
    v._loop = None
    v._phone_active = False
    v._ptt_enabled = False
    v._ptt_held = False
    v._wake_enabled = False
    v._awake = True
    v._barge_in_on = True
    v._barge_run = 0
    v._barge_pending = False
    v._barge_preroll = main.deque(maxlen=main._BARGE_PREROLL_COLD)
    v._echo = guard or EchoGuard()
    v._aec = NoAEC()
    v._aec_said = ""
    v._vision_close_pending = False
    v._vision_busy = False
    v._last_out_logged = ""
    v._session_log = []
    v._dashboard = None
    v._asst_name = "Vision"
    v._out_level = 0.0
    v._out_latency = 0.2
    v._tail_until = 0.0
    v._play_cursor = 0.0
    v._visemes = VisemeStream()
    v._last_user_speech = 0.0
    v._last_activity = 0.0
    v.audio_in_queue = asyncio.Queue()
    v.out_queue = asyncio.Queue(maxsize=200)
    v._turn_done_event = None
    v._rest_state = lambda: "LISTENING"
    v._note_user_activity = lambda: None
    return v


def speak(v) -> None:
    """Start a reply the way the playback loop does, without reading config."""
    orig = main.get_barge_in_enabled
    main.get_barge_in_enabled = lambda: v._barge_in_on
    try:
        v.set_speaking(True)
    finally:
        main.get_barge_in_enabled = orig


# ── synthetic voices ─────────────────────────────────────────────────────────

_rng = np.random.default_rng(7)


def _resonate(x: np.ndarray, sr: int, f: float, bw: float) -> np.ndarray:
    r = np.exp(-np.pi * bw / sr)
    a1, a2 = -2 * r * np.cos(2 * np.pi * f / sr), r * r
    y = np.zeros_like(x)
    for n in range(len(x)):
        y[n] = x[n] - a1 * (y[n - 1] if n else 0) - a2 * (y[n - 2] if n > 1 else 0)
    return y


def voice(seconds: float, sr: int, f0: float, vowels) -> np.ndarray:
    """A buzz at pitch f0 shaped into a sequence of vowels, peak about 0.5."""
    n = int(seconds * sr)
    out = np.zeros(n)
    seg = n // len(vowels)
    for i, formants in enumerate(vowels):
        t = np.arange(seg) / sr
        wobble = 1 + 0.03 * np.sin(2 * np.pi * 5 * t)
        phase = np.cumsum(f0 * wobble / sr)
        src = (np.diff(np.floor(phase), prepend=0) > 0).astype(float)
        x = src
        for f, bw in formants:
            x = _resonate(x, sr, f, bw)
        out[i * seg:(i + 1) * seg] = x
    out /= max(1e-9, np.abs(out).max())
    return 0.5 * out


VISION_VOWELS = [[(700, 90), (1200, 110), (2600, 160)],
                 [(400, 80), (2000, 120), (2800, 170)],
                 [(500, 90), (900, 100), (2500, 160)]]
KOSTAS_VOWELS = [[(300, 70), (2300, 120), (3000, 180)],
                 [(650, 90), (1700, 120), (2700, 170)],
                 [(350, 80), (800, 100), (2300, 150)]]


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    n = int(len(x) * sr_out / sr_in)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)


class ClockGuard(EchoGuard):
    """EchoGuard on the simulation's clock instead of the wall clock."""
    now = 0.0

    def is_user_speech(self, pcm, sr, level, when=None):
        return super().is_user_speech(pcm, sr, level, when=self.now)


def run_room(v, seconds: float, echo_gain: float, user_from: float | None,
             user_gain: float = 0.6, noise: float = 0.01, delay: float = 0.03):
    """Play a Vision reply for `seconds` and feed the mic what the room returns.

    Returns the simulated time of the first barge-in, or None.
    """
    g: ClockGuard = v._echo
    out = voice(seconds, 24000, 120, VISION_VOWELS * 3)
    heard = resample(out, 24000, 16000) * echo_gain
    d = int(delay * 16000)
    heard = np.concatenate([np.zeros(d), heard])[:len(heard)]
    if user_from is not None:
        start = int(user_from * 16000)
        u = voice(seconds - user_from, 16000, 210, KOSTAS_VOWELS * 2) * user_gain
        heard[start:start + len(u)] += u[:len(heard) - start]
    heard += _rng.normal(0, noise, len(heard))

    speak(v)
    loop = NowLoop()
    batch = 4800                      # 200 ms at 24 kHz, as _play_audio writes
    block = main.CHUNK_SIZE           # 64 ms at 16 kHz, as the mic delivers
    next_out = 0
    for i in range(0, len(heard) - block, block):
        t = i / 16000
        while next_out * batch / 24000 <= t + 0.2 and next_out * batch < len(out):
            seg = out[next_out * batch:(next_out + 1) * batch]
            pcm = (seg * 32767).astype(np.int16)
            g.note_output(pcm, 24000, main._pcm_level(pcm), when=next_out * batch / 24000)
            next_out += 1
        g.now = t
        mic = np.clip(heard[i:i + block] * 32767, -32768, 32767).astype(np.int16)
        with v._speaking_lock:
            speaking = v._is_speaking
        if not speaking:
            return t
        v._barge_in_check(mic.reshape(-1, 1), loop)
    return None


# ── tests ────────────────────────────────────────────────────────────────────

# Echo gain is how loud our own voice comes back into the mic, relative to how
# it was played. 0.8 is open speakers turned up next to the mic. 0.02 to 0.1 is
# what is left after the desktop's echo canceller (core/aec.py), or a mic that
# is far from the speakers. Measured here with the same synthetic voices: on
# open speakers the mic-only check cannot pick a person out at all, which is
# exactly why the canceller is put in front of it. What matters there is that
# it never stops on its own echo.

def test_echo_alone_never_interrupts():
    for noise in (0.001, 0.003, 0.01):
        for gain in (0.02, 0.1, 0.25, 0.4, 0.8):
            v = make(ClockGuard())
            # Several replies in a row: the guard keeps what it learned.
            hits = [run_room(v, 3.0, gain, None, noise=noise) for _ in range(3)]
            v.set_speaking(False)
            check(f"echo only (gain {gain}, noise {noise}) never interrupts",
                  all(h is None for h in hits), str(hits))


def test_talking_over_interrupts_quickly():
    for noise in (0.001, 0.003, 0.01):
        for gain in (0.02, 0.05, 0.1):
            for loudness in (0.3, 0.6):
                v = make(ClockGuard())
                run_room(v, 3.0, gain, None, noise=noise)   # learns the room
                v.set_speaking(False)
                hit = run_room(v, 3.0, gain, 1.0, user_gain=loudness, noise=noise)
                what = f"gain {gain}, noise {noise}, voice {loudness}"
                check(f"talking over quiet echo interrupts ({what})",
                      hit is not None)
                if hit is not None:
                    check(f"...within a second ({what})", 1.0 <= hit <= 2.0,
                          f"at {hit:.2f}s")
                check(f"...and Vision stops speaking ({what})", not v._is_speaking)
                check(f"...and logs it ({what})",
                      any("Interrupted" in l for l in v.ui.log))


def test_headphones_interrupt():
    v = make(ClockGuard())
    run_room(v, 3.0, 0.0, None, noise=0.002)
    v.set_speaking(False)
    hit = run_room(v, 3.0, 0.0, 1.0, noise=0.002)
    check("no echo (headphones): talking interrupts", hit is not None and hit < 1.6,
          str(hit))


def test_preroll_reaches_the_model():
    v = make(ClockGuard())
    run_room(v, 3.0, 0.05, None)
    v.set_speaking(False)
    while not v.out_queue.empty():
        v.out_queue.get_nowait()
    run_room(v, 3.0, 0.05, 1.0)
    n = v.out_queue.qsize()
    check("the words that triggered it are sent to the model",
          1 <= n <= main._BARGE_PREROLL_BLOCKS, f"{n} blocks")
    check("the echo tail is ended so the rest of the sentence is not dropped",
          v._tail_until == 0.0)


def test_mic_blocks_are_fingerprinted_as_audio():
    from core.echo import band_energies
    x = (voice(0.064, 16000, 200, KOSTAS_VOWELS[:1]) * 20000).astype(np.int16)
    flat = band_energies(x, 16000)
    as_mic = band_energies(x.reshape(-1, 1), 16000)
    check("a (frames, 1) mic block gives the same fingerprint as flat audio",
          np.allclose(flat, as_mic), f"{flat} vs {as_mic}")


def test_nothing_recorded_yet_is_not_a_voice():
    v = make(EchoGuard())
    speak(v)
    blk = (voice(0.064, 16000, 200, KOSTAS_VOWELS[:1]) * 20000).astype(np.int16)
    loop = NowLoop()
    for _ in range(10):
        v._barge_in_check(blk.reshape(-1, 1), loop)
    check("before the first slice of a reply is recorded nothing interrupts",
          v._is_speaking and loop.calls == 0)


class ScriptedGuard:
    """Says 'voice' for the blocks it is told to, nothing else."""
    floor, threshold, required_blocks = 0.1, 0.15, 5
    warm = True

    def __init__(self, answers):
        self.answers = list(answers)
        self._hist = [1]

    def is_user_speech(self, *a, **k):
        return self.answers.pop(0) if self.answers else False


def _blocks(v, n):
    loop = NowLoop()
    blk = np.zeros((main.CHUNK_SIZE, 1), dtype=np.int16)
    for _ in range(n):
        v._barge_in_check(blk, loop)
    return loop


def test_needs_sustained_voice():
    v = make(ScriptedGuard([True] * 4 + [False] + [True] * 4))
    speak(v)
    _blocks(v, 9)
    check("a voice broken up before 5 blocks does not interrupt", v._is_speaking)
    v = make(ScriptedGuard([True] * 5))
    speak(v)
    _blocks(v, 5)
    check("5 blocks in a row interrupt", not v._is_speaking)


def test_switches():
    for what, setup in (
        ("setting off", lambda v: setattr(v, "_barge_in_on", False)),
        ("muted", lambda v: setattr(v.ui, "muted", True)),
        ("phone mic in use", lambda v: setattr(v, "_phone_active", True)),
        ("push-to-talk on", lambda v: setattr(v, "_ptt_enabled", True)),
    ):
        v = make(ScriptedGuard([True] * 20))
        speak(v)
        setup(v)
        _blocks(v, 20)
        check(f"{what}: talking does not interrupt", v._is_speaking)


def test_setting_read_at_each_reply():
    v = make(ScriptedGuard([True] * 20))
    orig = main.get_barge_in_enabled
    try:
        main.get_barge_in_enabled = lambda: False
        v.set_speaking(True)
        check("the CONTROLS setting is picked up when a reply starts",
              v._barge_in_on is False)
        v.set_speaking(True)      # every chunk calls this; must not re-read
        main.get_barge_in_enabled = lambda: True
        v.set_speaking(True)
        check("...not on every chunk of the same reply", v._barge_in_on is False)
        v.set_speaking(False)
        v.set_speaking(True)
        check("...and the next reply sees the change", v._barge_in_on is True)
    finally:
        main.get_barge_in_enabled = orig


def test_cold_guard_keeps_longer_preroll():
    v = make(ScriptedGuard([False] * 16 + [True] * 5))
    v._echo.warm = False
    speak(v)
    loop = NowLoop()
    blk = np.zeros((main.CHUNK_SIZE, 1), dtype=np.int16)
    for i in range(21):
        if i == 16:
            v._echo.warm = True       # the guard finishes learning mid-sentence
        v._barge_in_check(blk, loop)
    check("a voice noticed just after warm-up sends the longer pre-roll",
          v.out_queue.qsize() == 21 and not v._is_speaking, str(v.out_queue.qsize()))


def test_reply_ended_meanwhile():
    v = make(ScriptedGuard([]))
    v._barge_preroll.append((np.zeros((4, 1), dtype=np.int16), True))
    v._barge_pending = True
    v._barge_in()
    check("a barge-in landing after the reply ended does nothing",
          not any("Interrupted" in l for l in v.ui.log) and v.out_queue.empty())
    check("...and clears the pending flag", v._barge_pending is False)


def test_ptt_key_interrupts():
    v = make()
    v._loop = NowLoop()
    v._ptt_enabled = True
    v._engine = None
    speak(v)
    v._on_ptt(True)
    check("holding the push-to-talk key while Vision talks stops it",
          not v._is_speaking)


# ── what happens to the rest of the reply ────────────────────────────────────

def _audio_msg():
    return Msg.audio(b"\x00\x01" * 1200)


def test_interrupt_after_server_finished_keeps_next_answer():
    v = make()
    v._queue_reply_audio(_audio_msg())
    v._finish_turn([], [])               # the server sent all of it already
    speak(v)
    v.interrupt()
    check("interrupting a reply the server already finished drops nothing more",
          v._interrupted is False)
    v._queue_reply_audio(_audio_msg())
    check("...so the answer to what the user says next is played",
          v.audio_in_queue.qsize() > 0)


def test_interrupt_mid_stream_drops_rest_until_server_says_so():
    v = make()
    v._queue_reply_audio(_audio_msg())   # reply still streaming
    speak(v)
    v.interrupt()
    check("playback queue drained", v.audio_in_queue.empty())
    check("rest of the reply will be dropped", v._interrupted is True)
    v._queue_reply_audio(_audio_msg())
    check("...and is", v.audio_in_queue.empty())
    out = ["half a sentence"]
    v._on_server_interrupted(out)
    check("server's 'interrupted' ends the dropping", v._interrupted is False)
    check("...and clears the cut-off transcript", out == [])
    v._queue_reply_audio(_audio_msg())
    check("the answer to the interruption is played",
          v.audio_in_queue.qsize() > 0)


def test_server_interrupt_stops_speakers():
    v = make()
    v._queue_reply_audio(_audio_msg())
    speak(v)
    v._on_server_interrupted([])
    check("Gemini hearing the user first also stops the speakers",
          not v._is_speaking and v.audio_in_queue.empty())


def test_local_engine_gets_stopped():
    v = make()
    v._engine = FakeEngine()
    speak(v)
    v.interrupt()
    check("interrupt cancels the local engine's generation",
          v._engine.interrupts == 1)


def test_engine_emits_interrupted_marker():
    async def go():
        eng = PipelineEngine.__new__(PipelineEngine)
        eng._queue = asyncio.Queue()
        eng._closed = False

        async def forever():
            await asyncio.sleep(60)

        eng._gen = asyncio.create_task(forever())
        await eng._queue.put(_audio_msg())
        eng.interrupt()
        first = await eng._queue.get()
        second = await eng._queue.get()
        await asyncio.sleep(0)
        return first, second, eng._gen.cancelled()

    first, second, cancelled = asyncio.run(go())
    check("engine cancels its generation", cancelled)
    check("audio already emitted stays ahead of the marker", first.data is not None)
    check("then an 'interrupted' marker, since no turn_end will come",
          second.server_content is not None and second.server_content.interrupted)

    async def idle():
        eng = PipelineEngine.__new__(PipelineEngine)
        eng._queue = asyncio.Queue()
        eng._closed = False
        eng._gen = None
        eng.interrupt()
        return eng._queue.qsize()

    check("nothing is emitted when nothing was being generated",
          asyncio.run(idle()) == 0)


# ── the desktop echo canceller (core/aec.py) ────────────────────────────────

class FakePactl:
    """Just enough of pactl's JSON for SystemAEC, with a log of commands."""

    def __init__(self, pid="4242", rec_on="1", play_on="1", alsa_names=False):
        self.pid = pid
        self.alsa_names = alsa_names
        self.cmds = []
        self.modules = []          # (index, argument)
        self.sinks = {"1": "alsa_output.speakers"}
        self.sources = {"1": "alsa_input.webcam_mic", "2": "alsa_input.headset"}
        self.input_sink = play_on
        self.output_source = rec_on
        self.fail_load = False

    def __call__(self, args):
        import json as _j
        self.cmds.append(args)
        if args[:2] == ["-f", "json"]:
            what = args[3:]
            if what == ["short", "sinks"]:
                return 0, _j.dumps([{"index": int(k), "name": v} for k, v in self.sinks.items()])
            if what == ["short", "sources"]:
                return 0, _j.dumps([{"index": int(k), "name": v} for k, v in self.sources.items()])
            if what == ["short", "modules"]:
                return 0, _j.dumps([{"index": int(i), "name": "module-echo-cancel",
                                     "argument": a} for i, a in self.modules])
            mine = {"application.process.id": self.pid}
            other = {"application.process.id": "1"}
            if self.alsa_names:
                # What Kostas's PC actually shows: PipeWire's ALSA plugin
                # gives no process id, only the program's name.
                mine = {"application.name": "PipeWire ALSA [python3.14]",
                        "node.name": "alsa_playback.python3.14"}
                mine_rec = {"application.name": "PipeWire ALSA [python3.14]",
                            "node.name": "alsa_capture.python3.14"}
                vlc = {"application.name": "VLC media player (LibVLC 3.0.24)",
                       "application.process.id": "89871"}
                if what == ["sink-inputs"]:
                    return 0, _j.dumps([
                        {"index": 1221, "sink": 1, "properties": vlc},
                        {"index": 1484, "sink": int(self.input_sink), "properties": mine}])
                if what == ["source-outputs"]:
                    return 0, _j.dumps([{"index": 1476, "source": int(self.output_source),
                                         "properties": mine_rec}])
            if what == ["sink-inputs"]:
                return 0, _j.dumps([{"index": 7, "sink": int(self.input_sink), "properties": mine},
                                    {"index": 8, "sink": 1, "properties": other}])
            if what == ["source-outputs"]:
                return 0, _j.dumps([{"index": 9, "source": int(self.output_source), "properties": mine}])
        if args[0] == "load-module":
            if self.fail_load:
                return 1, "Failure: Module initialization failed"
            idx = str(30 + len(self.modules))
            self.modules.append((idx, " ".join(args[2:])))
            self.sinks["90"] = "vision_aec_sink"
            self.sources["91"] = "vision_aec_source"
            return 0, idx + "\n"
        if args[0] == "move-sink-input":
            self.input_sink = [k for k, v in self.sinks.items() if v == args[2]][0]
        if args[0] == "move-source-output":
            self.output_source = [k for k, v in self.sources.items() if v == args[2]][0]
        if args[0] == "unload-module":
            self.modules = [m for m in self.modules if m[0] != args[1]]
            self.sinks.pop("90", None)
            self.sources.pop("91", None)
        return 0, ""


def _aec(fake, has_pactl=True):
    from core.aec import SystemAEC
    return SystemAEC(run=fake, pid=4242,
                     which=lambda n: "/usr/bin/pactl" if has_pactl else None,
                     programs=["python", "python3", "python3.14"])


def test_aec_finds_streams_named_by_the_alsa_plugin():
    f = FakePactl(alsa_names=True)
    a = _aec(f)
    check("streams with no process id are found by program name",
          a.ensure() == "on", a.detail)
    moves = [c for c in f.cmds if c[0].startswith("move-")]
    check("...and only Vision's are moved, not VLC's",
          moves == [["move-sink-input", "1484", "vision_aec_sink"],
                    ["move-source-output", "1476", "vision_aec_source"]], str(moves))
    from core.aec import _program_names
    check("this interpreter's names include python3.N",
          f"python{sys.version_info[0]}.{sys.version_info[1]}" in _program_names())


def test_aec_routes_only_visions_streams():
    f = FakePactl()
    a = _aec(f)
    check("echo canceller turns on", a.ensure() == "on", a.detail)
    loads = [c for c in f.cmds if c[0] == "load-module"]
    check("loaded once, with WebRTC", len(loads) == 1 and "aec_method=webrtc" in loads[0])
    check("on top of the mic and speaker Vision uses now",
          "source_master=alsa_input.webcam_mic" in loads[0]
          and "sink_master=alsa_output.speakers" in loads[0])
    moves = [c for c in f.cmds if c[0].startswith("move-")]
    check("only Vision's two streams are moved",
          moves == [["move-sink-input", "7", "vision_aec_sink"],
                    ["move-source-output", "9", "vision_aec_source"]], str(moves))
    n = len(f.cmds)
    check("calling again changes nothing", a.ensure() == "on"
          and not any(c[0] in ("load-module", "move-sink-input") for c in f.cmds[n:]))
    a.stop()
    check("closing Vision unloads it", f.modules == [])


def test_aec_follows_a_new_microphone():
    f = FakePactl()
    a = _aec(f)
    a.ensure()
    f.output_source = "2"          # mic stream reopened on the headset
    a.ensure()
    loads = [c for c in f.cmds if c[0] == "load-module"]
    check("switching microphone rebuilds the canceller on it",
          len(loads) == 2 and "source_master=alsa_input.headset" in loads[1])
    check("...and only one module is left", len(f.modules) == 1)


def test_aec_adopts_leftover_module():
    f = FakePactl()
    f.modules.append(("55", "aec_method=webrtc source_name=vision_aec_source "
                            "sink_name=vision_aec_sink"))
    f.sinks["90"] = "vision_aec_sink"
    f.sources["91"] = "vision_aec_source"
    a = _aec(f)
    a.ensure()
    check("a module left by a crash is reused, not doubled",
          not any(c[0] == "load-module" for c in f.cmds))
    a.stop()
    check("...and removed on close", ["unload-module", "55"] in f.cmds)


def test_aec_unavailable_paths():
    a = _aec(FakePactl(), has_pactl=False)
    check("no pactl: says so", a.ensure() == "no-pactl")
    f = FakePactl(pid="1")         # no streams belong to us
    check("streams not on the sound server: says so",
          _aec(f).ensure() == "no-streams")
    f = FakePactl()
    f.fail_load = True
    a = _aec(f)
    check("module refusing to load: says so", a.ensure() == "failed"
          and "Module initialization failed" in a.detail)
    check("...and nothing is moved",
          not any(c[0].startswith("move-") for c in f.cmds))

    def broken(args):
        return 0, "not json"
    check("old pactl without JSON: fails softly",
          _aec(broken).ensure() == "failed")


def main_() -> int:
    identity.listen = lambda block: False     # no owner voice learning here
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                global _failed
                _failed += 1
                import traceback
                traceback.print_exc()
                print(f"  FAIL  {name} raised {e!r}")
    print(f"barge-in: {_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main_())
