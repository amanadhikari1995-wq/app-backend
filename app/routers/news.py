"""
News router — fetches financial RSS feeds server-side.
No API key required. Results cached in-memory for 4 minutes.
"""
from fastapi import APIRouter
import urllib.request
import xml.etree.ElementTree as ET
import time
import html
import re
from typing import List, Dict, Any

router = APIRouter(prefix="/api/news", tags=["news"])

# ── Feed definitions ──────────────────────────────────────────────────────────
# All free public RSS feeds — no API key, no registration needed.
FEEDS = [
    {
        "name": "Reuters",
        "url":  "https://feeds.reuters.com/reuters/businessNews",
        "color": "#FF8C00",
    },
    {
        "name": "Yahoo Finance",
        "url":  "https://finance.yahoo.com/news/rssindex",
        "color": "#6001D2",
    },
    {
        "name": "MarketWatch",
        "url":  "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "color": "#00B140",
    },
    {
        "name": "Seeking Alpha",
        "url":  "https://seekingalpha.com/feed.xml",
        "color": "#1DB954",
    },
    {
        "name": "Investing.com",
        "url":  "https://www.investing.com/rss/news.rss",
        "color": "#E44D26",
    },
    {
        "name": "Google Finance",
        "url":  (
            "https://news.google.com/rss/search"
            "?q=stock+market+finance&hl=en-US&gl=US&ceid=US:en"
        ),
        "color": "#4285F4",
    },
]

# Number of items to pull per feed
ITEMS_PER_FEED = 6

# Cache: (timestamp, list_of_items)
_cache: Dict[str, Any] = {"ts": 0.0, "items": []}
CACHE_TTL = 240  # seconds (4 minutes)

# Media namespace used by most RSS image extensions
_MEDIA_NS = "http://search.yahoo.com/mrss/"

# ── Helpers ───────────────────────────────────────────────────────────────────
def _clean(text: str) -> str:
    """Strip HTML tags and decode HTML entities."""
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _parse_date(pub_date: str) -> float:
    """Parse RSS pubDate to a Unix timestamp (best-effort)."""
    import email.utils
    try:
        return email.utils.parsedate_to_datetime(pub_date).timestamp()
    except Exception:
        return 0.0


def _extract_image(el) -> str:
    """
    Try the most common RSS image extension patterns in order:
      1. <media:content url="..."> (Yahoo Media RSS)
      2. <media:thumbnail url="...">
      3. <enclosure url="..." type="image/...">
      4. <img src="..."> inside <description>
    Returns the first URL found, or "" if none.
    """
    # 1. media:content
    for tag in (f"{{{_MEDIA_NS}}}content", "media:content"):
        mc = el.find(tag)
        if mc is not None:
            url = mc.get("url", "")
            if url:
                return url

    # 2. media:thumbnail
    for tag in (f"{{{_MEDIA_NS}}}thumbnail", "media:thumbnail"):
        mt = el.find(tag)
        if mt is not None:
            url = mt.get("url", "")
            if url:
                return url

    # 3. enclosure with image MIME type
    enc = el.find("enclosure")
    if enc is not None:
        url = enc.get("url", "")
        typ = enc.get("type", "")
        if url and "image" in typ:
            return url

    # 4. <img src> embedded inside description HTML
    desc = el.findtext("description") or ""
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc, re.IGNORECASE)
    if m:
        return m.group(1)

    return ""


def _fetch_feed(feed: dict) -> List[dict]:
    """Fetch one RSS feed and return a list of item dicts."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    req = urllib.request.Request(feed["url"], headers=headers)
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read()

    root = ET.fromstring(raw)
    items = []

    # RSS
    for item in root.iter("item"):
        title = _clean(item.findtext("title") or "")
        link  = (item.findtext("link") or "").strip()
        pub   = item.findtext("pubDate") or ""
        desc  = _clean(item.findtext("description") or "")
        image = _extract_image(item)
        if title and link:
            items.append({
                "title":       title,
                "description": desc[:200] if desc else "",
                "link":        link,
                "pubDate":     pub,
                "ts":          _parse_date(pub),
                "source":      feed["name"],
                "color":       feed["color"],
                "image":       image,
            })

    # Atom (fallback)
    if not items:
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            title = _clean(entry.findtext("{http://www.w3.org/2005/Atom}title") or "")
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            link  = (link_el.get("href") if link_el is not None else "") or ""
            pub   = entry.findtext("{http://www.w3.org/2005/Atom}updated") or ""
            summary = _clean(entry.findtext("{http://www.w3.org/2005/Atom}summary") or "")
            image = _extract_image(entry)
            if title and link:
                items.append({
                    "title":       title,
                    "description": summary[:200] if summary else "",
                    "link":        link,
                    "pubDate":     pub,
                    "ts":          _parse_date(pub),
                    "source":      feed["name"],
                    "color":       feed["color"],
                    "image":       image,
                })

    return items[:ITEMS_PER_FEED]


def _fetch_all() -> List[dict]:
    """Try each feed in order; collect results; sort by timestamp desc."""
    all_items: List[dict] = []
    for feed in FEEDS:
        try:
            items = _fetch_feed(feed)
            all_items.extend(items)
        except Exception:
            # One broken feed should never kill the whole response
            pass

    # Sort newest first
    all_items.sort(key=lambda x: x["ts"], reverse=True)

    # Remove obvious duplicates (same title prefix)
    seen: set = set()
    unique: List[dict] = []
    for item in all_items:
        key = item["title"][:60].lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)

    return unique


# ── Endpoint ──────────────────────────────────────────────────────────────────
@router.get("/")
def get_news():
    """
    Return a list of recent financial news items.
    Results are cached for 4 minutes to avoid hammering RSS feeds.
    """
    now = time.time()
    if now - _cache["ts"] < CACHE_TTL and _cache["items"]:
        return {"items": _cache["items"], "cached": True, "age": int(now - _cache["ts"])}

    items = _fetch_all()

    # If everything failed (e.g. no internet on first boot), keep stale cache
    if items:
        _cache["ts"]    = now
        _cache["items"] = items

    return {
        "items":  _cache["items"],
        "cached": False,
        "age":    0,
    }
