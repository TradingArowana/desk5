"""
News Sentinel for Desk5.
Monitors geopolitical and macro news for commodity/crypto correlation.
"""
import json
import re
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any
import requests

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
STATE_DIR.mkdir(parents=True, exist_ok=True)
NEWS_DB = STATE_DIR / "news_sentinel.json"

# ── Keyword mapping: event → asset signal ────────────────────────────────

EVENT_MAP = {
    "oil": {"assets": ["PAXG"], "bias": "LONG", "urgency": "high", "ttl_hours": 72},
    "crude": {"assets": ["PAXG"], "bias": "LONG", "urgency": "high", "ttl_hours": 72},
    "brent": {"assets": ["PAXG"], "bias": "LONG", "urgency": "high", "ttl_hours": 72},
    "iran": {"assets": ["PAXG", "BTC"], "bias": "LONG", "urgency": "high", "ttl_hours": 48},
    "israel": {"assets": ["PAXG", "BTC"], "bias": "LONG", "urgency": "medium", "ttl_hours": 48},
    "missile": {"assets": ["PAXG", "BTC"], "bias": "LONG", "urgency": "high", "ttl_hours": 24},
    "sanctions": {"assets": ["PAXG", "BTC"], "bias": "LONG", "urgency": "medium", "ttl_hours": 72},
    "opec": {"assets": ["PAXG"], "bias": "LONG", "urgency": "medium", "ttl_hours": 96},
    "supply": {"assets": ["PAXG"], "bias": "LONG", "urgency": "medium", "ttl_hours": 48},
    "war": {"assets": ["PAXG", "BTC"], "bias": "LONG", "urgency": "high", "ttl_hours": 24},
    "de-escalation": {"assets": ["PAXG"], "bias": "SHORT", "urgency": "medium", "ttl_hours": 48},
    "ceasefire": {"assets": ["PAXG"], "bias": "SHORT", "urgency": "medium", "ttl_hours": 48},
    "tariff": {"assets": ["SPX"], "bias": "SHORT", "urgency": "medium", "ttl_hours": 72},
    "trade war": {"assets": ["SPX", "BTC"], "bias": "SHORT", "urgency": "medium", "ttl_hours": 72},
    "fed": {"assets": ["BTC", "SPX"], "bias": "LONG", "urgency": "medium", "ttl_hours": 48},
    "rate cut": {"assets": ["BTC", "SPX"], "bias": "LONG", "urgency": "medium", "ttl_hours": 48},
    "inflation": {"assets": ["BTC", "PAXG"], "bias": "LONG", "urgency": "high", "ttl_hours": 72},
}

BULLISH_TERMS = ["surge", "spike", "rocket", "rally", "soar", "boom", "breakthrough", "unprecedented", "sharp rise", "surge", "escalate"]
BEARISH_TERMS = ["plunge", "crash", "collapse", "tumble", "drop", "fall", "decline", "de-escalate", "ceasefire", "peace", "stabilize", "recover"]

# ── Feed Parsers ─────────────────────────────────────────────────────────

def fetch_rss(url: str) -> List[Dict]:
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Desk5-NewsBot/1.0"})
        r.raise_for_status()
        text = r.text
        items = []
        # Simple regex RSS parser (no XML lib needed)
        for block in re.findall(r"&lt;item&gt;(.*?)&lt;/item&gt;", text, re.DOTALL | re.IGNORECASE):
            title = re.search(r"&lt;title&gt;(.*?)&lt;/title&gt;", block, re.DOTALL | re.IGNORECASE)
            link = re.search(r"&lt;link&gt;(.*?)&lt;/link&gt;", block, re.DOTALL | re.IGNORECASE)
            pub = re.search(r"&lt;pubDate&gt;(.*?)&lt;/pubDate&gt;", block, re.DOTALL | re.IGNORECASE)
            desc = re.search(r"&lt;description&gt;(.*?)&lt;/description&gt;", block, re.DOTALL | re.IGNORECASE)
            items.append({
                "title": (title.group(1) if title else "").strip(),
                "url": (link.group(1) if link else "").strip(),
                "published": (pub.group(1) if pub else "").strip(),
                "summary": (desc.group(1) if desc else "").strip()[:300],
            })
        return items
    except Exception as exc:
        logger.warning("RSS fetch failed for %s: %s", url, exc)
        return []


def score_sentiment(title: str) -> float:
    text = title.lower()
    bull = sum(1 for t in BULLISH_TERMS if t in text)
    bear = sum(1 for t in BEARISH_TERMS if t in text)
    return round((bull - bear) / max(1, bull + bear), 2)


def extract_signals(title: str, source: str) -> List[Dict]:
    text = title.lower()
    signals = []
    for keyword, config in EVENT_MAP.items():
        if keyword in text:
            urgency_score = {"low": 1, "medium": 2, "high": 3}[config["urgency"]]
            sentiment = score_sentiment(title)
            # Adjust bias based on sentiment
            final_bias = config["bias"]
            if sentiment < -0.2 and config["bias"] == "LONG":
                final_bias = "SHORT"  # If article says prices "plunge" despite war
            elif sentiment > 0.2 and config["bias"] == "SHORT":
                final_bias = "LONG"
            signals.append({
                "keyword": keyword,
                "assets": config["assets"],
                "bias": final_bias,
                "urgency": config["urgency"],
                "ttl_hours": config["ttl_hours"],
                "sentiment_score": sentiment,
                "confidence": min(0.95, 0.4 + urgency_score * 0.2 + abs(sentiment) * 0.15),
            })
    return signals


def poll_feeds() -> List[Dict]:
    feeds = [
        "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
    ]
    all_items = []
    # Use oilprice.com as primary (most reliable commodity source)
    for url in feeds[3:]:  # Start with oilprice
        items = fetch_rss(url)
        for item in items[:10]:
            signals = extract_signals(item["title"], url)
            if signals:
                item["signals"] = signals
                item["source"] = url.split("/")[2]
                item["fetched_at"] = datetime.now(timezone.utc).isoformat()
                all_items.append(item)
    return all_items


def run_cycle() -> dict:
    items = poll_feeds()
    # Load existing
    if NEWS_DB.exists():
        try:
            existing = json.loads(NEWS_DB.read_text())
        except Exception:
            existing = []
    else:
        existing = []
    # Append and dedupe by title
    seen = {e["title"] for e in existing}
    new_items = [i for i in items if i["title"] not in seen]
    combined = (new_items + existing)[:200]  # Keep last 200
    NEWS_DB.write_text(json.dumps(combined, indent=2))

    # Generate actionable signals
    active_signals = []
    for item in new_items:
        for sig in item.get("signals", []):
            for asset in sig["assets"]:
                active_signals.append({
                    "asset": asset,
                    "bias": sig["bias"],
                    "urgency": sig["urgency"],
                    "keyword": sig["keyword"],
                    "sentiment": sig["sentiment_score"],
                    "confidence": sig["confidence"],
                    "headline": item["title"][:100],
                    "ttl_hours": sig["ttl_hours"],
                    "dt": item["fetched_at"],
                })

    return {
        "new_items": len(new_items),
        "total_stored": len(combined),
        "active_signals": active_signals,
        "top_keywords": _keyword_freq(combined),
        "dt": datetime.now(timezone.utc).isoformat(),
    }


def _keyword_freq(items: List[Dict]) -> Dict[str, int]:
    counts = {}
    for item in items:
        for sig in item.get("signals", []):
            kw = sig["keyword"]
            counts[kw] = counts.get(kw, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10])


def get_active_signals() -> List[Dict]:
    if not NEWS_DB.exists():
        return []
    try:
        items = json.loads(NEWS_DB.read_text())
    except Exception:
        return []
    now = datetime.now(timezone.utc)
    active = []
    for item in items[-50:]:
        for sig in item.get("signals", []):
            try:
                item_dt = datetime.fromisoformat(item["fetched_at"].replace("Z", "+00:00"))
                age_hours = (now - item_dt).total_seconds() / 3600
                if age_hours < sig["ttl_hours"]:
                    for asset in sig["assets"]:
                        active.append({
                            "asset": asset,
                            "bias": sig["bias"],
                            "urgency": sig["urgency"],
                            "keyword": sig["keyword"],
                            "confidence": sig["confidence"],
                            "headline": item["title"][:100],
                            "age_hours": round(age_hours, 1),
                            "remaining_hours": round(sig["ttl_hours"] - age_hours, 1),
                        })
            except Exception:
                continue
    return active


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_cycle()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["new_items"] > 0 else 0)
