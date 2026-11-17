"""actions/scenes.py - workspace scenes: several apps, each on its monitor.

"Save a scene called work: terminal on monitor 1 on the left half, Chrome on
monitor 2, Spotify on monitor 3." Then "start my work scene" opens them all,
one after another, each where it belongs.

A scene is a name and a list of {app, monitor, tile}. Opening is done by
actions/open_app.py, so everything it already does applies here: an app that
is already running is brought forward and moved instead of started twice, and
an app that never appears is reported, not claimed.

Scenes are stored in memory/scenes.json. Saving over a scene and deleting one
can both be taken back with "undo".
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from core import outcome
from core.paths import base_dir

MAX_APPS = 12
MAX_SCENES = 30
_lock = threading.RLock()
_running = threading.Lock()      # one scene at a time: they share the pointer

_TILES = {
    "left": "left", "left half": "left", "right": "right", "right half": "right",
    "up": "up", "top": "up", "top half": "up", "down": "down", "bottom": "down",
    "bottom half": "down", "center": "center", "centre": "center", "middle": "center",
    "all": "all", "full": "all", "maximised": "all", "maximized": "all", "whole": "all",
}


def scenes_path() -> Path:
    return base_dir() / "memory" / "scenes.json"


def _key(name: str) -> str:
    """'My Work scene' and 'work' are the same scene."""
    words = [w for w in str(name or "").lower().replace("-", " ").replace("_", " ").split()
             if w not in ("my", "the", "scene", "setup", "set", "up", "workspace", "layout", "mode")]
    return " ".join(words)


def _load() -> dict:
    try:
        data = json.loads(scenes_path().read_text(encoding="utf-8"))
        scenes = data.get("scenes") if isinstance(data, dict) else None
        return scenes if isinstance(scenes, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(scenes: dict) -> None:
    path = scenes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"scenes": scenes}, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _clean_apps(raw) -> list[dict]:
    """The model's list, checked. Raises ValueError with a sentence for the user."""
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise ValueError("a scene needs at least one application")
    apps = []
    for item in raw:
        if isinstance(item, str):
            item = {"app": item}
        if not isinstance(item, dict):
            continue
        app = " ".join(str(item.get("app") or item.get("app_name") or item.get("name") or "").split())
        if not app:
            continue
        monitor = " ".join(str(item.get("monitor") or item.get("screen") or "").lower().split())
        tile_raw = " ".join(str(item.get("tile") or "").lower().split())
        if tile_raw and tile_raw not in _TILES:
            raise ValueError(f"'{tile_raw}' is not a tile position; use left, right, "
                             "up, down, center or all")
        apps.append({"app": app[:80], "monitor": monitor[:30], "tile": _TILES.get(tile_raw, "")})
    if not apps:
        raise ValueError("a scene needs at least one application")
    if len(apps) > MAX_APPS:
        raise ValueError(f"a scene holds at most {MAX_APPS} applications")
    return apps


def _find(scenes: dict, name: str) -> str:
    """The stored key for `name`, or '' when none or several match."""
    key = _key(name)
    if key in scenes:
        return key
    close = [k for k in scenes if key and (key in k or k in key)]
    return close[0] if len(close) == 1 else ""


def _describe_app(a: dict) -> str:
    text = a["app"]
    if a.get("monitor"):
        text += f" on monitor {a['monitor']}"
    if a.get("tile"):
        text += f" ({a['tile']})"
    return text


def _push_undo(label: str, key: str, before) -> None:
    """`before` is the scene as it was, or None when it did not exist."""
    try:
        from core.undo import push_undo
    except Exception:
        return

    def restore() -> str:
        with _lock:
            scenes = _load()
            if before is None:
                scenes.pop(key, None)
            else:
                scenes[key] = before
            _save(scenes)
        return f"Scene '{(before or {}).get('name') or key}' is back as it was."

    push_undo(label, restore)


def _say(player, text: str) -> None:
    if player is not None and hasattr(player, "request_say"):
        try:
            player.request_say(text)
        except Exception:
            pass


def _log(player, text: str) -> None:
    if player is not None:
        try:
            player.write_log(text)
        except Exception:
            pass


def _run_scene(scene: dict, player, opener=None) -> list[tuple[dict, str]]:
    """Open every app in order. Returns (app, problem) for the ones that did
    not go cleanly; an empty list means the whole scene is on screen."""
    if opener is None:
        from actions.open_app import open_app_and_wait as opener
    prev_monitor = None
    try:
        from core import wayland_desktop
        if wayland_desktop.available():
            prev_monitor = wayland_desktop.working_monitor()
    except Exception:
        wayland_desktop = None
    problems = []
    try:
        for a in scene["apps"]:
            params = {"app_name": a["app"]}
            if a.get("monitor"):
                params["monitor"] = a["monitor"]
            if a.get("tile"):
                params["tile"] = a["tile"]
            try:
                result = opener(params, player=player)
            except Exception as e:
                result = outcome.failed("open_app", f"{type(e).__name__}: {e}")
            if outcome.is_problem(result):
                reason = result.split("reason=", 1)[-1].split(" -- ", 1)[0].strip().strip('"')
                problems.append((a, reason or "it could not be confirmed open"))
    finally:
        # open_app moves Vision's working screen to each app's monitor. Put it
        # back, or the next "what's on my screen" looks at the wrong one.
        if prev_monitor is not None:
            try:
                wayland_desktop.use_monitor(prev_monitor)
            except Exception:
                pass
    return problems


def _run_in_background(scene: dict, player) -> None:
    try:
        problems = _run_scene(scene, player)
    except Exception as e:
        problems = [({"app": "the scene"}, f"{type(e).__name__}: {e}")]
    finally:
        _running.release()
    name = scene["name"]
    if not problems:
        # Silence on success: the windows are on screen.
        _log(player, f"VISION: Scene '{name}' is open.")
        return
    listed = "; ".join(f"{a['app']}: {why}" for a, why in problems)
    ok = len(scene["apps"]) - len(problems)
    _log(player, f"VISION: Scene '{name}' - {len(problems)} problem(s): {listed}")
    _say(player, "[SYSTEM] Tell the user, briefly and in their own language: scene "
                 f"'{name}' opened {ok} of {len(scene['apps'])} applications. "
                 f"Not confirmed: {listed}")


def scene(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower().replace("-", "_")
    name = " ".join(str(p.get("name") or "").split())
    try:
        with _lock:
            scenes = _load()

            if action in ("list", "show"):
                if action == "show" and name:
                    key = _find(scenes, name)
                    if not key:
                        return outcome.failed("scene", f"there is no scene called '{name}'",
                                              saved=", ".join(s["name"] for s in scenes.values()) or "none")
                    s = scenes[key]
                    return outcome.done("scene", name=s["name"],
                                        apps="; ".join(_describe_app(a) for a in s["apps"]))
                if not scenes:
                    return outcome.done("scene", scenes=0, note="no scene is saved yet")
                return outcome.done("scene", scenes=len(scenes),
                                    saved="; ".join(f"{s['name']} ({len(s['apps'])} apps)"
                                                    for s in scenes.values()))

            if not name:
                return outcome.ask("scene", "Which scene?")
            key = _find(scenes, name)

            if action in ("save", "create", "set"):
                apps = _clean_apps(p.get("apps"))
                new_key = key or _key(name)
                if not new_key:
                    return outcome.ask("scene", "What should the scene be called?")
                if new_key not in scenes and len(scenes) >= MAX_SCENES:
                    return outcome.failed("scene", f"{MAX_SCENES} scenes are saved already; delete one first")
                before = scenes.get(new_key)
                scenes[new_key] = {"name": (before or {}).get("name") or name, "apps": apps}
                _save(scenes)
                _push_undo(f"scene '{name}' saved", new_key, before)
                return outcome.done("scene", saved=scenes[new_key]["name"],
                                    apps="; ".join(_describe_app(a) for a in apps),
                                    replaced=bool(before), verified=True)

            if not key:
                return outcome.failed("scene", f"there is no scene called '{name}'",
                                      saved=", ".join(s["name"] for s in scenes.values()) or "none")
            current = scenes[key]

            if action in ("add_app", "add"):
                added = _clean_apps(p.get("apps"))
                names = {a["app"].lower() for a in added}
                apps = [a for a in current["apps"] if a["app"].lower() not in names] + added
                if len(apps) > MAX_APPS:
                    return outcome.failed("scene", f"a scene holds at most {MAX_APPS} applications")
                scenes[key] = {"name": current["name"], "apps": apps}
                _save(scenes)
                _push_undo(f"scene '{current['name']}' changed", key, current)
                return outcome.done("scene", changed=current["name"],
                                    apps="; ".join(_describe_app(a) for a in apps), verified=True)

            if action in ("remove_app", "remove"):
                drop = {a["app"].lower() for a in _clean_apps(p.get("apps"))}
                apps = [a for a in current["apps"] if a["app"].lower() not in drop]
                if len(apps) == len(current["apps"]):
                    return outcome.failed("scene", "that application is not in the scene",
                                          apps="; ".join(_describe_app(a) for a in current["apps"]))
                if not apps:
                    return outcome.failed("scene", "that would leave the scene empty; delete the scene instead")
                scenes[key] = {"name": current["name"], "apps": apps}
                _save(scenes)
                _push_undo(f"scene '{current['name']}' changed", key, current)
                return outcome.done("scene", changed=current["name"],
                                    apps="; ".join(_describe_app(a) for a in apps), verified=True)

            if action == "delete":
                scenes.pop(key)
                _save(scenes)
                _push_undo(f"scene '{current['name']}' deleted", key, current)
                return outcome.done("scene", deleted=current["name"], can_undo=True, verified=True)

            if action not in ("run", "start", "open"):
                return outcome.failed("scene", f"unknown action '{action}'")

        # run - outside the storage lock, it takes a while
        if not _running.acquire(blocking=False):
            return outcome.failed("scene", "another scene is still opening; wait for it to finish")
        try:
            threading.Thread(target=_run_in_background, args=(current, player),
                             daemon=True, name=f"scene-{key[:20]}").start()
        except Exception:
            _running.release()
            raise
        return outcome.started("scene", name=current["name"], apps=len(current["apps"]))
    except ValueError as e:
        return outcome.failed("scene", str(e))
    except Exception as e:
        return outcome.failed("scene", f"{type(e).__name__}: {e}")


# -- Tool declaration (auto-discovered by core/action_loader.py) ---------------
TOOL = {
    "name": "scene",
    "description": (
        "Workspace scenes: a saved set of applications, each with the monitor "
        "and tile it opens on, started together. Use for 'start my work "
        "scene', 'open my work setup', 'gaming mode', 'save a scene called "
        "work with terminal on monitor 1 left and Chrome on monitor 2', 'add "
        "Spotify on monitor 3 to the work scene', 'what scenes do I have', "
        "'delete the work scene'. action=run answers at once and opens the "
        "apps in the background; on status=started say only that it is "
        "opening. You will be told separately if an app did not open. To open "
        "ONE application use open_app instead."
    ),
    "brief": "Save and start scenes: several apps, each on its monitor.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "run | save | add_app | remove_app | show | list | delete",
            },
            "name": {"type": "STRING", "description": "The scene's name, e.g. 'work'"},
            "apps": {
                "type": "ARRAY",
                "description": (
                    "save / add_app / remove_app: the applications. save "
                    "replaces the whole scene, so list every app it should hold."
                ),
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "app": {"type": "STRING", "description": "The application's real name, e.g. 'Visual Studio Code'"},
                        "monitor": {
                            "type": "STRING",
                            "description": "Optional: a monitor number (1, 2, 3, 4) or left, right, top, bottom, center, main",
                        },
                        "tile": {
                            "type": "STRING",
                            "description": "Optional Tiling Shell position: left | right | up | down | center | all",
                        },
                    },
                    "required": ["app"],
                },
            },
        },
        "required": ["action"],
    },
    "handler": scene,
}
