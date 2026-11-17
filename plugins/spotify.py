"""plugins/spotify.py - "play Radiohead on Spotify".

computer_settings already does play / pause / next for whatever is playing.
This plugin adds the part that was missing: choosing WHAT plays, by name.

How it works
    1. The name is looked up with Spotify's search API. That needs a client id
       and secret from a free Spotify developer app (docs/VISION.md, "Spotify").
       Search uses the app's own token, so nothing here signs in as you and
       nothing can change your account, playlists or library.
    2. The result's link (spotify:track:...) is handed to the Spotify desktop
       app over MPRIS, the same desktop interface the media keys use. Spotify
       is started first if it is not running.
    3. What Spotify says it is playing is read back, so "done" means it was
       seen playing, and anything less is reported as not confirmed.

Without the client id and secret it still opens Spotify's own search page for
the name, and says that it could not press play for you.
"""
from __future__ import annotations

import base64
import shutil
import subprocess
import threading
import time
from urllib.parse import quote

from core import outcome

_NS = "spotify"
_MPRIS = "org.mpris.MediaPlayer2.spotify"
_KINDS = ("track", "album", "artist", "playlist")
_token: dict = {"value": "", "until": 0.0, "for": ""}
_token_lock = threading.Lock()


# -- settings -----------------------------------------------------------------
def _creds() -> tuple[str, str]:
    try:
        from memory.config_manager import get_plugin_config
        cfg = get_plugin_config(_NS)
    except Exception:
        cfg = {}
    return (str(cfg.get("client_id") or "").strip(), str(cfg.get("client_secret") or "").strip())


# -- Spotify's web API (search only) ------------------------------------------
def _http():
    import requests
    return requests


def _get_token(client_id: str, secret: str, http=None) -> str:
    with _token_lock:
        if _token["value"] and _token["for"] == client_id and time.time() < _token["until"]:
            return _token["value"]
        http = http or _http()
        basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        r = http.post("https://accounts.spotify.com/api/token",
                      data={"grant_type": "client_credentials"},
                      headers={"Authorization": f"Basic {basic}"}, timeout=8)
        if r.status_code in (400, 401):
            raise PermissionError("Spotify refused the client id and secret")
        r.raise_for_status()
        body = r.json()
        _token.update(value=body["access_token"], until=time.time() + int(body.get("expires_in", 3600)) - 60,
                      **{"for": client_id})
        return _token["value"]


def _search(query: str, kind: str, artist: str = "", http=None) -> dict | None:
    """The best match as {uri, name, by, kind}, or None when nothing matched."""
    client_id, secret = _creds()
    http = http or _http()
    token = _get_token(client_id, secret, http)
    q = query if not artist or kind == "artist" else f"{query} artist:{artist}"
    r = http.get("https://api.spotify.com/v1/search",
                 params={"q": q, "type": kind, "limit": 5},
                 headers={"Authorization": f"Bearer {token}"}, timeout=8)
    if r.status_code == 401:
        with _token_lock:
            _token["value"] = ""
        raise PermissionError("Spotify did not accept the app's token")
    if r.status_code == 403:
        raise PermissionError("Spotify refused the search (the developer app may need "
                              "a Premium account behind it)")
    r.raise_for_status()
    items = [i for i in ((r.json().get(kind + "s") or {}).get("items") or []) if i]
    if not items:
        return None
    best = items[0]
    if kind == "artist":
        # "Radiohead" must not lose to "Radiohead Tribute Band".
        exact = [i for i in items if str(i.get("name", "")).lower() == query.lower()]
        best = exact[0] if exact else best
    by = ""
    if kind in ("track", "album"):
        by = ", ".join(a.get("name", "") for a in best.get("artists") or [] if a)
    elif kind == "playlist":
        by = str((best.get("owner") or {}).get("display_name") or "")
    return {"uri": best["uri"], "name": str(best.get("name") or ""), "by": by, "kind": kind}


# -- the desktop app ------------------------------------------------------------
def _run(cmd: list[str], timeout: float = 6.0) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 and r.stderr.strip():
            print(f"[Spotify] {cmd[0]}: {r.stderr.strip()[:200]}")
        return r.returncode, r.stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[Spotify] {cmd[0]} failed: {e}")
        return 1, ""


def _spotify_running() -> bool:
    if shutil.which("gdbus"):
        code, out = _run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
                          "--object-path", "/org/freedesktop/DBus",
                          "--method", "org.freedesktop.DBus.NameHasOwner", _MPRIS])
        if code == 0:
            return "true" in out.lower()
    if shutil.which("playerctl"):
        code, out = _run(["playerctl", "--list-all"])
        return code == 0 and any(line.strip().startswith("spotify") for line in out.splitlines())
    return False


def _open_uri(uri: str) -> bool:
    if shutil.which("playerctl"):
        code, _ = _run(["playerctl", "--player=spotify", "open", uri])
        if code == 0:
            return True
    if shutil.which("gdbus"):
        code, _ = _run(["gdbus", "call", "--session", "--dest", _MPRIS,
                        "--object-path", "/org/mpris/MediaPlayer2",
                        "--method", "org.mpris.MediaPlayer2.Player.OpenUri", uri])
        return code == 0
    return False


def _start_spotify(uri: str, wait: float = 25.0) -> bool:
    """Start the app through its own link handler, which works for the .deb,
    snap and flatpak builds alike. True once it answers on the session bus."""
    if not shutil.which("xdg-open"):
        return False
    _run(["xdg-open", uri])
    end = time.time() + wait
    while time.time() < end:
        if _spotify_running():
            time.sleep(1.5)      # the bus name appears before the player is ready
            return True
        time.sleep(0.5)
    return False


def _now_playing() -> dict | None:
    if not shutil.which("playerctl"):
        return None
    code, out = _run(["playerctl", "--player=spotify", "metadata", "--format",
                      "{{status}}\t{{artist}}\t{{title}}\t{{album}}\t{{mpris:trackid}}"])
    if code != 0 or not out:
        return None
    parts = (out.split("\t") + [""] * 5)[:5]
    return {"status": parts[0], "artist": parts[1], "title": parts[2],
            "album": parts[3], "trackid": parts[4]}


def _matches(found: dict, now: dict) -> bool:
    """Is what Spotify reports playing the thing that was asked for?"""
    if now.get("status") != "Playing":
        return False
    name = found["name"].lower()
    if found["kind"] == "track":
        return found["uri"].rsplit(":", 1)[-1] in now.get("trackid", "") or name == now.get("title", "").lower()
    if found["kind"] == "album":
        return name == now.get("album", "").lower()
    if found["kind"] == "artist":
        return name in now.get("artist", "").lower()
    return True      # a playlist: any track playing after the request counts


def _play(found: dict, before: dict | None) -> str:
    """Hand the link to the app and read back what plays."""
    if not _spotify_running():
        if not _start_spotify(found["uri"]):
            return outcome.failed("spotify", "the Spotify app did not start; is it installed?",
                                  wanted=found["name"])
    if not _open_uri(found["uri"]):
        return outcome.failed("spotify", "Spotify did not accept the request to play; "
                              "playerctl or gdbus is needed", wanted=found["name"])
    label = found["name"] + (f" by {found['by']}" if found["by"] else "")
    before_id = (before or {}).get("trackid", "")
    end = time.time() + 4.0
    now = None
    while time.time() < end:
        time.sleep(0.5)
        now = _now_playing()
        if now and _matches(found, now) and (found["kind"] != "playlist" or now["trackid"] != before_id
                                             or before is None or before.get("status") != "Playing"):
            return outcome.done("spotify", playing=f"{now['title']} by {now['artist']}",
                                asked_for=label, kind=found["kind"], verified=True)
    if now is None:
        return outcome.unverified("spotify", "Spotify was asked to play it, but what is "
                                  "playing could not be read back (playerctl is not installed)",
                                  asked_for=label)
    return outcome.unverified(
        "spotify", "Spotify was asked to play it, but it reports "
        + (f"'{now['title']}' by {now['artist']} ({now['status'].lower()})" if now.get("title")
           else "nothing playing"), asked_for=label)


def _play_in_background(found: dict, before, player) -> None:
    try:
        result = _play(found, before)
    except Exception as e:
        result = outcome.failed("spotify", f"{type(e).__name__}: {e}")
    print(f"[Spotify] {result[:200]}")
    if player is None:
        return
    try:
        if outcome.is_problem(result):
            if hasattr(player, "request_say"):
                player.request_say("Tell the user, in one short sentence in their own "
                                   "language, exactly what this says happened: " + result)
        else:
            player.write_log(f"VISION: Spotify - {found['name']}"
                             + (f" by {found['by']}" if found["by"] else ""))
    except Exception:
        pass


def _open_search_page(query: str) -> str:
    uri = "spotify:search:" + quote(query)
    ok = _open_uri(uri) if _spotify_running() else _start_spotify(uri, wait=20)
    if not ok:
        return outcome.failed("spotify", "Spotify could not be opened")
    return outcome.unverified(
        "spotify", "Spotify's search page for it is open, but nothing was played: "
        "choosing a result needs the Spotify client id and secret, which are not "
        "set (SETUP, plugin settings; steps in docs/VISION.md)", searched=query)


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "play").strip().lower()
    try:
        if action in ("now_playing", "what", "current"):
            now = _now_playing()
            if not now or not now.get("title"):
                return outcome.done("spotify", playing="nothing",
                                    note="Spotify is not playing or playerctl is not installed")
            return outcome.done("spotify", status=now["status"].lower(), title=now["title"],
                                artist=now["artist"], album=now["album"] or None)

        if action != "play":
            return outcome.failed("spotify", f"unknown action '{action}'; for pause, next "
                                  "and previous use computer_settings")

        query = " ".join(str(p.get("query") or "").split())
        if not query:
            return outcome.ask("spotify", "What should Spotify play?")
        kind = str(p.get("kind") or "track").strip().lower()
        if kind not in _KINDS:
            kind = "track"
        artist = " ".join(str(p.get("artist") or "").split())

        if not all(_creds()):
            return _open_search_page(f"{query} {artist}".strip())

        try:
            found = _search(query, kind, artist)
        except PermissionError as e:
            return outcome.failed("spotify", f"{e}; check the client id and secret in SETUP")
        except Exception as e:
            return outcome.failed("spotify", f"Spotify search could not be reached ({type(e).__name__})")
        if found is None:
            return outcome.failed("spotify", f"Spotify has no {kind} matching that", searched=query)

        before = _now_playing()
        if _spotify_running():
            return _play(found, before)
        threading.Thread(target=_play_in_background, args=(found, before, player),
                         daemon=True, name="spotify-play").start()
        return outcome.started("spotify", opening="Spotify", will_play=found["name"],
                               by=found["by"] or None)
    except Exception as e:
        return outcome.failed("spotify", f"{type(e).__name__}: {e}")


def _test_connection(values: dict | None = None) -> tuple[bool, str]:
    """The TEST button in SETUP. The form saves the fields before calling."""
    try:
        if not all(_creds()):
            return False, "Enter the client ID and the client secret first."
        found = _search("Daft Punk", "artist")
        if found:
            return True, f"Connected. Search works (found {found['name']})."
        return False, "Connected, but the test search found nothing."
    except Exception as e:
        return False, f"Not working: {e}"


PLUGIN_SETTINGS = {
    "namespace": _NS,
    "title": "SPOTIFY",
    "fields": [
        {"key": "client_id", "label": "Client ID", "type": "text",
         "placeholder": "from developer.spotify.com/dashboard"},
        {"key": "client_secret", "label": "Client secret", "type": "password"},
    ],
    "action": {"label": "TEST", "run": _test_connection},
}

PLUGIN = {
    "name": "spotify",
    "description": (
        "Plays a named song, album, artist or playlist in the Spotify desktop "
        "app, and tells what Spotify is playing. Use for 'play Radiohead on "
        "Spotify', 'play the album OK Computer', 'put on my Discover Weekly "
        "playlist', 'play Karma Police', 'what song is this'. Set 'kind' to "
        "track, album, artist or playlist from what the user asked for; a bare "
        "band or singer name is kind=artist. For pause, resume, next, previous "
        "or volume use computer_settings, not this tool. To play a VIDEO or "
        "something on YouTube use the video tools, not this tool."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "play | now_playing"},
            "query": {"type": "STRING", "description": "play: the name of the song, album, artist or playlist"},
            "kind": {"type": "STRING", "description": "play: track | album | artist | playlist"},
            "artist": {"type": "STRING", "description": "play: the artist, when the user named one with a song or album"},
        },
        "required": ["action"],
    },
}
