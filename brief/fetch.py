"""
Slice 2 — fetch articles from all configured RSS feeds.

For each feed:
  - Parse RSS with feedparser
  - Take the latest N entries (N = feed.max_articles)
  - Prefer RSS content:encoded for body text; fall back to trafilatura on the URL
  - Deduplicate against state/seen.json (URLs from the last 7 days)

Network calls get one retry. Telugu feeds that return 403 retry once with a
browser User-Agent; if that also fails, we log and skip the feed rather than
crashing the run.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import httpx
import trafilatura

log = logging.getLogger(__name__)

# A realistic desktop User-Agent — some feeds (e.g. Andhra Jyothy) return 403
# to the default python-httpx/feedparser UA.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 20.0
SEEN_WINDOW_DAYS = 7


@dataclass
class Article:
    category: str
    source: str
    lang: str
    title: str
    url: str
    published: str  # ISO string, best-effort
    text: str


# ----------------------------- seen.json -----------------------------

def _load_seen(state_dir: Path, filename: str = "seen.json") -> dict[str, str]:
    """Return {url: iso_timestamp} of URLs seen in the last SEEN_WINDOW_DAYS."""
    path = state_dir / filename
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        log.warning("seen.json was corrupt — starting fresh")
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_WINDOW_DAYS)
    pruned = {
        url: ts
        for url, ts in raw.items()
        if _parse_iso(ts) and _parse_iso(ts) >= cutoff
    }
    if len(pruned) != len(raw):
        log.info("Pruned %d entries older than %d days from seen.json",
                 len(raw) - len(pruned), SEEN_WINDOW_DAYS)
    return pruned


def _save_seen(state_dir: Path, seen: dict[str, str], filename: str = "seen.json") -> None:
    state_dir.mkdir(exist_ok=True)
    path = state_dir / filename
    path.write_text(json.dumps(seen, indent=2, sort_keys=True))


def _parse_iso(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


# ----------------------------- HTTP helpers --------------------------

def _http_get(url: str, *, use_browser_ua: bool = False) -> httpx.Response | None:
    """GET with one retry. Returns None on failure."""
    headers = {"User-Agent": BROWSER_UA} if use_browser_ua else {}
    for attempt in (1, 2):
        try:
            r = httpx.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True, headers=headers)
            r.raise_for_status()
            return r
        except httpx.HTTPError as e:
            if attempt == 1:
                log.warning("GET %s failed (attempt 1): %s — retrying", url, e)
                time.sleep(1)
            else:
                log.error("GET %s failed (attempt 2): %s", url, e)
    return None


def _fetch_feed_bytes(url: str) -> bytes | None:
    """
    Fetch raw feed bytes with one retry. Always uses a realistic browser UA
    because some feeds (CBC, Andhra Jyothy) block the default python-httpx UA
    at the TCP layer — CBC disconnects mid-response, others return 403.
    """
    headers = {"User-Agent": BROWSER_UA}
    for attempt in (1, 2):
        try:
            r = httpx.get(url, timeout=HTTP_TIMEOUT,
                          follow_redirects=True, headers=headers)
            r.raise_for_status()
            return r.content
        except httpx.HTTPError as e:
            if attempt == 1:
                log.warning("Feed fetch %s failed (attempt 1): %s — retrying",
                            url, e)
                time.sleep(1)
            else:
                log.error("Feed fetch failed for %s: %s", url, e)
    return None


# ----------------------------- article text --------------------------

def _extract_article_text(entry, url: str) -> str:
    """Prefer RSS content:encoded; else fetch URL and run trafilatura."""
    # feedparser exposes content:encoded as entry.content[*].value
    if getattr(entry, "content", None):
        for c in entry.content:
            val = c.get("value", "").strip()
            if val and len(val) > 200:
                # Strip HTML tags cheaply with trafilatura
                cleaned = trafilatura.extract(val) or val
                if cleaned:
                    return cleaned

    # Fallback: summary/description
    summary = (entry.get("summary") or "").strip()

    # Fetch article URL for the real thing
    r = _http_get(url, use_browser_ua=True)
    if r is not None:
        extracted = trafilatura.extract(r.text) or ""
        if extracted and len(extracted) > len(summary):
            return extracted

    return summary


# ----------------------------- main entry ----------------------------

def run(config: dict) -> list[Article]:
    """
    Fetch all feeds, extract text, dedupe against seen.json.
    Returns list of new Article objects (possibly empty).
    Updates seen.json with all URLs encountered this run.
    """
    state_dir = Path(config["state_dir"])
    seen_filename = config.get("output", {}).get("seen_filename", "seen.json")
    seen = _load_seen(state_dir, seen_filename)
    now_iso = datetime.now(timezone.utc).isoformat()

    new_articles: list[Article] = []
    total_seen = 0
    total_skipped = 0

    for category, feeds in config["feeds"].items():
        for feed_cfg in feeds:
            name = feed_cfg["name"]
            url = feed_cfg["url"]
            lang = feed_cfg["lang"]
            max_n = feed_cfg["max_articles"]

            log.info("Fetching feed: %s (%s)", name, url)
            raw = _fetch_feed_bytes(url)
            if raw is None:
                log.warning("Skipping feed %s — fetch failed", name)
                continue

            parsed = feedparser.parse(raw)
            if parsed.bozo and not parsed.entries:
                log.warning("Feed %s had parse errors and no entries: %s",
                            name, parsed.bozo_exception)
                continue

            entries = parsed.entries[:max_n]
            log.info("  %d entries (taking %d)", len(parsed.entries), len(entries))

            for entry in entries:
                total_seen += 1
                link = entry.get("link", "").strip()
                if not link:
                    continue

                if link in seen:
                    total_skipped += 1
                    continue

                title = entry.get("title", "(no title)").strip()
                published = entry.get("published", "") or entry.get("updated", "")
                text = _extract_article_text(entry, link)

                if not text or len(text) < 50:
                    log.warning("  Skipping %s — no usable text", link)
                    continue

                new_articles.append(Article(
                    category=category,
                    source=name,
                    lang=lang,
                    title=title,
                    url=link,
                    published=published,
                    text=text,
                ))
                seen[link] = now_iso
                log.info("  + [%s] %s", lang, title[:80])

    _save_seen(state_dir, seen, seen_filename)

    log.info("Fetch summary: %d entries scanned, %d already seen, %d new articles",
             total_seen, total_skipped, len(new_articles))
    return new_articles


# Allow standalone run for quick testing: `python -m brief.fetch`
if __name__ == "__main__":
    import sys
    import yaml
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s UTC [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.Formatter.converter = time.gmtime

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    articles = run(cfg)

    print("\n==== RESULTS ====")
    by_cat: dict[str, int] = {}
    for a in articles:
        by_cat[a.category] = by_cat.get(a.category, 0) + 1
    for cat, n in by_cat.items():
        print(f"  {cat}: {n}")
    print(f"  TOTAL: {len(articles)}")

    if articles:
        sample = articles[0]
        print("\n---- Sample article ----")
        print(f"  [{sample.category}] {sample.source} ({sample.lang})")
        print(f"  Title: {sample.title}")
        print(f"  URL:   {sample.url}")
        print(f"  Chars: {len(sample.text)}")
        print(f"  Preview: {sample.text[:300]}...")

    sys.exit(0 if articles else 0)  # empty result is not a failure
