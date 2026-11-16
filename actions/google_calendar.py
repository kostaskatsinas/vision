"""
actions/google_calendar.py - Vision's Google Calendar tool.

Reads the user's calendars, finds events, creates, moves and deletes them, and
puts phone reminders on events. It talks to the Google Calendar API with the
user's own OAuth client, so nothing passes through anyone else's server.

One-time setup (docs/VISION.md, "Google Calendar"): the user makes a Desktop
OAuth client in Google Cloud and saves its JSON as
~/.config/vision/google_client.json (a client_secret_*.json left in ~/Downloads
is picked up too). The first calendar request opens the browser to sign in; the
token is kept in ~/.config/vision/google_calendar_token.json, readable only by
the user.

Ask-first rule: deleting an event, and creating one that invites other people
(Google emails them), wait for the user's click on the confirm banner. See
`confirmation()`, which core/ask_first.py calls. Moving or renaming an event
never emails anyone.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "vision"
CLIENT_FILE = CONFIG_DIR / "google_client.json"
TOKEN_FILE = CONFIG_DIR / "google_calendar_token.json"

# Read and write events on every calendar, and see which calendars exist.
# Nothing else: no settings, no sharing, no deleting whole calendars.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
]
SIGN_IN_TIMEOUT_S = 240

_lock = threading.RLock()
_service_cache = None
# The events of the last list or search, so "move number 2" means what the user
# just heard.
_last: list[dict] = []
# Deletes resolved by confirmation(), keyed by the id of the call's argument
# dict. A delete only runs on a target that went through the confirm banner.
_pinned: dict[int, object] = {}

# Tests replace this with a stand-in for the Google client.
_factory = None


class CalendarError(Exception):
    """A problem to say to the user as it is."""


# ── Connection ───────────────────────────────────────────────────────────────

def _find_client_file() -> Optional[Path]:
    if CLIENT_FILE.exists():
        return CLIENT_FILE
    downloads = Path.home() / "Downloads"
    found = sorted(downloads.glob("client_secret_*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True) if downloads.is_dir() else []
    if found:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(found[0], CLIENT_FILE)
        os.chmod(CLIENT_FILE, 0o600)
        return CLIENT_FILE
    return None


def _missing_packages() -> list[str]:
    import importlib.util
    need = {"googleapiclient": "google-api-python-client",
            "google_auth_oauthlib": "google-auth-oauthlib"}
    return [pkg for mod, pkg in need.items() if importlib.util.find_spec(mod) is None]


def _save_token(creds) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    os.replace(tmp, TOKEN_FILE)


def _credentials(interactive: bool):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except Exception:
            creds = None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except Exception as e:
            print(f"[Calendar] token refresh failed: {e}")
            creds = None
    if not interactive:
        raise CalendarError("Google Calendar is not connected yet. Ask me to "
                            "connect your Google Calendar.")

    client = _find_client_file()
    if client is None:
        raise CalendarError(
            "Google Calendar needs a one-time setup first: a Google sign-in key "
            f"saved as {CLIENT_FILE}. The steps are in docs/VISION.md under "
            "Google Calendar.")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(client), SCOPES)
    print("[Calendar] opening the browser to sign in to Google")
    try:
        creds = flow.run_local_server(
            port=0, open_browser=True, timeout_seconds=SIGN_IN_TIMEOUT_S,
            authorization_prompt_message="",
            success_message="Vision can now use your Google Calendar. You can close this tab.")
    except Exception as e:
        raise CalendarError(f"the Google sign-in did not finish ({e}). Ask me to "
                            "connect again when you are ready.")
    if not creds:
        raise CalendarError("the Google sign-in did not finish. Ask me to "
                            "connect again when you are ready.")
    _save_token(creds)
    return creds


def _service(interactive: bool = False):
    global _service_cache
    with _lock:
        if _service_cache is not None:
            return _service_cache
        if _factory is not None:
            _service_cache = _factory()
            return _service_cache
        missing = _missing_packages()
        if missing:
            raise CalendarError("the Google packages are not installed. Run "
                                "./vision.sh once more, or: pip install " + " ".join(missing))
        from googleapiclient.discovery import build
        creds = _credentials(interactive)
        _service_cache = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return _service_cache


def _call(request):
    """Execute a Google request, turning its errors into sentences."""
    global _service_cache
    try:
        return request.execute()
    except CalendarError:
        raise
    except Exception as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        text = str(e)
        if status in (401, 403) or "invalid_grant" in text:
            with _lock:
                _service_cache = None
            if "invalid_grant" in text or status == 401:
                try:
                    TOKEN_FILE.unlink()
                except FileNotFoundError:
                    pass
                raise CalendarError("Google no longer accepts the saved sign-in. "
                                    "Ask me to connect your Google Calendar again.")
            raise CalendarError(f"Google refused the request: {_reason(e)}")
        if status == 404:
            raise CalendarError("Google could not find that event any more.")
        raise CalendarError(f"Google Calendar did not answer properly: {_reason(e)}")


def _reason(e) -> str:
    try:
        body = json.loads(getattr(e, "content", b"") or b"{}")
        return body.get("error", {}).get("message") or str(e)
    except Exception:
        return str(e)[:200]


# ── Dates and times ──────────────────────────────────────────────────────────

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _today() -> date:
    return datetime.now().date()


def parse_date(text) -> date:
    """YYYY-MM-DD, or today / tomorrow / yesterday / a weekday name (the next
    one, today included) / 'next monday' (the next one after today)."""
    s = str(text or "").strip().lower()
    if not s or s == "today":
        return _today()
    if s == "tomorrow":
        return _today() + timedelta(days=1)
    if s == "yesterday":
        return _today() - timedelta(days=1)
    words = s.replace(",", " ").split()
    if words and words[-1] in _WEEKDAYS:
        want = _WEEKDAYS.index(words[-1])
        ahead = (want - _today().weekday()) % 7
        if words[0] == "next" and len(words) > 1:
            ahead = ahead or 7
        return _today() + timedelta(days=ahead)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise CalendarError(f"I could not understand the date '{text}'. Use YYYY-MM-DD.")


def parse_time(text) -> dtime:
    """15:30, 9, 9am, 3:30 pm, noon, midnight."""
    s = str(text or "").strip().lower().replace(".", ":")
    if s in ("noon", "midday"):
        return dtime(12, 0)
    if s == "midnight":
        return dtime(0, 0)
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?", s)
    if not m:
        raise CalendarError(f"I could not understand the time '{text}'. Use HH:MM.")
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if half:
        if not 1 <= hour <= 12:
            raise CalendarError(f"'{text}' is not a valid time.")
        hour = hour % 12 + (12 if half.startswith("p") else 0)
    if hour > 23 or minute > 59:
        raise CalendarError(f"'{text}' is not a valid time.")
    return dtime(hour, minute)


def _aware(d: date, t: dtime) -> datetime:
    # astimezone() on a naive time applies this machine's zone rules for that
    # date, so summer time is right for events months away.
    return datetime.combine(d, t).astimezone()


def _local_zone_name() -> Optional[str]:
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz and "/" in tz:
        return tz
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except Exception:
        pass
    try:
        name = Path("/etc/timezone").read_text().strip()
        return name or None
    except Exception:
        return None


def _start_of(ev: dict) -> datetime:
    start = ev.get("start", {})
    if "dateTime" in start:
        return datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")).astimezone()
    return datetime.combine(date.fromisoformat(start["date"]), dtime(0, 0)).astimezone()


def _end_of(ev: dict) -> datetime:
    end = ev.get("end", {})
    if "dateTime" in end:
        return datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00")).astimezone()
    if "date" in end:
        return datetime.combine(date.fromisoformat(end["date"]), dtime(0, 0)).astimezone()
    return _start_of(ev)


def _day_name(d: date) -> str:
    delta = (d - _today()).days
    label = {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get(delta)
    full = d.strftime("%A %d %B").replace(" 0", " ")
    return f"{label}, {full}" if label else full


def _when(ev: dict) -> str:
    if "date" in ev.get("start", {}):
        return "all day"
    s, e = _start_of(ev), _end_of(ev)
    if s.date() == e.date():
        return f"{s:%H:%M}-{e:%H:%M}"
    return f"{s:%H:%M} until {_day_name(e.date())} {e:%H:%M}"


# ── Reading ──────────────────────────────────────────────────────────────────

def _calendars(svc) -> list[dict]:
    items, token = [], None
    while True:
        page = _call(svc.calendarList().list(pageToken=token, minAccessRole="reader"))
        items += page.get("items", [])
        token = page.get("nextPageToken")
        if not token:
            break
    shown = [c for c in items if c.get("selected", True) and not c.get("hidden")]
    return shown or [{"id": "primary", "summary": "Calendar", "primary": True}]


def _pick_calendar(svc, name) -> dict:
    cals = _calendars(svc)
    want = str(name or "").strip().lower()
    if not want or want in ("primary", "main", "default", "my calendar"):
        return next((c for c in cals if c.get("primary")), cals[0])
    for c in cals:
        if c.get("summary", "").lower() == want or c.get("id", "").lower() == want:
            return c
    for c in cals:
        if want in c.get("summary", "").lower():
            return c
    names = ", ".join(c.get("summary", c["id"]) for c in cals)
    raise CalendarError(f"there is no calendar called '{name}'. Yours are: {names}.")


def _events(svc, start: datetime, end: datetime, query: str = "",
            calendar: str = "") -> list[dict]:
    cals = [_pick_calendar(svc, calendar)] if calendar else _calendars(svc)
    found = []
    for cal in cals:
        token = None
        while True:
            page = _call(svc.events().list(
                calendarId=cal["id"], timeMin=start.isoformat(), timeMax=end.isoformat(),
                singleEvents=True, orderBy="startTime", maxResults=250,
                q=query or None, pageToken=token))
            for ev in page.get("items", []):
                if ev.get("status") == "cancelled":
                    continue
                me = next((a for a in ev.get("attendees", []) if a.get("self")), None)
                if me and me.get("responseStatus") == "declined":
                    continue
                ev["_calendar_id"] = cal["id"]
                ev["_calendar_name"] = "" if cal.get("primary") else cal.get("summary", "")
                found.append(ev)
            token = page.get("nextPageToken")
            if not token:
                break
    found.sort(key=_start_of)
    return found


def _remember(events: list[dict]) -> None:
    global _last
    _last = [{"calendar_id": ev["_calendar_id"], "id": ev["id"],
              "summary": ev.get("summary") or "(no title)",
              "start": ev.get("start"), "end": ev.get("end")} for ev in events]


def _describe(events: list[dict], numbered: bool = True, max_items: int = 40) -> str:
    lines, day = [], None
    for i, ev in enumerate(events[:max_items], 1):
        d = _start_of(ev).date()
        if d != day:
            day = d
            lines.append(f"{_day_name(d)}:")
        title = ev.get("summary") or "(no title)"
        extra = []
        if ev.get("location"):
            extra.append(f"at {ev['location']}")
        if ev.get("_calendar_name"):
            extra.append(f"[{ev['_calendar_name']}]")
        num = f"{i}. " if numbered else ""
        lines.append(f"  {num}{_when(ev)} {title}" + (f" ({', '.join(extra)})" if extra else ""))
    if len(events) > max_items:
        lines.append(f"  ...and {len(events) - max_items} more.")
    return "\n".join(lines)


def _list(svc, p: dict) -> str:
    first = parse_date(p.get("date") or "today")
    days = max(1, min(62, int(p.get("days") or 1)))
    start = _aware(first, dtime(0, 0))
    end = _aware(first + timedelta(days=days), dtime(0, 0))
    events = _events(svc, start, end, calendar=p.get("calendar") or "")
    _remember(events)
    span = _day_name(first) if days == 1 else f"{_day_name(first)} to {_day_name(first + timedelta(days=days - 1))}"
    if not events:
        return f"Nothing on the calendar for {span}."
    return (f"{len(events)} event{'s' if len(events) != 1 else ''} for {span}:\n"
            + _describe(events))


def _search(svc, p: dict) -> str:
    query = str(p.get("query") or p.get("title") or "").strip()
    if not query:
        raise CalendarError("tell me what to look for.")
    start = datetime.now().astimezone() - timedelta(days=int(p.get("days_back") or 0))
    end = start + timedelta(days=max(1, min(366, int(p.get("days") or 90))))
    events = _events(svc, start, end, query=query, calendar=p.get("calendar") or "")
    _remember(events)
    if not events:
        return f"No upcoming events match '{query}' in the next {(end - start).days} days."
    return f"Found {len(events)} upcoming event{'s' if len(events) != 1 else ''} for '{query}':\n" + _describe(events)


# ── Finding one event ────────────────────────────────────────────────────────

def _resolve(svc, p: dict) -> dict:
    """The event the user means: a number from the last list, or words of its
    title (optionally on a date). Raises CalendarError when it is not exactly
    one event."""
    ref = str(p.get("event") or p.get("title") or "").strip()
    if not ref:
        raise CalendarError("which event? Say its name, or list the calendar first "
                            "and give its number.")
    if ref.lstrip("#").isdigit() and len(ref.lstrip("#")) <= 3:
        n = int(ref.lstrip("#"))
        if not _last:
            raise CalendarError("list the calendar first, then I can go by number.")
        if not 1 <= n <= len(_last):
            raise CalendarError(f"there is no number {n} in the last list "
                                f"(it had {len(_last)}).")
        e = _last[n - 1]
        return _call(svc.events().get(calendarId=e["calendar_id"], eventId=e["id"])) | {
            "_calendar_id": e["calendar_id"]}

    if p.get("date"):
        d = parse_date(p["date"])
        start, end = _aware(d, dtime(0, 0)), _aware(d + timedelta(days=1), dtime(0, 0))
    else:
        start = datetime.now().astimezone() - timedelta(hours=12)
        end = start + timedelta(days=60)
    found = _events(svc, start, end, query=ref, calendar=p.get("calendar") or "")
    low = ref.lower()
    exact = [ev for ev in found if (ev.get("summary") or "").strip().lower() == low]
    if len(exact) == 1:
        found = exact
    if not found:
        where = f"on {_day_name(parse_date(p['date']))}" if p.get("date") else "in the next 60 days"
        raise CalendarError(f"I found no event called '{ref}' {where}.")
    if len(found) > 1:
        _remember(found)
        raise CalendarError(f"more than one event matches '{ref}':\n{_describe(found)}\n"
                            "Which number?")
    return found[0]


def _label(ev: dict) -> str:
    title = ev.get("summary") or "(no title)"
    return f"'{title}' ({_day_name(_start_of(ev).date())}, {_when(ev)})"


# ── Writing ──────────────────────────────────────────────────────────────────

def _emails(p: dict) -> list[str]:
    raw = p.get("attendees") or []
    if isinstance(raw, str):
        raw = re.split(r"[,;\s]+", raw)
    return [a.strip() for a in raw if a and "@" in a]


def _times(p: dict, base: Optional[dict] = None) -> tuple[dict, dict, bool]:
    """Start and end bodies for an event from date / time / end_time /
    duration_minutes / all_day. `base` is the event being moved: what is not
    given keeps its old value and the length is kept."""
    all_day = str(p.get("all_day", "")).lower() in ("1", "true", "yes")
    if base is not None and not p.get("time") and not all_day and "date" in base.get("start", {}):
        all_day = True
    old_start = _start_of(base) if base else None
    old_len = (_end_of(base) - old_start) if base else None

    d = parse_date(p["date"]) if p.get("date") else (old_start.date() if base else None)
    if d is None:
        raise CalendarError("on what day?")

    if all_day:
        days = max(1, int(p.get("days") or 1))
        if base and not p.get("days") and "date" in base.get("start", {}):
            days = max(1, (date.fromisoformat(base["end"]["date"]) - date.fromisoformat(base["start"]["date"])).days)
        return {"date": d.isoformat()}, {"date": (d + timedelta(days=days)).isoformat()}, True

    if p.get("time"):
        t = parse_time(p["time"])
    elif base and "dateTime" in base.get("start", {}):
        t = old_start.time().replace(tzinfo=None)
    else:
        raise CalendarError("at what time? Or say it is an all-day event.")
    start = _aware(d, t)
    if p.get("end_time"):
        end = _aware(d, parse_time(p["end_time"]))
        if end <= start:
            end += timedelta(days=1)
    elif p.get("duration_minutes"):
        end = start + timedelta(minutes=max(5, int(p["duration_minutes"])))
    elif old_len and "dateTime" in base.get("start", {}):
        end = start + old_len
    else:
        end = start + timedelta(hours=1)
    zone = _local_zone_name()
    sb, eb = {"dateTime": start.isoformat()}, {"dateTime": end.isoformat()}
    if zone:
        sb["timeZone"] = eb["timeZone"] = zone
    return sb, eb, False


def _reminder_body(p: dict) -> Optional[dict]:
    raw = p.get("reminder_minutes")
    if raw in (None, ""):
        return None
    minutes = [int(m) for m in (raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw))) if str(m).strip().isdigit()]
    if not minutes:
        return None
    return {"useDefault": False,
            "overrides": [{"method": "popup", "minutes": max(0, min(40320, m))} for m in minutes[:5]]}


def _clashes(svc, start: dict, end: dict, skip_id: str = "") -> str:
    if "dateTime" not in start:
        return ""
    s = datetime.fromisoformat(start["dateTime"])
    e = datetime.fromisoformat(end["dateTime"])
    try:
        others = [ev for ev in _events(svc, s, e) if ev["id"] != skip_id and "dateTime" in ev.get("start", {})]
    except CalendarError:
        return ""
    if not others:
        return ""
    names = "; ".join(_label(ev) for ev in others[:3])
    return f" Note: it overlaps {names}."


def _create(svc, p: dict) -> str:
    title = str(p.get("title") or "").strip()
    if not title:
        raise CalendarError("what should the event be called?")
    cal = _pick_calendar(svc, p.get("calendar") or "")
    start, end, all_day = _times(p)
    body = {"summary": title, "start": start, "end": end}
    for key in ("location", "description"):
        if p.get(key):
            body[key] = str(p[key])
    reminders = _reminder_body(p)
    if reminders:
        body["reminders"] = reminders
    guests = _emails(p)
    if guests:
        body["attendees"] = [{"email": g} for g in guests]
    ev = _call(svc.events().insert(calendarId=cal["id"], body=body,
                                   sendUpdates="all" if guests else "none"))
    ev["_calendar_id"] = cal["id"]
    note = _clashes(svc, start, end, skip_id=ev.get("id", ""))
    where = "" if cal.get("primary") else f" in {cal.get('summary')}"
    said = f"Added {_label(ev)}{where}."
    if reminders:
        mins = ", ".join(str(o["minutes"]) for o in reminders["overrides"])
        said += f" Google will remind you {mins} minutes before."
    if guests:
        said += f" Invitations sent to {', '.join(guests)}."
    return said + note


def _update(svc, p: dict) -> str:
    ev = _resolve(svc, p)
    cal_id = ev["_calendar_id"]
    before = _label(ev)
    patch = {}
    if p.get("new_title"):
        patch["summary"] = str(p["new_title"])
    for key in ("location", "description"):
        if p.get(key):
            patch[key] = str(p[key])
    moving = {"date": p.get("new_date"), "time": p.get("new_time"),
              "end_time": p.get("end_time"), "duration_minutes": p.get("duration_minutes"),
              "all_day": p.get("all_day"), "days": p.get("days")}
    moving = {k: v for k, v in moving.items() if v not in (None, "")}
    if moving:
        start, end, _ = _times(moving, base=ev)
        patch["start"], patch["end"] = start, end
    reminders = _reminder_body(p)
    if reminders:
        patch["reminders"] = reminders
    if not patch:
        raise CalendarError("what should change? A new day, time, title, place or reminder.")
    # sendUpdates none: moving an event never emails its guests without asking.
    new = _call(svc.events().patch(calendarId=cal_id, eventId=ev["id"], body=patch,
                                   sendUpdates="none"))
    note = _clashes(svc, new.get("start", {}), new.get("end", {}), skip_id=ev["id"]) if moving else ""
    guests = [a for a in ev.get("attendees", []) if not a.get("self")]
    told = " Guests were not emailed about the change." if guests else ""
    return f"Changed {before} to {_label(new)}.{told}{note}"


def _delete(svc, p: dict) -> str:
    target = _pinned.pop(id(p), None)
    if isinstance(target, str):
        return target                      # why it could not be resolved
    if not isinstance(target, dict):
        return ("Deleting an event needs the user's OK on screen, and this "
                "request did not get one. Nothing was deleted.")
    guests = [a for a in target.get("attendees", []) if not a.get("self")]
    _call(svc.events().delete(calendarId=target["_calendar_id"], eventId=target["id"],
                              sendUpdates="all" if guests else "none"))
    global _last
    _last = [e for e in _last if e["id"] != target["id"]]
    told = " Its guests were told it is cancelled." if guests else ""
    return f"Deleted {_label(target)}.{told}"


# ── Ask-first hook (called by core/ask_first.py) ─────────────────────────────

def confirmation(args: dict) -> Optional[tuple[str, str]]:
    """(title, detail) for the confirm banner, or None when the call may run
    without asking. Deletes are resolved here, before the banner, so the user
    sees exactly which event goes, and that event is the one deleted."""
    action = _action(args)
    if action == "delete":
        if len(_pinned) > 20:              # left by cancelled banners
            _pinned.clear()
        try:
            ev = _resolve(_service(), args)
        except CalendarError as e:
            _pinned[id(args)] = f"Nothing was deleted: {e}"
            return None
        except Exception as e:
            _pinned[id(args)] = f"Nothing was deleted: {e}"
            return None
        _pinned[id(args)] = ev
        guests = [a.get("email") for a in ev.get("attendees", []) if not a.get("self")]
        detail = f"Remove {_label(ev)} from your calendar."
        if guests:
            detail += f" Google will tell {len(guests)} guest(s) it is cancelled."
        return f"Delete event {(ev.get('summary') or '')[:50]}", detail
    if action == "create":
        guests = _emails(args)
        if guests:
            when = " ".join(str(args.get(k) or "") for k in ("date", "time")).strip()
            return (f"Invite {len(guests)} to {str(args.get('title') or 'an event')[:40]}",
                    f"Google will email an invitation for {when or 'the event'} to "
                    + ", ".join(guests) + ".")
    return None


# ── Status, connect, sign out ────────────────────────────────────────────────

def _status() -> str:
    if _missing_packages() and _factory is None:
        return "Google Calendar: the Google packages are not installed yet."
    if TOKEN_FILE.exists() or _factory is not None:
        try:
            cals = _calendars(_service())
            return ("Google Calendar is connected. Calendars: "
                    + ", ".join(c.get("summary", c["id"]) for c in cals) + ".")
        except CalendarError as e:
            return f"Google Calendar: {e}"
    if _find_client_file():
        return "Google Calendar is set up but not signed in. Ask me to connect it."
    return ("Google Calendar is not set up. It needs a one-time Google sign-in key; "
            "the steps are in docs/VISION.md under Google Calendar.")


_signin: Optional[threading.Thread] = None


def _connect(player=None) -> str:
    """Start the Google sign-in and return at once. Signing in takes the user a
    minute in the browser, and a tool must not hold the conversation that long
    (AGENTS.md), so the result is reported back when it is done."""
    global _service_cache, _signin
    if _factory is None:
        missing = _missing_packages()
        if missing:
            raise CalendarError("the Google packages are not installed. Run "
                                "./vision.sh once more, or: pip install " + " ".join(missing))
        if _find_client_file() is None:
            raise CalendarError(
                "Google Calendar needs a one-time setup first: a Google sign-in key "
                f"saved as {CLIENT_FILE}. The steps are in docs/VISION.md under "
                "Google Calendar.")
    if _signin is not None and _signin.is_alive():
        return ("The Google sign-in page is already open in the browser. Finish "
                "signing in there.")
    with _lock:
        _service_cache = None

    def work():
        try:
            cals = _calendars(_service(interactive=True))
            text = ("Google Calendar is now connected. Calendars: "
                    + ", ".join(c.get("summary", c["id"]) for c in cals) + ".")
        except CalendarError as e:
            text = f"Connecting Google Calendar did not work: {e}"
        except Exception as e:
            text = f"Connecting Google Calendar did not work: {type(e).__name__}: {e}"
        print(f"[Calendar] {text}")
        if player:
            try:
                player.write_log(f"[Calendar] {text[:100]}")
            except Exception:
                pass
        try:
            from core import ask_first, outcome
            ask_first.tell("[SYSTEM] The Google Calendar sign-in finished. "
                           + outcome.untrusted("calendar", text)
                           + " Tell the user briefly.")
        except Exception:
            pass

    _signin = threading.Thread(target=work, name="calendar-sign-in", daemon=True)
    _signin.start()
    if TOKEN_FILE.exists() or _factory is not None:
        return "Checking the Google Calendar connection; I will say when it is ready."
    return ("I opened Google's sign-in page in the browser. Sign in there and allow "
            "access to the calendar; I will say when it is done. If Google warns that "
            "the app is not verified, that is the user's own sign-in key: click "
            "Advanced, then Go to Vision.")


def _sign_out() -> str:
    global _service_cache
    with _lock:
        _service_cache = None
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        return ("Signed out of Google Calendar on this computer. To remove the "
                "access completely, also remove Vision at myaccount.google.com/permissions.")
    return "Google Calendar was not signed in."


# ── Tool entry point ─────────────────────────────────────────────────────────

_ACTIONS = {
    "list": ("list", "read", "show", "agenda", "today", "check", "events", "what"),
    "search": ("search", "find", "when", "lookup"),
    "create": ("create", "add", "new", "schedule", "book", "make"),
    "update": ("update", "move", "change", "reschedule", "rename", "edit", "postpone"),
    "delete": ("delete", "remove", "cancel"),
    "connect": ("connect", "login", "sign_in", "signin", "setup", "authorize"),
    "sign_out": ("sign_out", "signout", "logout", "disconnect"),
    "status": ("status",),
}


def _action(p: dict) -> str:
    word = str(p.get("action") or "list").strip().lower().replace(" ", "_").replace("-", "_")
    for action, words in _ACTIONS.items():
        if word in words:
            return action
    return word


def google_calendar(parameters: dict, player=None) -> str:
    p = parameters or {}
    action = _action(p)
    try:
        if action == "status":
            result = _status()
        elif action == "connect":
            result = _connect(player)
        elif action == "sign_out":
            result = _sign_out()
        elif action in ("list", "search", "create", "update", "delete"):
            svc = _service()
            result = {"list": _list, "search": _search, "create": _create,
                      "update": _update, "delete": _delete}[action](svc, p)
        else:
            return (f"Unknown calendar action '{p.get('action')}'. Use list, search, "
                    "create, update, delete, connect, sign_out or status.")
    except CalendarError as e:
        _pinned.pop(id(p), None)
        result = f"Calendar: {e}"
    except Exception as e:
        _pinned.pop(id(p), None)
        print(f"[Calendar] {type(e).__name__}: {e}")
        result = f"Calendar failed: {e}"
    if player:
        try:
            player.write_log(f"[Calendar] {result.splitlines()[0][:80]}")
        except Exception:
            pass
    # Event titles, places and calendar names can be written by other people
    # (an invitation, a shared calendar), so the result goes to the model as
    # data, like a web page. See core/outcome.untrusted().
    from core import outcome
    return outcome.untrusted("calendar", result)


TOOL = {
    "name": "google_calendar",
    "description": (
        "The user's Google Calendar. Use it to say what is on today, tomorrow or "
        "any day or week (action list), find when something is (search), add an "
        "event (create), move, rename or change an event (update), and delete one "
        "(delete). Read the result to the user in a short, natural way. Numbers in "
        "a list let the user say 'move number 2'. For a reminder the user wants "
        "on their phone or in their calendar, create an event with "
        "reminder_minutes; for a quick reminder on this computer only, use the "
        "reminder tool instead. Deleting, and creating an event with attendees "
        "(Google emails them), show an on-screen confirm first: tell the user to "
        "confirm on screen. If the result says the calendar is not connected, "
        "offer to connect it (action connect opens the browser to sign in)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "list | search | create | update | delete | connect | sign_out | status"},
            "date": {"type": "STRING",
                     "description": "YYYY-MM-DD (or today / tomorrow / a weekday). For list: the first day. "
                                    "For create: the event's day. For update/delete: the day the event is on now, to find it"},
            "days": {"type": "INTEGER",
                     "description": "list: how many days from date (1 = that day, 7 = a week). "
                                    "search: how many days ahead to look (default 90). create all-day: length in days"},
            "time": {"type": "STRING", "description": "create: start time HH:MM (24h)"},
            "end_time": {"type": "STRING", "description": "create/update: end time HH:MM (24h)"},
            "duration_minutes": {"type": "INTEGER", "description": "create/update: length in minutes (default 60)"},
            "all_day": {"type": "BOOLEAN", "description": "create/update: an all-day event"},
            "title": {"type": "STRING", "description": "create: the event's name"},
            "query": {"type": "STRING", "description": "search: words to look for in events"},
            "event": {"type": "STRING",
                      "description": "update/delete: the number from the last list or search, or the event's name"},
            "new_date": {"type": "STRING", "description": "update: move to this day (YYYY-MM-DD)"},
            "new_time": {"type": "STRING", "description": "update: move to this start time (HH:MM); the length is kept"},
            "new_title": {"type": "STRING", "description": "update: rename the event"},
            "location": {"type": "STRING", "description": "create/update: where"},
            "description": {"type": "STRING", "description": "create/update: notes"},
            "reminder_minutes": {"type": "STRING",
                                 "description": "create/update: phone/calendar reminder, minutes before; several allowed, e.g. '60, 10'"},
            "attendees": {"type": "STRING",
                          "description": "create: email addresses to invite, comma separated. Only when the user asks to invite people"},
            "calendar": {"type": "STRING",
                         "description": "which calendar by name; leave empty for the main one (list/search: all calendars)"},
        },
        "required": ["action"],
    },
    "handler": google_calendar,
    "tier": "core",
}
