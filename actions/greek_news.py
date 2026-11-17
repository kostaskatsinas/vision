"""
greek_news — headlines from Greece, read out as a short summary. No browser.

Where the headlines come from:
  * No topic: the RSS feeds of three Greek outlets (ERT, in.gr, Naftemporiki),
    merged, newest first, near-duplicates dropped.
  * A topic ("news about the economy", "τι γίνεται με την κυβέρνηση"):
    Google News for Greece searched for it, in Greek or English. If that
    fails, the outlet feeds are filtered by the topic's words, and the last
    resort is the web search the rest of Vision already uses.

The feed list can be replaced in config/api_keys.json under "news_feeds"
(a list of RSS URLs). RSS needs nothing beyond the standard library.

Headlines are written by strangers, so they go back to the model wrapped as
untrusted data (core.outcome.untrusted): a headline that says "ignore your
instructions" is news, not an instruction.
"""
from __future__ import annotations

import html
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime

FEEDS = (
    ("ERT News", "https://www.ertnews.gr/feed/"),
    ("in.gr", "https://www.in.gr/feed/"),
    ("Naftemporiki", "https://www.naftemporiki.gr/feed/"),
)
_TIMEOUT = 6
_MAX_BYTES = 3_000_000
_CACHE_SECONDS = 300        # asking twice in five minutes gets the same news

_cache: dict[str, tuple[float, list]] = {}
_cache_lock = threading.Lock()


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) vision-assistant",
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return resp.read(_MAX_BYTES)


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(str(text or "")))
    return " ".join(html.unescape(text).split())


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(data: bytes, source: str) -> list[dict]:
    """RSS 2.0 or Atom -> [{title, summary, source, ts}]. Bad XML -> []."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    out = []
    for item in root.iter():
        if _local(item.tag) not in ("item", "entry"):
            continue
        fields = {}
        for child in item:
            name = _local(child.tag)
            if name in ("title", "description", "summary", "pubDate", "published",
                        "updated", "source") and name not in fields:
                fields[name] = child.text or ""
        title = _clean(fields.get("title"))
        if not title:
            continue
        src = _clean(fields.get("source")) or source
        # Google News puts the outlet at the end of the title: "... - Καθημερινή".
        if source == "Google News" and " - " in title:
            title, _, tail = title.rpartition(" - ")
            src = src if src != source else tail.strip()
        summary = _clean(fields.get("description") or fields.get("summary"))
        if summary.startswith(title[:40]):
            summary = ""            # Google News repeats the title as a link
        ts = 0.0
        stamp = fields.get("pubDate") or fields.get("published") or fields.get("updated")
        if stamp:
            try:
                ts = parsedate_to_datetime(stamp.strip()).timestamp()
            except (TypeError, ValueError, IndexError):
                try:
                    from datetime import datetime
                    ts = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00")).timestamp()
                except ValueError:
                    ts = 0.0
        out.append({"title": title, "summary": summary[:240], "source": src, "ts": ts})
    return out


def _feeds() -> tuple:
    try:
        from memory.config_manager import load_api_keys
        custom = load_api_keys().get("news_feeds")
        if isinstance(custom, list) and custom:
            return tuple((urllib.parse.urlparse(u).hostname or "feed", u)
                         for u in custom if isinstance(u, str) and u.startswith("http"))
    except Exception:
        pass
    return FEEDS


def _key(title: str) -> str:
    return re.sub(r"\W+", "", title.lower())[:50]


def merge(lists: list[list[dict]], limit: int) -> list[dict]:
    """Newest first, one copy of each story."""
    seen, out = set(), []
    for row in sorted((r for rows in lists for r in rows), key=lambda r: -r["ts"]):
        k = _key(row["title"])
        if k and k not in seen:
            seen.add(k)
            out.append(row)
    return out[:limit]


def _cached(key: str, fn):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_SECONDS:
            return hit[1]
    rows = fn()
    if rows:
        with _cache_lock:
            _cache[key] = (time.monotonic(), rows)
    return rows


def outlet_headlines() -> list[dict]:
    def one(feed):
        name, url = feed
        try:
            return parse_feed(_fetch(url), name)
        except Exception as e:
            print(f"[GreekNews] {name} failed: {type(e).__name__}")
            return []
    feeds = _feeds()
    with ThreadPoolExecutor(max_workers=len(feeds) or 1) as pool:
        lists = list(pool.map(one, feeds))
    return merge(lists, 40)


def google_news(topic: str, lang: str) -> list[dict]:
    lang = "en" if lang == "en" else "el"
    url = ("https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{topic} when:2d", "hl": lang, "gl": "GR", "ceid": f"GR:{lang}"}))
    return merge([parse_feed(_fetch(url), "Google News")], 20)


def _words(text: str) -> list[str]:
    import unicodedata
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return [w for w in re.findall(r"\w+", text) if len(w) > 3]


def filter_topic(rows: list[dict], topic: str) -> list[dict]:
    """Rows that mention a word of the topic. Greek words change their ending
    with grammar, so each word is matched on its first five letters."""
    stems = [w[:5] for w in _words(topic)]
    if not stems:
        return rows
    return [r for r in rows
            if any(s in " ".join(_words(r["title"] + " " + r["summary"])) for s in stems)]


def gather(topic: str, lang: str, count: int) -> tuple[list[dict], str]:
    """(headlines, where they came from)."""
    if topic:
        try:
            rows = _cached(f"g:{lang}:{topic.lower()}", lambda: google_news(topic, lang))
            if rows:
                return rows[:count], "Google News Greece"
        except Exception as e:
            print(f"[GreekNews] Google News failed: {type(e).__name__}")
        rows = filter_topic(_cached("outlets", outlet_headlines), topic)
        if rows:
            return rows[:count], "Greek outlets"
        try:
            from actions.web_search import _ddg_news
            found = _ddg_news(f"Greece {topic}", max_results=count) or []
            rows = [{"title": _clean(r.get("title")), "summary": _clean(r.get("snippet"))[:240],
                     "source": r.get("source", ""), "ts": 0.0} for r in found if r.get("title")]
            return rows, "web search"
        except Exception as e:
            print(f"[GreekNews] web search failed: {type(e).__name__}")
            return [], ""
    rows = _cached("outlets", outlet_headlines)
    if rows:
        return rows[:count], "Greek outlets"
    try:
        rows = _cached(f"g:{lang}:top", lambda: google_news("Ελλάδα" if lang == "el" else "Greece", lang))
        return rows[:count], "Google News Greece"
    except Exception as e:
        print(f"[GreekNews] Google News failed: {type(e).__name__}")
        return [], ""


def _age(ts: float) -> str:
    if not ts:
        return ""
    mins = int((time.time() - ts) // 60)
    if mins < 0:
        return ""
    if mins < 60:
        return f"{mins} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} days ago"


def format_rows(rows: list[dict]) -> str:
    lines = []
    for i, r in enumerate(rows, 1):
        meta = ", ".join(x for x in (r.get("source"), _age(r.get("ts") or 0)) if x)
        line = f"{i}. {r['title']}" + (f" ({meta})" if meta else "")
        if r.get("summary"):
            line += f"\n   {r['summary']}"
        lines.append(line)
    return "\n".join(lines)


def greek_news(parameters: dict, player=None, session_memory=None) -> str:
    parameters = parameters or {}
    topic = " ".join(str(parameters.get("topic") or "").split())[:80]
    lang = str(parameters.get("language") or "el").strip().lower()[:2]
    try:
        count = max(3, min(int(parameters.get("count") or 6), 10))
    except (TypeError, ValueError):
        count = 6

    rows, origin = gather(topic, lang, count)
    if not rows:
        msg = "The Greek news sources did not answer. Tell the user to try again in a minute."
        print(f"[GreekNews] {msg}")
        return msg

    text = format_rows(rows)
    if player:
        try:
            title = f"NEWS — {topic[:30]}" if topic else "NEWS — GREECE"
            player.show_content(title, text)
        except Exception:
            pass

    from core import outcome
    return (f"{len(rows)} headlines from {origin}"
            + (f" about '{topic}'" if topic else "") + ":\n"
            + outcome.untrusted("news", text)
            + "\nGive the user a short spoken summary in their language: the three or four "
              "most important stories, one sentence each, no links, no browser. Offer more "
              "detail on any of them if they want it.")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "greek_news",
    "description": (
        "Latest news from Greece, read out as a short spoken summary; it never opens a "
        "browser. Use when the user asks for the news, what is happening in Greece, or "
        "Greek news on a topic ('news about the economy', 'ειδήσεις για την κυβέρνηση'). "
        "Leave 'topic' empty for the top stories. For news about another country or "
        "the world, use web_search with mode 'news' instead."
    ),
    "brief": "Latest Greek news headlines, summarised by voice.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "topic": {
                "type": "STRING",
                "description": "Optional subject, e.g. 'economy', 'sports', 'Athens', 'κυβέρνηση'. Empty = top stories.",
            },
            "language": {
                "type": "STRING",
                "description": "Language of the sources for a topic search: 'el' (Greek, default) or 'en'.",
            },
            "count": {
                "type": "INTEGER",
                "description": "How many headlines to fetch, 3 to 10. Default 6.",
            },
        },
    },
    "handler": greek_news,
}
