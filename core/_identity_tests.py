"""
Tests for learning the owner's face and voice (core/identity.py,
actions/identity_tool.py) and the owner lock on the CONFIRM banner.
Run with:  python -m core._identity_tests

No camera, microphone, model or network is used: the face and voice checks
are replaced by stand-ins, and the stored data goes to a temporary folder.
What these cannot show is how well the real models tell Kostas apart from
someone else in his room; that is checked on the PC, and every check writes
its score to the activity log for tuning.

Optional: set IDENTITY_MODELS to a folder holding the three .onnx files and
IDENTITY_FACES to a folder with two photos of one person (a1.jpg, a2.jpg)
and one of another (b.jpg) to also run the real face model.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from core import ask_first, confirm, identity

_passed = 0
_failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


def _use_temp_store() -> Path:
    d = Path(tempfile.mkdtemp(prefix="vision-identity-"))
    identity.IDENTITY_DIR = d
    identity.FACE_FILE = d / "face.npy"
    identity.VOICE_FILE = d / "voice.npy"
    identity._face_ok_until = 0.0
    return d


class Banner:
    def __init__(self):
        self.shown = []

    def show(self, title, detail):
        self.shown.append(title)

    def hide(self):
        pass


def _bind_banner() -> Banner:
    b = Banner()
    confirm.bind(show=b.show, hide=b.hide, log=lambda m: None)
    confirm._pending = None
    return b


def _speech(seconds: float, level: int = 3000, seed: int = 0) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    n = int(seconds * identity.SAMPLE_RATE / 1024)
    return [(rng.standard_normal(1024) * level).astype(np.int16) for _ in range(n)]


def _silence(seconds: float) -> list[np.ndarray]:
    n = int(seconds * identity.SAMPLE_RATE / 1024)
    return [np.zeros(1024, dtype=np.int16) for _ in range(n)]


# ── tests ────────────────────────────────────────────────────────────────────

def test_nothing_learned_is_unlocked():
    _use_temp_store()
    b = _bind_banner()
    check("not locked when nothing learned", not identity.locked())
    check("gate lets it through", identity.gate("Delete x") is None)
    r = confirm.request("k", "Delete x", "d", lambda: "ok")
    check("banner shown when unlocked", b.shown == ["Delete x"], str(b.shown))
    check("pending text", "CONFIRMATION_PENDING" in r)


def test_owner_voice_opens_banner():
    _use_temp_store()
    identity._save(identity.VOICE_FILE, [np.ones(4, np.float32)])
    b = _bind_banner()
    real = identity.check_voice, identity.check_face
    identity.check_voice = lambda: (True, 0.8, "voice")
    try:
        r = confirm.request("k", "Shut this computer down", "d", lambda: "ok")
    finally:
        identity.check_voice, identity.check_face = real
    check("owner voice: banner shown", b.shown == ["Shut this computer down"], str(b.shown))
    check("owner voice: pending", "CONFIRMATION_PENDING" in r, r)


def test_stranger_refused_no_banner():
    _use_temp_store()
    identity._save(identity.VOICE_FILE, [np.ones(4, np.float32)])
    identity._save(identity.FACE_FILE, [np.ones(4, np.float32)])
    b = _bind_banner()
    real = identity.check_voice, identity.check_face
    identity.check_voice = lambda: (False, 0.21, "voice")
    identity.check_face = lambda seconds=2.5: (False, 0.1, "a face that is not yours")
    ran = []
    try:
        r = confirm.request("k", "Delete report", "d", lambda: ran.append(1) or "ok")
    finally:
        identity.check_voice, identity.check_face = real
    check("stranger: no banner", b.shown == [], str(b.shown))
    check("stranger: refusal text", "[NOT_THE_OWNER]" in r and "NOT done" in r, r)
    check("stranger: nothing pending", confirm.pending_title() == "")
    check("stranger: action did not run", ran == [])


def test_face_rescues_short_voice():
    _use_temp_store()
    identity._save(identity.VOICE_FILE, [np.ones(4, np.float32)])
    identity._save(identity.FACE_FILE, [np.ones(4, np.float32)])
    real = identity.check_voice, identity.check_face
    identity.check_voice = lambda: (None, 0.0, "no recent speech")
    identity.check_face = lambda seconds=2.5: (True, 0.7, "face matched")
    try:
        ok, how = identity.verify_owner()
    finally:
        identity.check_voice, identity.check_face = real
    check("typed command, face seen: owner", ok and how == "face", how)


def test_broken_check_stays_locked():
    _use_temp_store()
    identity._save(identity.FACE_FILE, [np.ones(4, np.float32)])
    real = identity.check_face

    def boom(seconds=2.5):
        raise RuntimeError("camera exploded")
    identity.check_face = boom
    try:
        r = identity.gate("Install x")
    finally:
        identity.check_face = real
    check("an error in the check refuses", r is not None and "NOT_THE_OWNER" in r, str(r))


def test_ask_first_goes_through_lock():
    _use_temp_store()
    identity._save(identity.VOICE_FILE, [np.ones(4, np.float32)])
    b = _bind_banner()
    real = identity.check_voice
    identity.check_voice = lambda: (False, 0.1, "voice")
    ran = []
    try:
        r = ask_first.guard("send_message",
                            {"receiver": "Maria", "message_text": "hi"},
                            lambda: ran.append(1) or "sent")
    finally:
        identity.check_voice = real
    check("ask_first: stranger refused", "[NOT_THE_OWNER]" in r and not b.shown, r)
    check("ask_first: not sent", ran == [])
    r2 = ask_first.guard("open_app", {"app_name": "firefox"}, lambda: "opened")
    check("ordinary actions are not locked", r2 == "opened", r2)


def test_forget_needs_banner():
    check("forget asks", ask_first.describe("identity", {"action": "forget", "which": "face"})
          is not None)
    check("status does not ask", ask_first.describe("identity", {"action": "status"}) is None)
    check("learn does not ask", ask_first.describe("identity", {"action": "learn_face"}) is None)


def test_forget_deletes():
    d = _use_temp_store()
    identity._save(identity.VOICE_FILE, [np.ones(4, np.float32)])
    identity._save(identity.FACE_FILE, [np.ones(4, np.float32)])
    mode = oct(identity.FACE_FILE.stat().st_mode & 0o777)
    check("stored file is private", mode == "0o600", mode)
    r = identity.forget("face")
    check("forget face", not identity.has_face() and identity.has_voice(), r)
    check("still locked by voice", identity.locked())
    r = identity.forget("all")
    check("forget all", not identity.locked() and "no longer locked" in r, r)
    check("forget nothing", "nothing" in identity.forget("all").lower())
    check("no photos or audio on disk", sorted(p.name for p in d.iterdir()) == [])


def test_reference_is_average():
    _use_temp_store()
    identity._save(identity.FACE_FILE, [np.array([1, 0], np.float32), np.array([0, 2], np.float32)])
    ref = identity._reference(identity.FACE_FILE)
    check("reference = mean of normalised", np.allclose(ref, [0.5, 0.5]), str(ref))


def test_last_utterance():
    tap = identity.VoiceTap()
    for blk in _speech(3.0, seed=1) + _silence(2.0) + _speech(1.5, seed=2) + _silence(0.3):
        tap.feed(blk)
    u = tap.last_utterance()
    secs = u.size / identity.SAMPLE_RATE
    check("only the last utterance", 1.3 < secs < 1.8, f"{secs:.2f}s")
    tap.clear()
    check("cleared", tap.last_utterance().size == 0)
    for blk in _speech(12.0, seed=3):
        tap.feed(blk)
    secs = tap.last_utterance().size / identity.SAMPLE_RATE
    check("capped at MAX_VOICED", secs <= identity.MAX_VOICED + 0.1, f"{secs:.2f}s")


def test_quiet_room_is_not_speech():
    tap = identity.VoiceTap()
    for blk in _speech(5.0, level=40):
        tap.feed(blk)
    check("hum is not voiced", tap.voiced_seconds() == 0.0, str(tap.voiced_seconds()))


def test_listen_while_learning():
    _use_temp_store()
    identity.tap.clear()
    identity._learning = identity.VoiceTap()
    try:
        blk = _speech(0.1)[0]
        check("learning: block kept from the model", identity.listen(blk) is True)
        check("learning: not in the command tap", identity.tap.voiced_seconds() == 0.0)
    finally:
        identity._learning = None
    check("normal: block goes to the model", identity.listen(_speech(0.1)[0]) is False)
    identity.heard_elsewhere()
    check("text command clears the tap", identity.tap.last_utterance().size == 0)


def test_relearn_needs_owner():
    _use_temp_store()
    identity._save(identity.VOICE_FILE, [np.ones(4, np.float32)])
    from actions import identity_tool
    real = (identity.check_voice, identity.voice_engine_installed, identity.learn_voice)
    started = []
    identity.check_voice = lambda: (False, 0.2, "voice")
    identity.voice_engine_installed = lambda: True
    identity.learn_voice = lambda on_done: started.append(1) or "listening"
    try:
        r = identity_tool.identity_tool({"action": "learn_voice"})
    finally:
        identity.check_voice, identity.voice_engine_installed, identity.learn_voice = real
    check("stranger cannot replace the voice", "locked" in r and not started, r)


def test_missing_engine_asks_before_install():
    _use_temp_store()
    from actions import identity_tool
    b = _bind_banner()
    real = identity.voice_engine_installed
    identity.voice_engine_installed = lambda: False
    try:
        r = identity_tool.identity_tool({"action": "learn_voice"})
    finally:
        identity.voice_engine_installed = real
    check("install goes behind the banner",
          b.shown == ["Install the voice recognition engine"] and "PENDING" in r, r)


def test_vision_close_not_locked():
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    start = src.index("async def _tool_shutdown_vision")
    body = src[start:src.index("async def", start + 10)]
    check("closing Vision never asks", "confirm" not in body and "identity" not in body)


def test_tool_declared():
    from actions import identity_tool
    t = identity_tool.TOOL
    check("tool name", t["name"] == "identity")
    check("actions listed", all(a in t["parameters"]["properties"]["action"]["description"]
                                for a in ("learn_face", "learn_voice", "forget", "check")))


def test_greet_only_owner():
    _use_temp_store()
    identity._save(identity.FACE_FILE, [np.ones(4, np.float32)])
    said = []
    real = identity.check_face, identity.owner_name
    identity.owner_name = lambda: "Kostas"
    try:
        identity._last_greet = None
        identity.check_face = lambda seconds=2.5: (False, 0.1, "a face that is not yours")
        identity.greet_if_owner(said.append)
        time.sleep(0.2)
        check("stranger not greeted", said == [], str(said))
        identity.check_face = lambda seconds=2.5: (True, 0.8, "face matched")
        identity.greet_if_owner(said.append)
        time.sleep(0.2)
        check("owner greeted by name", len(said) == 1 and "Kostas" in said[0], str(said))
        identity.greet_if_owner(said.append)
        time.sleep(0.2)
        check("not greeted twice in a row", len(said) == 1, str(said))
    finally:
        identity.check_face, identity.owner_name = real
        identity._last_greet = None


def test_real_face_model():
    models, faces = os.environ.get("IDENTITY_MODELS"), os.environ.get("IDENTITY_FACES")
    if not models or not faces:
        return
    identity.MODEL_DIR = Path(models)
    import cv2
    a1, a2, b = (cv2.imread(str(Path(faces) / n)) for n in ("a1.jpg", "a2.jpg", "b.jpg"))
    ea1, ea2, eb = (identity.face_embeddings(x)[0] for x in (a1, a2, b))
    same, other = identity._cos(ea1, ea2), identity._cos(ea1, eb)
    print(f"  real face model: same {same:.2f}, other {other:.2f}")
    check("real model: same person matches", same >= identity.FACE_THRESHOLD, f"{same:.2f}")
    check("real model: other person does not", other < identity.FACE_THRESHOLD, f"{other:.2f}")


def main() -> int:
    for t in (test_nothing_learned_is_unlocked, test_owner_voice_opens_banner,
              test_stranger_refused_no_banner, test_face_rescues_short_voice,
              test_broken_check_stays_locked, test_ask_first_goes_through_lock,
              test_forget_needs_banner, test_forget_deletes, test_reference_is_average,
              test_last_utterance, test_quiet_room_is_not_speech,
              test_listen_while_learning, test_relearn_needs_owner,
              test_missing_engine_asks_before_install, test_vision_close_not_locked,
              test_tool_declared, test_greet_only_owner, test_real_face_model):
        try:
            t()
        except Exception as e:
            check(t.__name__, False, f"raised {type(e).__name__}: {e}")
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
