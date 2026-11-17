"""
weather_report — the weather, spoken. No browser is opened.

Data comes from Open-Meteo (open-meteo.com): free, no key, no account. One
call finds the place, one more gets the current conditions and a 16-day daily
forecast. The tool returns plain facts and the model says them in the user's
language, so Greek and English need nothing extra here.

Three questions are answered:
  * "What's the weather?"                     -> now, plus today's range
  * "Weather on Friday / on 21 November?"     -> that one day
  * "Weather for the coming days / next week" -> a short list, one line a day

Which city, when none is named:
  1. config/api_keys.json  "weather_city"  (set it there by hand), else
  2. the home city Vision remembers ("my city is Thessaloniki"), else
  3. Athens, and the reply says so, so the user knows how to change it.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

DEFAULT_CITY = "Athens"
MAX_DAYS = 16                # Open-Meteo's daily forecast reaches 16 days
_TIMEOUT = 8

# WMO weather codes, as words for the model. It speaks them in the user's
# language; they are never shown to anyone as they stand.
WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog",
       48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
       56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
       65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow",
       75: "heavy snow", 77: "snow grains", 80: "rain showers", 81: "rain showers",
       82: "violent rain showers", 85: "snow showers", 86: "heavy snow showers",
       95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail"}

_WEEKDAYS = {
    # English
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
    "saturday": 5, "sunday": 6,
    # Greek, without accents (see _plain)
    "δευτερα": 0, "τριτη": 1, "τεταρτη": 2, "πεμπτη": 3, "παρασκευη": 4,
    "σαββατο": 5, "κυριακη": 6,
}
_RELATIVE = {
    "today": 0, "now": 0, "tonight": 0, "σημερα": 0, "τωρα": 0, "αποψε": 0,
    "tomorrow": 1, "αυριο": 1,
    "day after tomorrow": 2, "the day after tomorrow": 2, "μεθαυριο": 2,
}
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "ιαν": 1, "φεβ": 2, "μαρ": 3, "απρ": 4, "μαι": 5, "ιουν": 6, "ιουλ": 7,
    "αυγ": 8, "σεπ": 9, "οκτ": 10, "νοε": 11, "δεκ": 12,
}

# Places looked up once per run: "Athens" does not move.
_geo_cache: dict[str, dict] = {}
_geo_lock = threading.Lock()


def _plain(text: str) -> str:
    """Lower case, Greek accents removed, spaces squeezed."""
    import unicodedata
    text = unicodedata.normalize("NFD", str(text or "").lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return " ".join(text.split())


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "vision-assistant"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read(2_000_000).decode("utf-8"))


# ── Where ────────────────────────────────────────────────────────────────────
def default_city() -> tuple[str, str]:
    """(city, where it came from): 'config', 'memory' or 'default'."""
    try:
        from memory.config_manager import load_api_keys
        city = str(load_api_keys().get("weather_city") or "").strip()
        if city:
            return city, "config"
    except Exception:
        pass
    try:
        from memory.memory_manager import load_memory
        city = (load_memory().get("identity") or {}).get("city") or ""
        city = str(city.get("value") if isinstance(city, dict) else city).strip()
        if city:
            return city, "memory"
    except Exception:
        pass
    return DEFAULT_CITY, "default"


def find_place(city: str) -> dict:
    """The best Open-Meteo match for `city`. "Paris, Texas" or "Larissa, GR"
    narrows by country (name or two-letter code)."""
    key = _plain(city)
    with _geo_lock:
        if key in _geo_cache:
            return _geo_cache[key]
    name, _, country = city.partition(",")
    name, country = name.strip(), _plain(country)
    geo = _get_json("https://geocoding-api.open-meteo.com/v1/search?count=10&format=json&name="
                    + urllib.parse.quote(name))
    hits = geo.get("results") or []
    if country:
        narrowed = [h for h in hits
                    if country in (_plain(h.get("country")), _plain(h.get("country_code")))]
        hits = narrowed or hits
    if not hits:
        raise LookupError(f"no place called '{city}' was found")
    place = hits[0]
    with _geo_lock:
        _geo_cache[key] = place
    return place


# ── When ─────────────────────────────────────────────────────────────────────
def resolve_date(text: str | None, today: date) -> date | None:
    """The day the user means, or None when the words are not a day.

    Accepts YYYY-MM-DD, DD/MM[/YYYY], DD.MM[.YYYY], '21 November', 'today',
    'tomorrow', 'day after tomorrow', weekday names ('Friday' is the next
    Friday, today included), and the same words in Greek."""
    raw = _plain(text)
    if not raw:
        return today
    raw = re.sub(r"^(on|for|next|this|την|τη|το|στισ|στις|αυτη την|αυτην την|επομενη)\s+", "", raw)
    if raw in _RELATIVE:
        return today + timedelta(days=_RELATIVE[raw])
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?", raw)
    if m:
        return _day_month(int(m[1]), int(m[2]), m[3], today)
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([^\d\s]+)(?:\s+(\d{4}))?", raw)
    if m and _month(m[2]):
        return _day_month(int(m[1]), _month(m[2]), m[3], today)
    m = re.fullmatch(r"([^\d\s]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(\d{4}))?", raw)
    if m and _month(m[1]):
        return _day_month(int(m[2]), _month(m[1]), m[3], today)
    if raw in _WEEKDAYS:
        return today + timedelta(days=(_WEEKDAYS[raw] - today.weekday()) % 7)
    return None


def _month(word: str) -> int | None:
    """'november', 'Nov', 'νοεμβριου' -> 11. Greek June and July share three
    letters, so four are tried first."""
    return _MONTHS.get(word[:4]) or (_MONTHS.get(word[:3]) if word[:3] != "ιου" else None)


def _day_month(day: int, month: int, year: str | None, today: date) -> date | None:
    try:
        if year:
            y = int(year)
            return date(y + 2000 if y < 100 else y, month, day)
        d = date(today.year, month, day)
        # "5 January" said in December is next year's.
        return d if d >= today - timedelta(days=1) else date(today.year + 1, month, day)
    except ValueError:
        return None


# ── The forecast ─────────────────────────────────────────────────────────────
def fetch_forecast(place: dict) -> dict:
    query = urllib.parse.urlencode({
        "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
        "forecast_days": MAX_DAYS,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "weather_code,wind_speed_10m,precipitation",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
    })
    return _get_json("https://api.open-meteo.com/v1/forecast?" + query)


def place_today(fc: dict, now: datetime | None = None) -> date:
    """Today where the place is, not where the computer is."""
    now = now or datetime.now(timezone.utc)
    offset = timedelta(seconds=int(fc.get("utc_offset_seconds") or 0))
    return (now.astimezone(timezone.utc) + offset).date()


def _num(v, unit: str = "") -> str:
    if v is None:
        return "?"
    v = round(float(v))
    return f"{v}{unit}"


def _day_line(daily: dict, i: int) -> str:
    def at(key):
        vals = daily.get(key) or []
        return vals[i] if i < len(vals) else None
    d = date.fromisoformat(daily["time"][i])
    parts = [f"{d:%A %d %B}: {WMO.get(at('weather_code'), 'unknown')}",
             f"{_num(at('temperature_2m_min'))} to {_num(at('temperature_2m_max'), '°C')}",
             f"rain chance {_num(at('precipitation_probability_max'), '%')}"]
    rain = at("precipitation_sum")
    if rain:
        parts.append(f"{rain:.1f} mm")
    parts.append(f"wind up to {_num(at('wind_speed_10m_max'), ' km/h')}")
    return ", ".join(parts)


def _place_name(place: dict) -> str:
    return ", ".join(x for x in (place.get("name"), place.get("country")) if x)


def describe(place: dict, fc: dict, when: str | None, days: int | None,
             now: datetime | None = None) -> str:
    """The facts the model will speak. Plain text, one fact per clause."""
    daily = fc.get("daily") or {}
    times = daily.get("time") or []
    today = place_today(fc, now)
    name = _place_name(place)

    if days:
        days = max(1, min(int(days), MAX_DAYS))
        start = 0
        if when and _plain(when) not in ("", "today", "σημερα", "now", "τωρα"):
            first = resolve_date(when, today)
            if first is None:
                return f"I could not tell which day '{when}' means."
            start = (first - today).days
        idx = [i for i in range(start, start + days)
               if 0 <= i < len(times)]
        if not idx:
            return f"The forecast for {name} only reaches {MAX_DAYS} days ahead."
        lines = [f"Forecast for {name}, {len(idx)} day(s):"]
        lines += [_day_line(daily, i) for i in idx]
        return "\n".join(lines)

    target = resolve_date(when, today)
    if target is None:
        return (f"I could not tell which day '{when}' means. "
                "Ask the user for the day, or pass it as YYYY-MM-DD.")
    offset = (target - today).days
    if offset < 0:
        return f"{target:%A %d %B} has already passed; only forecasts are available."
    if offset >= len(times):
        return (f"{target:%A %d %B} is too far ahead: the forecast for {name} "
                f"only reaches {MAX_DAYS} days.")

    if offset == 0:
        cur = fc.get("current") or {}
        out = (f"Weather in {name} now: {WMO.get(cur.get('weather_code'), 'unknown')}, "
               f"{_num(cur.get('temperature_2m'), '°C')} "
               f"(feels like {_num(cur.get('apparent_temperature'), '°C')}), "
               f"humidity {_num(cur.get('relative_humidity_2m'), '%')}, "
               f"wind {_num(cur.get('wind_speed_10m'), ' km/h')}.")
        return out + "\nToday: " + _day_line(daily, 0).split(": ", 1)[1] + "."
    return f"Forecast for {name}, " + _day_line(daily, offset) + "."


# ── The tool ─────────────────────────────────────────────────────────────────
def weather_action(parameters: dict, player=None, session_memory=None) -> str:
    parameters = parameters or {}
    city = str(parameters.get("city") or "").strip()
    when = str(parameters.get("date") or parameters.get("time") or "").strip()
    days = parameters.get("days")
    try:
        days = int(days) if days not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        days = None

    source = "named"
    if not city:
        city, source = default_city()

    started = time.monotonic()
    try:
        place = find_place(city)
        fc = fetch_forecast(place)
        text = describe(place, fc, when, days)
    except LookupError as e:
        return _done(f"I could not find that place: {e}. Ask the user to say the city again.", player)
    except Exception as e:
        return _done(f"The weather service did not answer ({type(e).__name__}). "
                     "Tell the user to try again in a minute.", player)

    print(f"[Weather] {city} {when or 'now'} days={days} in {time.monotonic() - started:.1f}s")
    if player:
        try:
            player.show_content(f"WEATHER — {_place_name(place)[:34]}", text)
        except Exception:
            pass

    note = ("\nSay this briefly and naturally in the user's language; round numbers, "
            "do not read out every figure, and do not open a browser.")
    if source == "default":
        note += (f"\nNo home city is set, so this is for {DEFAULT_CITY}. Mention that once, "
                 "and that the user can say which city they live in so you remember it.")
    if session_memory:
        try:
            session_memory.set_last_search(query=f"weather {city} {when}", response=text)
        except Exception:
            pass
    return text + note


def _done(msg: str, player=None) -> str:
    print(f"[Weather] {msg}")
    if player:
        try:
            player.write_log(f"VISION: {msg}")
        except Exception:
            pass
    return msg


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "weather_report",
    "description": (
        "Gets the weather and tells it by voice; it never opens a browser. Use for any "
        "question about weather, temperature, rain, wind or the forecast. Leave 'city' "
        "empty for the user's home city. For one day, put it in 'date' (YYYY-MM-DD is "
        "best; 'tomorrow' or a weekday name also work). For several days ('this week', "
        "'the next 5 days', 'the weekend'), put the number of days in 'days' and the "
        "first day in 'date'. Forecasts reach 16 days ahead."
    ),
    "brief": "The weather now, on a given day, or for the coming days, spoken.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "city": {
                "type": "STRING",
                "description": "City name, optionally with country ('Paris, France'). Empty = the user's city.",
            },
            "date": {
                "type": "STRING",
                "description": "The day: YYYY-MM-DD, 'today', 'tomorrow' or a weekday. Empty = now.",
            },
            "days": {
                "type": "INTEGER",
                "description": "For a multi-day forecast: how many days, 1 to 16. Leave empty for one day.",
            },
        },
    },
    "handler": weather_action,
}
