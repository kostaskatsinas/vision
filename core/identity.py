"""core/identity.py - Vision learns the owner's face and voice, and checks them.

WHAT IT DOES
    "Learn my face" looks through the webcam for a few seconds. "Learn my voice"
    listens while the owner talks for about twenty seconds. From then on:

      * every action that already needs the on-screen CONFIRM banner (deleting
        files, sending messages, installing, shutting the computer down, ...)
        first has to be asked for by the owner. Vision checks the voice that
        gave the command and, if that does not match, looks through the camera
        once. Anyone else gets a polite no. See core/confirm.py, which calls
        `gate()` before it shows a banner;
      * when Vision wakes up it looks once and greets the owner by name.

    Closing Vision itself is NOT locked: shutdown_vision never goes through the
    banner, and the window's close button is not a tool at all.

WHAT IS STORED, AND WHERE
    Only numbers. A face becomes 128 numbers (OpenCV's SFace model), a voice
    192 numbers (NVIDIA's TitaNet-small, run by sherpa-onnx). No photo and no
    recording is ever written to disk or sent anywhere. The numbers live in
    config/identity/ (face.npy, voice.npy), readable only by this user, and
    "forget my face" / "forget my voice" deletes them.

    The three models (about 80 MB together) are downloaded once from GitHub on
    the first "learn", checked against a pinned SHA-256, and kept in
    config/identity/models/. They run on this computer's CPU.

HOW SURE IT IS
    This is a lock for a shared room, not for a bank. A good photo can fool the
    face check and a recording can fool the voice check. It stops a guest, a
    child or the TV from deleting or sending things; it is not meant to stop a
    determined attacker sitting at the keyboard.

THRESHOLDS
    Cosine similarity against the enrolled average. Defaults below; override in
    config/api_keys.json with identity_face_threshold / identity_voice_threshold.
    Every check logs its score, so tuning is one look at the activity log.
"""
from __future__ import annotations

import collections
import hashlib
import importlib.util
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from core.paths import base_dir

IDENTITY_DIR = base_dir() / "config" / "identity"
MODEL_DIR    = IDENTITY_DIR / "models"
FACE_FILE    = IDENTITY_DIR / "face.npy"
VOICE_FILE   = IDENTITY_DIR / "voice.npy"

# name -> (url, sha256, size in MB for the log line)
_MODELS = {
    "face_detect": (
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
        "face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4", 0.2),
    "face_recognize": (
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
        "face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79", 39),
    "voice": (
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
        "speaker-recongition-models/nemo_en_titanet_small.onnx",
        "ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e", 40),
}
_FILES = {
    "face_detect":    "face_detection_yunet_2023mar.onnx",
    "face_recognize": "face_recognition_sface_2021dec.onnx",
    "voice":          "nemo_en_titanet_small.onnx",
}

# OpenCV's published SFace cosine threshold is 0.363. Compared against the
# AVERAGE of a dozen enrolment frames the owner scores well above it.
FACE_THRESHOLD = 0.40
# TitaNet on real microphones: the same person usually scores 0.7 to 0.9 and
# different people below 0.4. Measured on 20 synthetic voices: owner never
# below 0.73, strangers 0.1% above 0.73. 0.60 leaves room for a cold or a
# different room without letting strangers in.
VOICE_THRESHOLD = 0.60

SAMPLE_RATE = 16000

# Voice learning: how much speech to collect, and how long to wait for it.
LEARN_VOICE_SECONDS = 20.0
LEARN_VOICE_TIMEOUT = 75.0
# Face learning: frames to keep, and how long to try.
LEARN_FACE_FRAMES  = 15
LEARN_FACE_TIMEOUT = 12.0

# The voice behind a command: speech heard in the last RECENT_SECONDS, read
# backwards from the newest block, stopping at the first pause longer than
# PAUSE_SECONDS once MIN_VOICED seconds have been found. So the score belongs
# to the last thing said, not to someone who spoke a minute ago.
RECENT_SECONDS = 25.0
PAUSE_SECONDS  = 1.2
MIN_VOICED     = 1.0
MAX_VOICED     = 8.0

# A face match is reused for this long, so two banners in a row do not open
# the camera twice.
FACE_REUSE_SECONDS = 20.0

# Do not greet again on every wake within this long.
GREET_GAP_SECONDS = 30 * 60

_log_cb: Optional[Callable[[str], None]] = None


def bind(log: Callable[[str], None]) -> None:
    """`log(text)` writes a line to the activity log. Called by main.py."""
    global _log_cb
    _log_cb = log


def _log(msg: str) -> None:
    print(f"[Identity] {msg}")
    if _log_cb:
        try:
            _log_cb(f"SYS: {msg}")
        except Exception:
            pass


def _setting(key: str, default):
    try:
        from memory.config_manager import load_api_keys
        value = load_api_keys().get(key, default)
        return type(default)(value)
    except Exception:
        return default


def face_threshold() -> float:
    return _setting("identity_face_threshold", FACE_THRESHOLD)


def voice_threshold() -> float:
    return _setting("identity_voice_threshold", VOICE_THRESHOLD)


def owner_name() -> str:
    try:
        from memory.config_manager import get_user_name
        return (get_user_name() or "").strip()
    except Exception:
        return ""


# ── What is enrolled ─────────────────────────────────────────────────────────

def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _load(path: Path) -> Optional[np.ndarray]:
    try:
        if path.exists():
            data = np.load(path, allow_pickle=False)
            if data.ndim == 2 and data.shape[0] > 0:
                return data.astype(np.float32)
    except Exception as e:
        _log(f"Could not read {path.name}: {e}")
    return None


def _save(path: Path, embeddings: list[np.ndarray]) -> None:
    IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(IDENTITY_DIR, 0o700)
    except OSError:
        pass
    tmp = path.with_suffix(".tmp.npy")
    np.save(tmp, np.stack(embeddings).astype(np.float32), allow_pickle=False)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _reference(path: Path) -> Optional[np.ndarray]:
    data = _load(path)
    if data is None:
        return None
    normed = data / np.maximum(np.linalg.norm(data, axis=1, keepdims=True), 1e-9)
    return normed.mean(axis=0)


def has_face() -> bool:
    return FACE_FILE.exists()


def has_voice() -> bool:
    return VOICE_FILE.exists()


def locked() -> bool:
    """True once a face or a voice is learned: risky actions need the owner."""
    return has_face() or has_voice()


def forget(which: str = "all") -> str:
    which = (which or "all").strip().lower()
    gone = []
    for kind, path in (("face", FACE_FILE), ("voice", VOICE_FILE)):
        if which in (kind, "all", "both") and path.exists():
            try:
                path.unlink()
                gone.append(kind)
            except OSError as e:
                return f"Could not delete the {kind} data: {e}"
    global _face_ok_until
    _face_ok_until = 0.0
    if not gone:
        return "There was nothing to forget."
    left = [k for k, f in (("face", has_face), ("voice", has_voice)) if f()]
    tail = (f" I still know your {left[0]}." if left
            else " Risky actions are no longer locked to you.")
    return f"Forgot your {' and '.join(gone)}.{tail}"


# ── Models ───────────────────────────────────────────────────────────────────

def _model_path(name: str) -> Path:
    return MODEL_DIR / _FILES[name]


def _ensure_model(name: str) -> Path:
    """Download one model if missing. Raises with a readable message."""
    path = _model_path(name)
    if path.exists():
        return path
    url, sha, mb = _MODELS[name]
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    _log(f"Downloading the {name.replace('_', ' ')} model ({mb:g} MB, one time)...")
    tmp = path.with_suffix(".part")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as out:
            while True:
                block = resp.read(1 << 16)
                if not block:
                    break
                digest.update(block)
                out.write(block)
        if digest.hexdigest() != sha:
            raise RuntimeError("the download did not match its checksum")
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return path


def voice_engine_installed() -> bool:
    return importlib.util.find_spec("sherpa_onnx") is not None


_face_models = None
_voice_model = None
_model_lock = threading.Lock()


def _face():
    """(detector, recognizer), loaded once."""
    global _face_models
    with _model_lock:
        if _face_models is None:
            import cv2
            det = cv2.FaceDetectorYN.create(str(_ensure_model("face_detect")), "",
                                            (320, 320), 0.85)
            rec = cv2.FaceRecognizerSF.create(str(_ensure_model("face_recognize")), "")
            _face_models = (det, rec)
        return _face_models


def _voice():
    global _voice_model
    with _model_lock:
        if _voice_model is None:
            import sherpa_onnx
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(_ensure_model("voice")), num_threads=2)
            _voice_model = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        return _voice_model


# ── Face ─────────────────────────────────────────────────────────────────────

def face_embeddings(frame) -> list[np.ndarray]:
    """One embedding per face in a BGR frame, largest face first."""
    det, rec = _face()
    h, w = frame.shape[:2]
    det.setInputSize((w, h))
    _, faces = det.detect(frame)
    if faces is None:
        return []
    faces = sorted(faces, key=lambda f: -(f[2] * f[3]))
    return [rec.feature(rec.alignCrop(frame, f)).flatten().astype(np.float32)
            for f in faces]


def _camera_index() -> int:
    return _setting("camera_index", 0)


def _frames(seconds: float):
    """Yield webcam frames for up to `seconds`. Raises if the camera fails."""
    import cv2
    cap = cv2.VideoCapture(_camera_index(), cv2.CAP_ANY)
    if not cap.isOpened():
        cap.release()
        cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        cap.release()
        raise RuntimeError("the camera could not be opened (is it in use?)")
    try:
        for _ in range(4):           # the first frames are often dark
            cap.read()
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ok, frame = cap.read()
            if ok and frame is not None:
                yield frame
    finally:
        cap.release()


_face_ok_until = 0.0


def check_face(seconds: float = 2.5) -> tuple[Optional[bool], float, str]:
    """(matched, best score, note). matched is None when no face is learned
    or the camera could not be used."""
    global _face_ok_until
    ref = _reference(FACE_FILE)
    if ref is None:
        return None, 0.0, "no face learned"
    if time.monotonic() < _face_ok_until:
        return True, 1.0, "seen a moment ago"
    best, seen = 0.0, False
    try:
        for frame in _frames(seconds):
            for emb in face_embeddings(frame):
                seen = True
                best = max(best, _cos(ref, emb))
            if best >= face_threshold():
                _face_ok_until = time.monotonic() + FACE_REUSE_SECONDS
                return True, best, "face matched"
    except Exception as e:
        return None, 0.0, f"camera: {e}"
    return False, best, ("a face that is not yours" if seen else "nobody in front of the camera")


def learn_face() -> str:
    """Look through the webcam and store the owner's face. Blocks ~5-12 s."""
    got: list[np.ndarray] = []
    last = 0.0
    try:
        for frame in _frames(LEARN_FACE_TIMEOUT):
            embs = face_embeddings(frame)
            if len(embs) != 1:
                continue             # nobody, or more than one person
            if time.monotonic() - last < 0.25:
                continue             # spread the frames over a few seconds
            last = time.monotonic()
            got.append(embs[0])
            if len(got) >= LEARN_FACE_FRAMES:
                break
    except Exception as e:
        return f"I could not learn your face: {e}."
    if len(got) < 5:
        return ("I could not see one face clearly enough. Sit in front of the "
                "camera alone, in good light, and ask again.")
    _save(FACE_FILE, got)
    global _face_ok_until
    _face_ok_until = 0.0
    _log(f"Face learned from {len(got)} frames.")
    return f"Learned your face from {len(got)} frames."


# ── Voice ────────────────────────────────────────────────────────────────────

def _rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(block.astype(np.float32))))) if block.size else 0.0


class VoiceTap:
    """The last few seconds of what the microphone sent to the assistant.

    main.py feeds it from the mic callback (a queue append, nothing more), so
    it only ever holds audio the user chose to send: never Vision's own voice,
    never anything heard while asleep, muted or with push-to-talk released.
    It lives in memory only and is never written anywhere."""

    def __init__(self, seconds: float = RECENT_SECONDS):
        self._blocks: collections.deque = collections.deque()
        self._seconds = seconds
        self._lock = threading.Lock()
        self._floor = 150.0          # running noise floor (int16 RMS)

    def feed(self, block) -> None:
        a = np.asarray(block, dtype=np.int16).reshape(-1).copy()
        now = time.monotonic()
        level = _rms(a)
        with self._lock:
            # Noise floor: follows quiet blocks down quickly, creeps up slowly.
            if level < self._floor:
                self._floor = max(60.0, 0.9 * self._floor + 0.1 * level)
            else:
                self._floor = min(2000.0, self._floor * 1.001)
            voiced = level > max(250.0, self._floor * 2.5)
            self._blocks.append((now, voiced, a))
            while self._blocks and now - self._blocks[0][0] > self._seconds:
                self._blocks.popleft()

    def clear(self) -> None:
        with self._lock:
            self._blocks.clear()

    def last_utterance(self) -> np.ndarray:
        """The most recent stretch of speech, as float32 at 16 kHz."""
        with self._lock:
            blocks = list(self._blocks)
        out: list[np.ndarray] = []
        voiced_s, gap_s = 0.0, 0.0
        for _, voiced, a in reversed(blocks):
            dur = a.size / SAMPLE_RATE
            if voiced:
                out.append(a)
                voiced_s += dur
                gap_s = 0.0
                if voiced_s >= MAX_VOICED:
                    break
            else:
                gap_s += dur
                if voiced_s >= MIN_VOICED and gap_s > PAUSE_SECONDS:
                    break
        if not out:
            return np.zeros(0, dtype=np.float32)
        return (np.concatenate(out[::-1]).astype(np.float32) / 32768.0)

    def voiced_seconds(self) -> float:
        with self._lock:
            return sum(a.size for _, v, a in self._blocks if v) / SAMPLE_RATE

    def voiced_audio(self) -> np.ndarray:
        with self._lock:
            parts = [a for _, v, a in self._blocks if v]
        if not parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts).astype(np.float32) / 32768.0


tap = VoiceTap()

# Voice learning in progress: frames go here and are NOT sent to the model, so
# the assistant does not answer the passage being read to it.
_learning: Optional[VoiceTap] = None
_learn_lock = threading.Lock()


def listen(block) -> bool:
    """Called by the mic callback for every block it is about to stream.

    Returns True while a voice is being learned: the caller then keeps the
    block away from the model. Never raises."""
    try:
        learner = _learning
        if learner is not None:
            learner.feed(block)
            return True
        tap.feed(block)
    except Exception:
        pass
    return False


def heard_elsewhere() -> None:
    """A command arrived by text, from the phone or the dashboard: whatever the
    PC microphone heard earlier is not the voice behind it."""
    tap.clear()


def voice_embedding(audio: np.ndarray) -> Optional[np.ndarray]:
    if audio.size < SAMPLE_RATE * 0.5:
        return None
    ex = _voice()
    stream = ex.create_stream()
    stream.accept_waveform(SAMPLE_RATE, audio)
    stream.input_finished()
    if not ex.is_ready(stream):
        return None
    return np.asarray(ex.compute(stream), dtype=np.float32)


def check_voice() -> tuple[Optional[bool], float, str]:
    """(matched, score, note) for the last thing said into the microphone.
    matched is None when no voice is learned or nothing usable was heard."""
    ref = _reference(VOICE_FILE)
    if ref is None:
        return None, 0.0, "no voice learned"
    if not voice_engine_installed():
        return None, 0.0, "the voice engine is not installed"
    audio = tap.last_utterance()
    if audio.size < SAMPLE_RATE * MIN_VOICED:
        return None, 0.0, "no recent speech"
    try:
        emb = voice_embedding(audio)
    except Exception as e:
        return None, 0.0, f"voice model: {e}"
    if emb is None:
        return None, 0.0, "the speech was too short"
    score = _cos(ref, emb)
    return score >= voice_threshold(), score, "voice"


def learn_voice(on_done: Callable[[str], None]) -> str:
    """Start learning the owner's voice in the background.

    Returns at once. Speech is collected from the mic callback (see listen())
    until LEARN_VOICE_SECONDS of it are heard, then `on_done(result)` is
    called from a worker thread."""
    global _learning
    with _learn_lock:
        if _learning is not None:
            return "already listening"
        _learning = VoiceTap(seconds=LEARN_VOICE_TIMEOUT + 5)

    def _work():
        global _learning
        learner = _learning
        result = ""
        try:
            _voice()                                   # download/load first
            end = time.monotonic() + LEARN_VOICE_TIMEOUT
            next_note = 5.0
            while time.monotonic() < end:
                heard = learner.voiced_seconds()
                if heard >= LEARN_VOICE_SECONDS:
                    break
                if heard >= next_note:
                    _log(f"Learning your voice... {int(heard)} of "
                         f"{int(LEARN_VOICE_SECONDS)} seconds.")
                    next_note += 5.0
                time.sleep(0.25)
            with _learn_lock:
                _learning = None
            audio = learner.voiced_audio()
            if audio.size < SAMPLE_RATE * 8:
                result = ("I did not hear enough speech to learn your voice. "
                          "Ask again and keep talking for about twenty seconds.")
            else:
                # Several embeddings from overlapping 4 s pieces, so the stored
                # average does not lean on one sentence.
                piece, hop = SAMPLE_RATE * 4, SAMPLE_RATE * 2
                embs = []
                for start in range(0, max(1, audio.size - piece + 1), hop):
                    e = voice_embedding(audio[start:start + piece])
                    if e is not None:
                        embs.append(e)
                if len(embs) < 2:
                    result = "I could not make sense of the recording. Please try again."
                else:
                    _save(VOICE_FILE, embs)
                    _log(f"Voice learned from {audio.size / SAMPLE_RATE:.0f} s of speech.")
                    result = (f"Learned your voice from {audio.size / SAMPLE_RATE:.0f} "
                              f"seconds of speech.")
        except Exception as e:
            result = f"I could not learn your voice: {e}."
        finally:
            with _learn_lock:
                _learning = None
        try:
            on_done(result)
        except Exception:
            pass

    threading.Thread(target=_work, daemon=True, name="identity-voice").start()
    return "listening"


def learning_voice() -> bool:
    return _learning is not None


# ── Is it the owner? ─────────────────────────────────────────────────────────

def verify_owner(use_camera: bool = True) -> tuple[bool, str]:
    """(is the owner, how it was decided). True when nothing is learned."""
    if not locked():
        return True, "nothing learned yet"
    notes = []
    if has_voice():
        ok, score, note = check_voice()
        if ok:
            _log(f"Owner check: voice matched ({score:.2f}).")
            return True, "voice"
        notes.append(f"voice {score:.2f}" if ok is False else note)
    if has_face() and use_camera:
        ok, score, note = check_face()
        if ok:
            _log(f"Owner check: face matched ({score:.2f}).")
            return True, "face"
        notes.append(f"face {score:.2f}, {note}" if ok is False else note)
    why = "; ".join(notes) or "nothing to check against"
    _log(f"Owner check: not matched ({why}).")
    return False, why


def gate(title: str) -> Optional[str]:
    """None when this risky action may go ahead; otherwise the sentence for the
    assistant. Called by core/confirm.py before it shows a banner."""
    if not locked():
        return None
    try:
        ok, why = verify_owner()
    except Exception as e:           # a broken check must not open the lock
        ok, why = False, f"the check failed: {e}"
    if ok:
        return None
    name = owner_name() or "the owner"
    hint = ("look at the camera and ask again" if has_face()
            else "ask again in your own voice")
    return (f"[NOT_THE_OWNER] '{title}' is locked to {name}, and I could not "
            f"confirm it is them ({why}). It was NOT done. Say ONE short sentence "
            f"in the user's language: only {name} can ask for this; if they are "
            f"{name}, {hint}.")


# ── Greeting ─────────────────────────────────────────────────────────────────

_last_greet: Optional[float] = None


def greet_if_owner(say: Callable[[str], None]) -> None:
    """On wake: look once and, if it is the owner, ask the assistant to greet
    them by name. Runs on its own thread; does nothing when no face is learned,
    the setting identity_greet is off, or the owner was greeted recently."""
    if not has_face() or not _setting("identity_greet", True):
        return
    if _last_greet is not None and time.monotonic() - _last_greet < GREET_GAP_SECONDS:
        return

    def _work():
        global _last_greet
        try:
            ok, score, _ = check_face(seconds=2.0)
        except Exception:
            return
        if not ok:
            return
        _last_greet = time.monotonic()
        name = owner_name()
        who = name or "the owner"
        say(f"[SYSTEM] The camera just recognised {who}. Greet {who} by name in "
            f"one short, warm sentence in their language, then wait.")

    threading.Thread(target=_work, daemon=True, name="identity-greet").start()


def status() -> str:
    parts = []
    parts.append("face: learned" if has_face() else "face: not learned")
    parts.append("voice: learned" if has_voice() else "voice: not learned")
    parts.append("risky actions locked to the owner" if locked()
                 else "risky actions not locked")
    return "; ".join(parts) + "."
