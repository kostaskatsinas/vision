"""
Local wake-word detection for VISION ("Vision" or "Hey Vision").

Design goals:
  • ZERO cost when the feature is off — sherpa_onnx is imported ONLY inside
    start()/install helpers, never at module load. If the user never enables
    wake word, none of this touches the app.
  • ZERO latency on the audio path — the microphone callback only ever does a
    cheap, non-blocking queue push (feed()); the actual model inference runs in
    this module's own background thread, so the real-time audio thread and the
    Gemini stream are never slowed.
  • Fully local & offline — audio fed here never leaves the machine; there is no
    network call except the one-time model download the user triggers from the UI.

The engine is sherpa-onnx's open-vocabulary keyword spotter: a 3 MB streaming
speech model that listens for whatever phrases it is given as text, so the
wake phrase needs no trained model of its own. (Upstream used openWakeWord,
whose pretrained model only knows its own phrase; a new phrase there means
training a model.)

"Vision" on its own is a short word that hides inside longer ones. Measured on
synthetic speech from several hundred voices, a spotter given only "Vision"
woke on every "television", "division" and "revision". So the spotter is also
given those longer words as DECOYS: it hears them as themselves, and a decoy
never wakes the assistant. With the decoys in place the same test woke on
about 95% of "Vision" / "Hey Vision" and on none of the ordinary English or
Greek sentences; what is left are occasional wakes on sentences packed with
"-ision" words. "Hey Vision" is the sturdier phrase if that ever gets annoying.
"""
from __future__ import annotations

import hashlib
import queue
import subprocess
import sys
import tarfile
import threading
import urllib.request
from pathlib import Path
from typing import Callable

# What the user says. Shown in the UI and the log.
WAKE_PHRASE = "Vision"

# Each line is the phrase spelled in the model's own word pieces (its BPE
# vocabulary, see tokens.txt beside the model). They are written out here so
# the app needs no tokenizer package at run time.
_WAKE_KEYWORDS = (
    "▁VI S ION",            # vision
    "▁HE Y ▁VI S ION",      # hey vision
)
# Longer words that contain "vision" or sound close to it. Heard, then ignored.
_DECOY_KEYWORDS = (
    "▁T E LE VI S ION",     # television
    "▁DI VI S ION",         # division
    "▁RE VI S ION",         # revision
    "▁PRO VI S ION",        # provision
    "▁SU PER VI S ION",     # supervision
    "▁EN VI S ION",         # envision
    "▁SU B DI VI S ION",    # subdivision
    "▁DE CI S ION",         # decision
    "▁ VER S ION",          # version
    "▁MI S S ION",          # mission
    "▁PRE CI S ION",        # precision
    "▁COL LI S ION",        # collision
    "▁IN CI S ION",         # incision
    "▁FI S S ION",          # fission
    "▁PH Y S IC IAN",       # physician
)
_WAKE_LABEL  = "WAKE"
_DECOY_LABEL = "DECOY"

# How sure the spotter must be, in [0,1]. Lower wakes more easily (and more
# often by mistake); raise it if the assistant wakes when nobody called it.
DEFAULT_THRESHOLD = 0.1
_DECOY_THRESHOLD  = 0.25
# Mic frames arrive at 16 kHz int16; this is just the detector's input rate.
SAMPLE_RATE = 16000

# The model: about 17 MB to download, 5 MB kept. Pinned by checksum, so what is
# unpacked is exactly the file this code was tested against.
_MODEL_NAME   = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
_MODEL_URL    = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                 f"kws-models/{_MODEL_NAME}.tar.bz2")
_MODEL_SHA256 = "f170013b4716e41b62b9bfd809687c207cef798ef9bc6534d524e17af9b6561a"
_MODEL_FILES  = {
    "encoder": "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "decoder": "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "joiner":  "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "tokens":  "tokens.txt",
}
MODEL_DIR = Path(__file__).resolve().parent.parent / "config" / "wake_word"


def is_installed() -> bool:
    """True if the sherpa_onnx package is importable (no model check)."""
    try:
        import importlib.util
        return importlib.util.find_spec("sherpa_onnx") is not None
    except Exception:
        return False


def _model_present() -> bool:
    try:
        return all((MODEL_DIR / name).is_file() and (MODEL_DIR / name).stat().st_size > 0
                   for name in _MODEL_FILES.values())
    except Exception:
        return False


def is_ready() -> bool:
    """True if sherpa_onnx is installed AND the model files are present on disk.

    This is a cheap, DETERMINISTIC file-existence check. It deliberately does NOT
    load the model to probe readiness — doing that is slow and can clash with the
    detector's own model when it's already running. Never raises.
    """
    return is_installed() and _model_present()


def _keywords_text(threshold: float) -> str:
    lines = [f"{k} :1.0 #{threshold} @{_WAKE_LABEL}" for k in _WAKE_KEYWORDS]
    lines += [f"{k} :1.0 #{_DECOY_THRESHOLD} @{_DECOY_LABEL}" for k in _DECOY_KEYWORDS]
    return "\n".join(lines) + "\n"


def _download_model(logger: Callable[[str], None]) -> None:
    """Fetch the model archive, check it, and keep the four files that are used.
    Raises on any failure; the caller turns that into a message."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    archive = MODEL_DIR / "model.tar.bz2.part"
    try:
        digest = hashlib.sha256()
        with urllib.request.urlopen(_MODEL_URL, timeout=60) as resp, open(archive, "wb") as out:
            while True:
                block = resp.read(1 << 16)
                if not block:
                    break
                digest.update(block)
                out.write(block)
        if digest.hexdigest() != _MODEL_SHA256:
            raise RuntimeError("the downloaded model did not match its checksum")
        wanted = {f"{_MODEL_NAME}/{name}": name for name in _MODEL_FILES.values()}
        with tarfile.open(archive, "r:bz2") as tar:
            for member in tar:
                name = wanted.get(member.name)
                if name is None or not member.isfile():
                    continue
                src = tar.extractfile(member)
                if src is None:
                    continue
                # Written under our own fixed names, never the archive's paths.
                tmp = MODEL_DIR / (name + ".part")
                with src, open(tmp, "wb") as dst:
                    while True:
                        block = src.read(1 << 16)
                        if not block:
                            break
                        dst.write(block)
                tmp.replace(MODEL_DIR / name)
    finally:
        try:
            archive.unlink()
        except OSError:
            pass


def install_and_download(logger: Callable[[str], None] = print,
                         notify: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """
    One-click setup for the UI button: pip-install sherpa-onnx if missing, then
    download the wake model. Returns (ok, message). Never raises — every failure
    is reported through the returned message and the logger.
    """
    _tell = notify or (lambda _msg: None)
    try:
        if not is_installed():
            logger("Wake word: installing sherpa-onnx (one-time)…")
            _tell("Wake word: installing sherpa-onnx (one-time)…")
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "sherpa-onnx"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if r.returncode != 0:
                tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
                return False, f"pip install failed: {tail[0][:160]}"
            import importlib
            importlib.invalidate_caches()
        if not _model_present():
            logger("Wake word: downloading the model (17 MB)…")
            _tell("Wake word: downloading the model (17 MB)…")
            try:
                _download_model(logger)
            except Exception as e:
                return False, f"model download failed: {e}"

        if not is_ready():
            return False, "installed, but the wake model could not be found."
        logger("Wake word: ready.")
        return True, "Wake word installed and ready."
    except Exception as e:
        return False, f"setup error: {e}"


class WakeWordDetector:
    """
    Runs the wake model in a dedicated thread. The mic thread calls feed() with
    raw int16 frames; detections invoke on_detect() (called from this thread —
    the callback must marshal to whatever loop/UI it needs).
    """

    def __init__(self, on_detect: Callable[[], None],
                 threshold: float = DEFAULT_THRESHOLD,
                 logger: Callable[[str], None] = print,
                 notify: Callable[[str], None] | None = None):
        self._on_detect = on_detect
        self._threshold = threshold
        self._logger    = logger
        # See PluginRegistry: `logger` is the console and gets everything,
        # `notify` is the activity log and gets only what the user must act on.
        self._notify    = notify or (lambda _msg: None)
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._thread: threading.Thread | None = None
        self._running = False
        self._model = None
        self._stream = None
        self._ready = False
        self._reset_pending = False

    def start(self) -> bool:
        """Load the model and spawn the inference thread. Returns True on success.
        Safe to call again — a no-op if already running. Never raises."""
        if self._running:
            return True
        try:
            import sherpa_onnx
            keywords = MODEL_DIR / "keywords.txt"
            keywords.write_text(_keywords_text(self._threshold), encoding="utf-8")
            self._model = sherpa_onnx.KeywordSpotter(
                tokens=str(MODEL_DIR / _MODEL_FILES["tokens"]),
                encoder=str(MODEL_DIR / _MODEL_FILES["encoder"]),
                decoder=str(MODEL_DIR / _MODEL_FILES["decoder"]),
                joiner=str(MODEL_DIR / _MODEL_FILES["joiner"]),
                num_threads=1,
                keywords_file=str(keywords),
                keywords_score=1.0,
                keywords_threshold=self._threshold,
                provider="cpu",
            )
            self._stream = self._model.create_stream()
        except Exception as e:
            self._logger(f"Wake word: could not load model — {e}")
            self._notify("Wake word unavailable — use the WAKE NOW button.")
            self._model = None
            self._stream = None
            return False
        self._running = True
        self._ready = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="WakeWordThread")
        self._thread.start()
        self._logger(f"Wake word: listening for '{WAKE_PHRASE}'.")
        return True

    def stop(self) -> None:
        self._running = False
        # unblock the thread if it's waiting on the queue
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        self._model = None
        self._stream = None
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def reset(self) -> None:
        """Forget what was heard so far — call it when the assistant goes to sleep.
        Done on the detector's own thread, which is the only one that touches
        the model. Audio still waiting in the queue is dropped too, so it is
        not heard after the reset."""
        self._drain()
        self._reset_pending = True

    def _reset_model(self) -> None:
        # The stream keeps what it has heard so far. A fresh one means one
        # utterance is one wake, and audio from before the assistant woke
        # cannot wake it again after it goes back to sleep.
        try:
            self._stream = self._model.create_stream()
        except Exception:
            pass

    def feed(self, frame_int16) -> None:
        """Called from the mic callback (real-time thread). Must stay cheap and
        never block — the frame is copied and dropped if the queue is backed up."""
        if not self._running:
            return
        try:
            # frame_int16 is a numpy int16 array (possibly 2-D mono) — flatten to 1-D
            data = frame_int16[:, 0].copy() if getattr(frame_int16, "ndim", 1) > 1 else frame_int16.copy()
            self._queue.put_nowait(data)
        except queue.Full:
            pass
        except Exception:
            pass

    def _loop(self) -> None:
        import numpy as np
        while self._running:
            try:
                frame = self._queue.get()
                if frame is None or not self._running:
                    break
                if self._reset_pending:
                    self._reset_pending = False
                    self._reset_model()
                model, stream = self._model, self._stream
                if model is None or stream is None:
                    break
                samples = np.asarray(frame, dtype=np.float32) / 32768.0
                stream.accept_waveform(SAMPLE_RATE, samples)
                heard = ""
                while model.is_ready(stream):
                    model.decode_stream(stream)
                    result = model.get_result(stream)
                    if result:
                        # A decoy clears the stream too, so the "vision" inside
                        # "television" is not matched a moment later.
                        model.reset_stream(stream)
                        if result == _WAKE_LABEL:
                            heard = result
                            break
                if heard:
                    # drain any backlog and clear the model, so one utterance is one wake
                    self._drain()
                    self._reset_model()
                    try:
                        self._on_detect()
                    except Exception as e:
                        self._logger(f"Wake word: on_detect error — {e}")
            except Exception as e:
                self._logger(f"Wake word: inference error — {e}")

    def _drain(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
        except Exception:
            pass
