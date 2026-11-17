"""actions/identity_tool.py - "learn my face", "learn my voice", "forget my face".

The recognising is done by core/identity.py. This file is the voice command:
learning runs on a worker thread and its result comes back to the
conversation through core/ask_first.tell(), so the tool itself returns at once.

Rules:
  * learning when nothing is learned yet is free;
  * learning AGAIN (replacing the stored face or voice) needs the owner, the
    same check a risky action gets, or a guest could enrol themselves;
  * forgetting goes through the CONFIRM banner (core/ask_first.py), and that
    banner is locked to the owner like every other one.
"""
from __future__ import annotations

import subprocess
import sys
import threading

from core import ask_first, confirm, identity, outcome

_PASSAGE = (
    "Read this aloud at your normal pace, in English or in Greek:\n\n"
    "The morning was cold and bright. I made coffee, opened the window and "
    "listened to the city waking up. Later I will check my calendar, answer "
    "a few messages and go for a walk before lunch. In the evening I like to "
    "read, cook something simple and listen to music.\n\n"
    "Το πρωί ήταν κρύο και φωτεινό. Έφτιαξα καφέ, άνοιξα το παράθυρο και "
    "άκουσα την πόλη να ξυπνάει. Αργότερα θα δω το ημερολόγιό μου, θα "
    "απαντήσω σε μερικά μηνύματα και θα πάω μια βόλτα πριν το μεσημεριανό."
)


def _tell(text: str) -> None:
    ask_first.tell(f"[SYSTEM] {text} Tell the user in one short sentence.")


def _owner_or_refuse(what: str) -> str:
    """'' when the speaker may replace stored data, else the refusal."""
    ok, why = identity.verify_owner()
    if ok:
        return ""
    name = identity.owner_name() or "the owner"
    return (f"Replacing the stored {what} is locked to {name}, and I could not "
            f"confirm it is them ({why}). Nothing was changed.")


def _learn_face() -> str:
    def _work():
        refuse = _owner_or_refuse("face") if identity.has_face() else ""
        _tell(refuse or identity.learn_face())

    threading.Thread(target=_work, daemon=True, name="identity-learn-face").start()
    return ("[LEARNING_FACE] The camera is on for a few seconds. Tell the user, in "
            "one short sentence in their language, to look at the camera alone "
            "and move their head a little. The result arrives by itself.")


def _install_voice_engine() -> str:
    r = subprocess.run([sys.executable, "-m", "pip", "install", "sherpa-onnx"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
        msg = f"Installing the voice engine failed: {tail[0][:160]}"
    else:
        import importlib
        importlib.invalidate_caches()
        msg = "The voice engine is installed. Ask me to learn your voice again."
    _tell(msg)
    return msg


def _learn_voice(player) -> str:
    if not identity.voice_engine_installed():
        # Installing software is behind the banner, like every other install.
        return confirm.request(
            "identity:install", "Install the voice recognition engine",
            "pip install sherpa-onnx (the same engine the wake word uses).",
            _install_voice_engine)
    if identity.learning_voice():
        return "Already listening to learn the voice. Ask the user to keep talking."
    if identity.has_voice():
        refuse = _owner_or_refuse("voice")
        if refuse:
            return refuse

    identity.learn_voice(on_done=_tell)
    if player is not None:
        try:
            player.show_content("LEARN MY VOICE", _PASSAGE)
        except Exception:
            pass
    return ("[LEARNING_VOICE] Vision is now listening ONLY to learn the user's voice: "
            "for the next minute what they say is not sent to you. Say ONE short "
            "sentence in their language asking them to read the text on screen, "
            "or just talk, for about twenty seconds. Then stay silent; the result "
            "arrives by itself.")


def identity_tool(parameters: dict, player=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "status").strip().lower().replace("-", "_")
    try:
        if action == "learn_face":
            return _learn_face()
        if action == "learn_voice":
            return _learn_voice(player)
        if action == "forget":
            # Reached only after the banner was confirmed (core/ask_first.py).
            return identity.forget(str(p.get("which") or "all"))
        if action in ("check", "who"):
            if not identity.locked():
                return "Nothing is learned yet, so I cannot tell who is speaking."
            ok, how = identity.verify_owner()
            name = identity.owner_name() or "the owner"
            return (f"Yes, this is {name} (recognised by {how})." if ok
                    else f"I could not confirm this is {name} ({how}).")
        if action == "status":
            return identity.status()
    except Exception as e:
        return outcome.failed("identity", str(e))
    return "Unknown action. Use learn_face, learn_voice, forget, check or status."


def confirmation(args: dict):
    """(title, detail) for the banner when this call needs one, else None."""
    if str((args or {}).get("action") or "").strip().lower() != "forget":
        return None
    which = str(args.get("which") or "all").strip().lower()
    what = {"face": "your face", "voice": "your voice"}.get(which, "your face and voice")
    return (f"Forget {what}",
            f"Deletes what Vision learned about {what}. Risky actions stop being "
            f"locked to you unless something is still learned.")


TOOL = {
    "name": "identity",
    "description": (
        "Learn, check or forget the user's face and voice. 'learn my face' -> "
        "learn_face; 'learn my voice' / 'remember my voice' -> learn_voice; "
        "'forget my face / voice / both' -> forget with which; 'do you know who "
        "I am?' / 'who is talking?' -> check; 'what do you know about my face "
        "and voice' -> status. Once learned, every action that needs an "
        "on-screen confirmation (deleting, sending, installing, shutting the PC "
        "down) only goes ahead when the owner's voice or face is recognised; a "
        "[NOT_THE_OWNER] result means it was refused and NOT done. Everything "
        "stays on this computer."
    ),
    "brief": "Learn, check or forget the owner's face and voice.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "learn_face | learn_voice | forget | check | status",
            },
            "which": {
                "type": "STRING",
                "description": "forget: face | voice | all (default all)",
            },
        },
        "required": ["action"],
    },
    "handler": identity_tool,
}
