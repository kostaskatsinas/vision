"""
One-time carry-over from the assistant's old name.

This fork was renamed to VISION. Things the app had already written under the
old name — the auto-start entry, browser profiles with their logins, the
dashboard certificate, the desktop launcher, the saved assistant name — would
otherwise be orphaned, or worse, doubled (two auto-start entries start two
copies). `migrate()` renames each one once, at startup, and only when the new
name is not already taken. Nothing is deleted or overwritten.

This is the only file that still spells the old name, because it has to.
"""
from __future__ import annotations

import json
from pathlib import Path

_OLD = "jar" + "vis"          # lower-case stem of the old name
_BASE = Path(__file__).resolve().parent.parent


def _pairs() -> list[tuple[Path, Path]]:
    home = Path.home()
    desktop = home / "Desktop"
    certs = _BASE / "config" / "certs"
    old_cap = _OLD.capitalize()
    return [
        (home / ".config" / "autostart" / f"{_OLD}.desktop",
         home / ".config" / "autostart" / "vision.desktop"),
        (home / f".{_OLD}_profiles", home / ".vision_profiles"),
        (home / f".{_OLD}_profiles" / f"firefox_{_OLD}",
         home / ".vision_profiles" / "firefox_vision"),
        (certs / f"{_OLD}.key", certs / "vision.key"),
        (certs / f"{_OLD}.crt", certs / "vision.crt"),
        (_BASE / "config" / f"{_OLD}.png", _BASE / "config" / "vision.png"),
        (desktop / (".".join(_OLD.upper()) + ".desktop"), desktop / "VISION.desktop"),
        (desktop / f"{old_cap}Projects", desktop / "VisionProjects"),
    ]


def _rename_paths() -> None:
    pairs = _pairs()
    # The profiles folder itself moves first, so its inner folder is then
    # looked for under the new parent.
    for old, new in pairs:
        if old.parent.name == f".{_OLD}_profiles" and not old.exists():
            old = new.parent / old.name
        try:
            if old.exists() and not new.exists():
                new.parent.mkdir(parents=True, exist_ok=True)
                old.rename(new)
                print(f"[VISION] renamed {old} -> {new}")
        except Exception as e:
            print(f"[VISION] could not rename {old}: {e!r}")


def _fix_launcher() -> None:
    """The desktop launcher carries the old name and icon path inside it."""
    desk = Path.home() / "Desktop" / "VISION.desktop"
    try:
        if not desk.exists():
            return
        text = desk.read_text(encoding="utf-8")
        fixed = (text.replace(".".join(_OLD.upper()), "VISION")
                     .replace(f"{_OLD}.png", "vision.png")
                     .replace(f"{_OLD}.ico", "vision.ico"))
        if fixed != text:
            desk.write_text(fixed, encoding="utf-8")
    except Exception as e:
        print(f"[VISION] could not update the desktop launcher: {e!r}")


def _fix_saved_name() -> None:
    """A name saved as the old default becomes the new default. A name the
    user chose themselves is left alone."""
    path = _BASE / "config" / "api_keys.json"
    try:
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        name = str(data.get("assistant_name") or "")
        if name.replace(".", "").strip().lower() != _OLD:
            return
        from memory.config_manager import save_assistant_config
        save_assistant_config("VISION", str(data.get("user_name") or ""))
        print("[VISION] saved assistant name updated to VISION")
    except Exception as e:
        print(f"[VISION] could not update the saved assistant name: {e!r}")


def migrate() -> None:
    _rename_paths()
    _fix_launcher()
    _fix_saved_name()
