
"""
Alpha scanner: long-tail small-caps beyond top 30.
Scans CoinGecko top 250 for momentum + volume outliers.
"""
import json
import time
import random
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict

import requests

CG_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
STATE_PATH = Path(__file__).parent.parent.parent / "data_store" / "scanner_state.json"


def fetch_markets(page: int = 1, per_page: int = 250) -> List[Dict]:
    try:
        r = requests.get(
            CG_MARKETS,
            params={
                "vs_currency": "usd",
                "order": "volume_desc",
                "per_page": per_page,
                "page": page,
                "sparkline": "false",
                "price_change_percentage": "24h",
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return []


def filter_longtail(data: List[Dict]) -> List[Dict]:
    out = []
    for c in data:
        rank = c.get("market_cap_rank") or 9999
        mcap = c.get("market_cap") or 0
        vol = c.get("total_volume") or 0
        change = c.get("price_change_percentage_24h") or 0
        # Filters: outside top 30, > $1M mcap, > $500k vol, > 5% move
        if rank > 30 and mcap > 1_000_000 and vol > 500_000 and abs(change) >= 5.0:
            out.append({
                "symbol": c["symbol"].upper(),
                "name": c["name"],
                "price": c.get("current_price"),
                "change_24h": round(change, 2),
                "volume_24h": vol,
                "market_cap": mcap,
                "rank": rank,
            })
    out.sort(key=lambda x: abs(x["change_24h"]), reverse=True)
    return out[:20]


def run_scan() -> Dict:
    data = fetch_markets(page=1, per_page=250)
    if not data:
        return {"error": "CoinGecko fetch failed", "watchlist": []}
    longtail = filter_longtail(data)
    # Also include top mover for momentum
    top = sorted(data, key=lambda x: abs(x.get("price_change_percentage_24h") or 0), reverse=True)[:5]
    state = {
        "watchlist": longtail,
        "top_movers": [
            {"symbol": c["symbol"].upper(), "change_24h": round(c.get("price_change_percentage_24h") or 0, 2),
             "price": c.get("current_price"), "volume_24h": c.get("total_volume")}
            for c in top
        ],
        "last_scan": datetime.now(timezone.utc).isoformat(),
        "count": len(longtail),
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))
    return state


if __name__ == "__main__":
    print(json.dumps(run_scan(), indent=2))
