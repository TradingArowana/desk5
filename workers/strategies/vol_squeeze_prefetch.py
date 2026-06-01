"""
vol_squeeze_prefetch.py
Pre-fetches CoinGecko OHLC for the top 20 mapped coins and stores them in
data_store/ohlc_cache.json so that vol_squeeze.py can serve scans from cache
with sub-second latency.
"""
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone
import requests

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
STATE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = STATE_DIR / "ohlc_cache.json"

# Top 20 coins by general priority / market-cap proxy
CG_OHLC_MAP = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "AVAX": "avalanche-2",
    "DOGE": "dogecoin",
    "LINK": "chainlink",
    "MATIC": "matic-network",
    "ARB": "arbitrum",
    "BNB": "binancecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOT": "polkadot",
    "OP": "optimism",
    "ATOM": "cosmos",
    "INJ": "injective-protocol",
    "SUI": "sui",
    "CRV": "curve-dao-token",
    "LDO": "lido-dao",
    "STX": "blockstack",
    "RNDR": "render-token",
}

_REQUEST_DELAY = 3.0  # seconds between CG requests (429 protection)


def _fetch_ohlc(cg_id: str) -> list:
    """Fetch 30-day daily OHLC for a single CoinGecko ID."""
    url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc"
    params = {"vs_currency": "usd", "days": 30}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            logger.warning("CG API error for %s: %s", cg_id, data)
            return []
        return [
            {"o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4])}
            for c in data
        ]
    except Exception as exc:
        logger.warning("CG fetch failed for %s: %s", cg_id, exc)
        return []


def refresh_cache() -> dict:
    """Fetch OHLC for all top-20 coins and atomically rewrite ohlc_cache.json."""
    data = {}
    for symbol, cg_id in CG_OHLC_MAP.items():
        ohlc = _fetch_ohlc(cg_id)
        data[cg_id] = {"symbol": symbol, "cg_id": cg_id, "ohlc": ohlc}
        time.sleep(_REQUEST_DELAY)

    cache = {
        "meta": {"updated_at": datetime.now(timezone.utc).isoformat()},
        "data": data,
    }
    CACHE_FILE.write_text(json.dumps(cache, indent=2))
    logger.info("OHLC cache refreshed for %d coins", len(data))
    return cache


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    cache = refresh_cache()
    ok = [k for k, v in cache["data"].items() if v.get("ohlc")]
    print(json.dumps({"cached_coins": ok, "total": len(ok)}, indent=2))
    sys.exit(0)
