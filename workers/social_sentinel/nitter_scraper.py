"""
Nitter-based Twitter scraper fallback.
Requires NO API key. Scrapes public Nitter instances with instance rotation
and exponential backoff.
"""

import re
import time
import logging
import sqlite3
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.pussthecat.org",
    "https://nitter.it",
    "https://nitter.cz",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


class NitterScraper:
    def __init__(
        self,
        instances: Optional[List[str]] = None,
        timeout: int = 15,
        retries: int = 3,
        max_results_per_run: int = 50,
    ):
        self.instances = instances or DEFAULT_NITTER_INSTANCES[:]
        self.timeout = timeout
        self.retries = retries
        self.max_results_per_run = max_results_per_run
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        # Rotator state
        self._instance_index = 0
        self._backoff_until: Dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # Instance rotation & raw fetch
    # ------------------------------------------------------------------ #

    def _pick_instance(self) -> Optional[str]:
        now = time.time()
        candidates = [
            inst
            for inst in self.instances
            if now >= self._backoff_until.get(inst, 0)
        ]
        if not candidates:
            return None
        # simple round-robin among healthy candidates
        pick = candidates[self._instance_index % len(candidates)]
        self._instance_index += 1
        return pick

    def _mark_unhealthy(self, instance: str, level: int = 0):
        """Exponential backoff: 60s, 120s, 240s … capped at 1h."""
        backoff = min(60 * (2 ** level), 3600)
        self._backoff_until[instance] = time.time() + backoff
        logger.warning("Nitter instance %s marked unhealthy for %ss", instance, backoff)

    def _fetch(self, path: str) -> Optional[BeautifulSoup]:
        """Fetch a Nitter path (e.g. /elonmusk or /search?f=tweets&q=btc)
        and return parsed BeautifulSoup on success."""
        for attempt in range(self.retries):
            inst = self._pick_instance()
            if inst is None:
                logger.error("All Nitter instances are currently backlogged.")
                time.sleep(2 ** attempt)
                continue

            url = urljoin(inst, path)
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "lxml")
                    # Anti-bot / rate-limit signals
                    if not resp.text or len(resp.text) < 800:
                        self._mark_unhealthy(inst, attempt)
                        continue
                    if soup.find("div", class_="error-panel") or "rate limit" in resp.text.lower():
                        self._mark_unhealthy(inst, attempt)
                        continue
                    # Cloudflare challenge or redirect-homepage guard
                    title = (soup.title.string or "").lower() if soup.title else ""
                    if "just a moment" in title or "nitter - homepage" in title or "nitter.it - homepage" in title:
                        self._mark_unhealthy(inst, attempt)
                        continue
                    # Ensure timeline exists; if not, still return soup and let parser return zero items
                    return soup
                elif resp.status_code in (429, 503, 502, 403):
                    self._mark_unhealthy(inst, attempt)
                    continue
                else:
                    logger.warning("Nitter %s returned HTTP %s", inst, resp.status_code)
                    self._mark_unhealthy(inst, attempt)
            except requests.exceptions.Timeout:
                self._mark_unhealthy(inst, attempt)
            except requests.exceptions.RequestException as exc:
                logger.warning("Nitter %s request error: %s", inst, exc)
                self._mark_unhealthy(inst, attempt)

            if attempt < self.retries - 1:
                time.sleep(1.5 ** attempt)

        return None

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _safe_int(text: str) -> int:
        text = (text or "").replace(",", "").replace("K", "000").replace("M", "000000").strip()
        m = re.search(r"(\d+)", text)
        return int(m.group(1)) if m else 0

    @staticmethod
    def _extract_hashtags(text: str) -> List[str]:
        return re.findall(r"#(\w+)", text)

    def _parse_timeline_item(self, item: BeautifulSoup) -> Optional[Dict[str, Any]]:
        """Parse a single .timeline-item into a tweet dict."""
        if not item or item.get_text(strip=True) == "":
            return None

        # Skip pinned separator or showmore items
        if item.find("div", class_="showmore"):
            return None

        tweet_link = item.find("a", class_="tweet-link")
        if not tweet_link:
            tweet_link = item.select_one("a[href*='/status/']")
        tweet_url = None
        post_id = None
        if tweet_link and tweet_link.get("href"):
            raw = tweet_link["href"]
            if raw.startswith("/"):
                raw = raw[1:]
            tweet_url = f"https://twitter.com/{raw}"
            # Extract status id
            m = re.search(r"/status/(\d+)", raw)
            if m:
                post_id = m.group(1)

        body = item.find("div", class_="tweet-content") or item
        text_el = body.find("div", class_="tweet-content") or body.find("div", class_="tweet-body")
        content = ""
        if text_el:
            content = text_el.get_text(separator=" ", strip=True)
        else:
            # Try extracting text from tweet-body descendants
            tb = item.find("div", class_="tweet-body")
            if tb:
                content = tb.get_text(separator=" ", strip=True)
            else:
                # Fallback: grab all text inside the item except stats and meta
                content_parts = []
                for child in item.descendants:
                    if isinstance(child, str):
                        content_parts.append(child)
                content = " ".join(content_parts).strip()
                # Heuristic cleanup
                for h in item.find_all("div", class_=re.compile("tweet-stats|icon-[")):
                    content = content.replace(h.get_text(strip=True), "")
                content = re.sub(r"\s+", " ", content).strip()

        # author
        author = ""
        username_el = item.find("a", class_="username")
        if username_el:
            author = username_el.get_text(strip=True).lstrip("@")
        else:
            # Try extracting from URL
            if tweet_url:
                m = re.search(r"twitter\.com/(\w+)/status", tweet_url)
                if m:
                    author = m.group(1)

        # timestamp
        created_at = datetime.now(timezone.utc).isoformat()
        date_el = item.find("span", class_="tweet-date")
        if date_el:
            time_tag = date_el.find("a")
            if time_tag and time_tag.get("title"):
                try:
                    # Nitter often uses ISO-like title or epoch
                    title = time_tag["title"]
                    dt = datetime.strptime(title, "%b %d, %Y · %I:%M %p %Z")
                    created_at = dt.replace(tzinfo=timezone.utc).isoformat()
                except Exception:
                    try:
                        created_at = datetime.fromisoformat(title.replace("Z", "+00:00")).isoformat()
                    except Exception:
                        pass

        # stats
        likes, retweets, replies = 0, 0, 0
        stats = item.find("div", class_="tweet-stats")
        if stats:
            for icon, key in (("icon-heart", "likes"), ("icon-retweet", "retweets"), ("icon-message", "replies")):
                tag = stats.find("div", class_=icon)
                if tag:
                    val = self._safe_int(tag.get_text(strip=True))
                    if key == "likes":
                        likes = val
                    elif key == "retweets":
                        retweets = val
                    else:
                        replies = val
        else:
            # Alternate structure
            for stat in item.select(".tweet-stat"):
                icon = stat.find("div", class_=re.compile("icon-"))
                val = self._safe_int(stat.get_text(strip=True))
                if icon:
                    cls = " ".join(icon.get("class", []))
                    if "heart" in cls:
                        likes = val
                    elif "retweet" in cls:
                        retweets = val
                    elif "message" in cls or "reply" in cls:
                        replies = val

        # media / links
        media = []
        for img in item.find_all("img"):
            src = img.get("src")
            if src and "/pic/" in src:
                media.append(urljoin("https://nitter.net", src))
        for vid in item.find_all("source"):
            src = vid.get("src")
            if src:
                media.append(urljoin("https://nitter.net", src))

        links = []
        for a in item.find_all("a"):
            href = a.get("href", "")
            if href.startswith("http") and "twitter.com" not in href and "nitter" not in href:
                links.append(href)

        hashtags = self._extract_hashtags(content)

        return {
            "post_id": post_id or "",
            "text": content,
            "author": author,
            "followers_count": 0,   # Nitter search/timeline pages don't expose this easily
            "following_count": 0,
            "created_at": created_at,
            "retweet_count": retweets,
            "reply_count": replies,
            "like_count": likes,
            "hashtags": hashtags,
            "url": tweet_url or "",
            "media": media,
            "links": links,
            "source": "twitter",
            "platform": "twitter",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def _parse_page(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """Extract all timeline items from a Nitter HTML page."""
        tweets: List[Dict[str, Any]] = []
        timeline = soup.find("div", class_="timeline")
        if not timeline:
            timeline = soup
        items = timeline.find_all("div", class_="timeline-item")
        for item in items:
            parsed = self._parse_timeline_item(item)
            if parsed:
                tweets.append(parsed)
        return tweets

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def search_tweets(self, keyword: str) -> List[Dict[str, Any]]:
        """Search Nitter for a keyword."""
        path = f"/search?f=tweets&q={requests.utils.quote(keyword)}"
        soup = self._fetch(path)
        if not soup:
            return []
        tweets = self._parse_page(soup)
        # Respect max_results_per_run per query
        return tweets[: self.max_results_per_run]

    def get_user_tweets(self, username: str) -> List[Dict[str, Any]]:
        """Fetch latest tweets from a watch-listed user."""
        username = username.lstrip("@")
        path = f"/{username}"
        soup = self._fetch(path)
        if not soup:
            return []
        tweets = self._parse_page(soup)
        return tweets[: self.max_results_per_run]

    def health(self) -> Dict[str, Any]:
        """Return the current health of all configured instances."""
        now = time.time()
        status = []
        healthy_count = 0
        for inst in self.instances:
            backoff = self._backoff_until.get(inst, 0)
            healthy = now >= backoff
            if healthy:
                healthy_count += 1
            status.append(
                {
                    "instance": inst,
                    "healthy": healthy,
                    "backoff_seconds_left": max(0, int(backoff - now)),
                }
            )
        return {
            "overall_healthy": healthy_count > 0,
            "healthy_count": healthy_count,
            "total_count": len(self.instances),
            "instances": status,
        }
