"""
CoinGecko OHLC cache fetcher.
Pre-fetches daily OHLC for all mapped coins with rate limiting.
Vol Squeeze / Momentum Fade read from cache for sub-second scans.
"""
import json, time, logging
from pathlib import Path
from datetime import datetime, timezone
import requests

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
STATE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = STATE_DIR / "cg_ohlc_cache.json"
TTL_SEC = 3600  # 1 hour

CG_OHLC_MAP = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "AVAX": "avalanche-2",
    "PAXG": "pax-gold", "SPX": None, "DOGE": "dogecoin", "LINK": "chainlink",
    "MATIC": "matic-network", "ARB": "arbitrum", "GMX": "gmx", "BNB": "binancecoin",
    "XRP": "ripple", "ADA": "cardano", "DOT": "polkadot", "OP": "optimism",
    "ATOM": "cosmos", "APE": "apecoin", "INJ": "injective-protocol",
    "SUI": "sui", "CRV": "curve-dao-token", "LDO": "lido-dao", "STX": "blockstack",
    "RNDR": "render-token", "FTM": "fantom", "SNX": "havven", "BCH": "bitcoin-cash",
    "APT": "aptos", "AAVE": "aave", "COMP": "compound-governance-token",
    "MKR": "maker", "WLD": "worldcoin-wld", "TRX": "tron",
}


def _fetch_one(coin: str, cg_id: str) -> dict:
    url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc"
    params = {"vs_currency": "usd", "days": 30}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            logger.warning("CG error for %s: %s", coin, data)
            return {"cg_id": cg_id, "ohlc": [], "error": str(data)}
        # Format compatible with vol_squeeze.py: keys "o","h","l","c"
        ohlc = [{"o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4])} for c in data]
        return {"cg_id": cg_id, "ohlc": ohlc}
    except Exception as exc:
        logger.warning("CG fetch failed for %s: %s", coin, exc)
        return {"cg_id": cg_id, "ohlc": [], "error": str(exc)}


def refresh_cache() -> dict:
    pairs = [(k, v) for k, v in CG_OHLC_MAP.items() if v]
    data = {}
    for coin, cg_id in pairs:
        data[cg_id] = _fetch_one(coin, cg_id)
        time.sleep(1.2)  # rate limit
    cache = {
        "meta": {"updated_at": datetime.now(timezone.utc).isoformat()},
        "data": data,
    }
    CACHE_FILE.write_text(json.dumps(cache, indent=2))
    logger.info("Cache refreshed with %d coins", len(data))
    return cache


def load_cache(max_age_sec: int = TTL_SEC) -> dict:
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text())
            updated_at = cache.get("meta", {}).get("updated_at")
            if updated_at:
                updated = datetime.fromisoformat(updated_at)
                age = (datetime.now(timezone.utc) - updated).total_seconds()
                if age < max_age_sec:
                    logger.debug("Cache age %ds < TTL", age)
                    return cache
        except Exception:
            pass
    return refresh_cache()


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cache = refresh_cache()
    print(json.dumps({
        "cached_coins": [k for k, v in cache["data"].items() if v.get("ohlc")],
        "total": len(list(cache["data"].keys())),
    }, indent=2))
    sys.exit(0)
