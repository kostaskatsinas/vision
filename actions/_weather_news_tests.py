"""
Tests for the spoken weather and the Greek news.
Run with:  python -m actions._weather_news_tests

No network is needed: Open-Meteo and the RSS feeds are replaced by canned
answers shaped like the real ones. What these cannot show is the real
services answering from your PC; say "what's the weather?" and "any news?"
there to check that.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from actions import greek_news as gn
from actions import weather_report as wr

_passed = 0
_failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


class Player:
    def __init__(self):
        self.shown, self.log = [], []

    def show_content(self, title, text):
        self.shown.append((title, text))

    def write_log(self, text):
        self.log.append(text)


# ── dates ────────────────────────────────────────────────────────────────────
TODAY = date(2026, 11, 17)          # a Tuesday
R = wr.resolve_date
check("empty is today", R("", TODAY) == TODAY)
check("today", R("today", TODAY) == TODAY)
check("tomorrow", R("tomorrow", TODAY) == date(2026, 11, 18))
check("day after tomorrow", R("the day after tomorrow", TODAY) == date(2026, 11, 19))
check("Greek tomorrow", R("αύριο", TODAY) == date(2026, 11, 18))
check("Greek day after", R("Μεθαύριο", TODAY) == date(2026, 11, 19))
check("ISO", R("2026-11-25", TODAY) == date(2026, 11, 25))
check("dd/mm", R("21/11", TODAY) == date(2026, 11, 21))
check("dd.mm.yyyy", R("3.12.2026", TODAY) == date(2026, 12, 3))
check("21 November", R("21 November", TODAY) == date(2026, 11, 21))
check("November 21st", R("November 21st", TODAY) == date(2026, 11, 21))
check("Greek month", R("21 Νοεμβρίου", TODAY) == date(2026, 11, 21))
check("Greek June not July", R("5 Ιουνίου", TODAY) == date(2027, 6, 5))
check("Greek July", R("5 Ιουλίου", TODAY) == date(2027, 7, 5))
check("January said in November is next year", R("5 January", TODAY) == date(2027, 1, 5))
check("Friday", R("friday", TODAY) == date(2026, 11, 20))
check("on Friday", R("on Friday", TODAY) == date(2026, 11, 20))
check("weekday today counts", R("Tuesday", TODAY) == TODAY)
check("Greek Saturday", R("το Σάββατο", TODAY) == date(2026, 11, 21))
check("Greek Friday", R("Παρασκευή", TODAY) == date(2026, 11, 20))
check("nonsense", R("whenever", TODAY) is None)
check("bad date", R("2026-02-30", TODAY) is None)


# ── forecast text ────────────────────────────────────────────────────────────
DAYS = [f"2026-11-{d:02d}" for d in range(17, 17 + 16)][:14] + ["2026-12-01", "2026-12-02"]
FC = {
    "utc_offset_seconds": 7200, "timezone": "Europe/Athens",
    "current": {"temperature_2m": 18.4, "apparent_temperature": 17.1,
                "relative_humidity_2m": 62, "weather_code": 2, "wind_speed_10m": 11.3},
    "daily": {"time": DAYS,
              "weather_code": [2, 61, 0, 3, 95] + [1] * 11,
              "temperature_2m_max": [20.2, 17.0, 19.5, 18, 15] + [16] * 11,
              "temperature_2m_min": [11.6, 10.1, 9.8, 10, 8] + [7] * 11,
              "precipitation_probability_max": [5, 80, 0, 10, 90] + [0] * 11,
              "precipitation_sum": [0, 6.25, 0, 0, 12] + [0] * 11,
              "wind_speed_10m_max": [20, 30, 12, 15, 45] + [10] * 11},
}
PLACE = {"name": "Athens", "country": "Greece", "latitude": 37.98, "longitude": 23.72,
         "country_code": "GR"}
NOW = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)

t = wr.describe(PLACE, FC, "", None, NOW)
check("now names place", "Athens, Greece" in t, t)
check("now has temperature", "18°C" in t and "feels like 17°C" in t, t)
check("now has condition", "partly cloudy" in t, t)
check("now has today's range", "12 to 20°C" in t, t)

t = wr.describe(PLACE, FC, "tomorrow", None, NOW)
check("tomorrow is Wednesday 18", "Wednesday 18 November" in t, t)
check("tomorrow rain", "light rain" in t and "80%" in t and "6.2 mm" in t, t)

t = wr.describe(PLACE, FC, "2026-11-21", None, NOW)
check("a date", "Saturday 21 November" in t and "thunderstorm" in t, t)

t = wr.describe(PLACE, FC, "", 5, NOW)
check("5 days has 5 lines", t.count("\n") == 5, t)
check("5 days starts today", "Tuesday 17 November" in t and "Saturday 21 November" in t, t)

t = wr.describe(PLACE, FC, "friday", 3, NOW)
check("3 days from Friday", "Friday 20 November" in t and "Sunday 22 November" in t
      and "Thursday" not in t, t)

t = wr.describe(PLACE, FC, "", 40, NOW)
check("days capped at 16", t.count("\n") == 16, t)

check("past day", "already passed" in wr.describe(PLACE, FC, "2026-11-10", None, NOW))
check("too far", "too far ahead" in wr.describe(PLACE, FC, "2027-01-30", None, NOW))
check("unknown words", "could not tell" in wr.describe(PLACE, FC, "someday", None, NOW))

late = datetime(2026, 11, 17, 22, 30, tzinfo=timezone.utc)      # 00:30 in Athens
check("today is the place's today", wr.place_today(FC, late) == date(2026, 11, 18))


# ── the tool, end to end, with Open-Meteo replaced ───────────────────────────
calls = []


def fake_get(url):
    calls.append(url)
    if "geocoding" in url:
        if "Nowhere" in url:
            return {}
        if "Paris" in url:
            return {"results": [
                {"name": "Paris", "country": "France", "country_code": "FR", "latitude": 48.8, "longitude": 2.3},
                {"name": "Paris", "country": "United States", "country_code": "US", "latitude": 33.6, "longitude": -95.5}]}
        return {"results": [PLACE]}
    return FC


wr._get_json = fake_get
wr._geo_cache.clear()
wr.default_city = lambda: ("Athens", "default")

p = Player()
out = wr.weather_action({}, player=p)
check("tool: no browser, facts back", "Weather in Athens" in out, out)
check("tool: default city mentioned", "No home city is set" in out, out)
check("tool: panel shown", p.shown and "WEATHER" in p.shown[0][0], str(p.shown))
check("tool: never opens a browser", not any("google.com" in u for u in calls))

wr.default_city = lambda: ("Thessaloniki", "memory")
out = wr.weather_action({"date": "tomorrow"})
check("tool: remembered city gets no note", "No home city" not in out, out)

out = wr.weather_action({"city": "Paris, US"})
check("tool: country narrows", "United States" in out, out)
out = wr.weather_action({"city": "Paris"})
check("tool: first match without country", "France" in out, out)

out = wr.weather_action({"city": "Nowhere"})
check("tool: unknown place", "could not find" in out, out)

n = len([u for u in calls if "geocoding" in u])
wr.weather_action({"city": "Paris"})
check("tool: place cached", len([u for u in calls if "geocoding" in u]) == n)

out = wr.weather_action({"days": "4"})
check("tool: days as text", out.count("\n") >= 4 and "4 day(s)" in out, out)


def broken(url):
    raise TimeoutError("slow")


wr._get_json = broken
wr._geo_cache.clear()
out = wr.weather_action({"city": "Athens"})
check("tool: service down is a sentence", "did not answer" in out, out)

check("declaration: no required city", "required" not in wr.TOOL["parameters"])
check("declaration: says no browser", "never opens a browser" in wr.TOOL["description"])


# ── news: parsing ────────────────────────────────────────────────────────────
RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>ERT</title>
<item><title>Συνάντηση για τον προϋπολογισμό &amp; την οικονομία</title>
<pubDate>Tue, 17 Nov 2026 12:00:00 +0200</pubDate>
<description><![CDATA[<p>Η κυβέρνηση παρουσίασε τον <b>προϋπολογισμό</b>.</p>]]></description></item>
<item><title>Ολυμπιακός: νίκη στο ντέρμπι</title>
<pubDate>Tue, 17 Nov 2026 09:00:00 +0200</pubDate><description>Αθλητικά.</description></item>
<item><title></title></item>
</channel></rss>""".encode("utf-8")

GOOGLE = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Economy grows 2.1% in third quarter - eKathimerini.com</title>
<pubDate>Tue, 17 Nov 2026 08:00:00 GMT</pubDate>
<description>&lt;a href="x"&gt;Economy grows 2.1% in third quarter&lt;/a&gt;</description>
<source url="https://www.ekathimerini.com">eKathimerini.com</source></item>
</channel></rss>""".encode("utf-8")

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Atom story</title><updated>2026-11-17T11:00:00Z</updated>
<summary>Short.</summary></entry></feed>""".encode("utf-8")

rows = gn.parse_feed(RSS, "ERT News")
check("rss: two items, empty title dropped", len(rows) == 2, str(rows))
check("rss: entities and tags cleaned", rows[0]["title"].endswith("& την οικονομία")
      and rows[0]["summary"] == "Η κυβέρνηση παρουσίασε τον προϋπολογισμό .", str(rows[0]))
check("rss: time parsed", rows[0]["ts"] > rows[1]["ts"] > 0)
check("rss: source", rows[0]["source"] == "ERT News")

g = gn.parse_feed(GOOGLE, "Google News")
check("google: outlet split from title", g[0]["title"] == "Economy grows 2.1% in third quarter", str(g))
check("google: outlet as source", g[0]["source"] == "eKathimerini.com", str(g))
check("google: repeated title not a summary", g[0]["summary"] == "", str(g))

a = gn.parse_feed(ATOM, "x")
check("atom parsed", a and a[0]["title"] == "Atom story" and a[0]["ts"] > 0, str(a))
check("bad xml is empty", gn.parse_feed(b"<rss><item>", "x") == [])

dup = [{"title": "Same story!", "summary": "", "source": "A", "ts": 2},
       {"title": "same story", "summary": "", "source": "B", "ts": 1},
       {"title": "Other", "summary": "", "source": "B", "ts": 3}]
m = gn.merge([dup], 10)
check("merge: newest first, duplicates gone", [r["title"] for r in m] == ["Other", "Same story!"], str(m))

check("topic filter: Greek ending changes", len(gn.filter_topic(rows, "προϋπολογισμός")) == 1)
check("topic filter: no match", gn.filter_topic(rows, "Μητσοτάκης") == [])


# ── news: the tool ───────────────────────────────────────────────────────────
fetched = []


def fake_fetch(url):
    fetched.append(url)
    if "news.google.com" in url:
        return GOOGLE
    if "in.gr" in url:
        raise TimeoutError("down")          # one outlet failing is fine
    return RSS


gn._fetch = fake_fetch
gn._feeds = lambda: gn.FEEDS
gn._cache.clear()

p = Player()
out = gn.greek_news({}, player=p)
check("news: headlines returned", "Ολυμπιακός" in out and "προϋπολογισμό" in out, out)
check("news: wrapped as untrusted", "<<<UNTRUSTED" in out, out)
check("news: asks for a short summary", "short spoken summary" in out, out)
check("news: outlets used, not Google, for top stories",
      not any("google" in u for u in fetched), str(fetched))
check("news: deduped across outlets", out.count("Ολυμπιακός") == 1, out)
check("news: panel shown", p.shown and p.shown[0][0] == "NEWS — GREECE", str(p.shown))

n = len(fetched)
gn.greek_news({})
check("news: cached for a few minutes", len(fetched) == n)

out = gn.greek_news({"topic": "economy", "language": "en"})
check("news: topic goes to Google News", "Economy grows" in out and "about 'economy'" in out, out)
check("news: english search url", any("hl=en" in u and "ceid=GR%3Aen" in u for u in fetched), str(fetched))


def google_down(url):
    if "news.google.com" in url:
        raise TimeoutError("down")
    return RSS


gn._fetch = google_down
gn._cache.clear()
out = gn.greek_news({"topic": "προϋπολογισμός"})
check("news: topic falls back to outlet filter", "προϋπολογισμό" in out and "Ολυμπιακός" not in out, out)


def all_down(url):
    raise TimeoutError("down")


gn._fetch = all_down
gn._cache.clear()
out = gn.greek_news({})
check("news: everything down is a sentence", "did not answer" in out, out)

out = gn.greek_news({"count": "lots"})
check("news: bad count does not raise", isinstance(out, str))

check("declaration: name", gn.TOOL["name"] == "greek_news")


print(f"\n{_passed} passed, {_failed} failed")
raise SystemExit(1 if _failed else 0)
