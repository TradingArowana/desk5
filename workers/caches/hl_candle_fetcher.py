"""
Hyperliquid Candle Fetcher — native OHLC from HL API.
Replaces CoinGecko dependency for desk5 signal strategies.
"""
import requests, json, logging, time
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
HL_BASE = "https://api.hyperliquid.xyz"

# CoinGecko ID → Hyperliquid coin name mapping
CG_TO_HL = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "avalanche-2": "AVAX",
    "pax-gold": "PAXG", "dogecoin": "DOGE", "chainlink": "LINK",
    "matic-network": "MATIC", "arbitrum": "ARB", "gmx": "GMX", "binancecoin": "BNB",
    "ripple": "XRP", "cardano": "ADA", "polkadot": "DOT", "optimism": "OP",
    "cosmos": "ATOM", "apecoin": "APE", "injective-protocol": "INJ",
    "sui": "SUI", "curve-dao-token": "CRV", "lido-dao": "LDO", "blockstack": "STX",
    "render-token": "RNDR", "fantom": "FTM", "havven": "SNX", "bitcoin-cash": "BCH",
    "aptos": "APT", "aave": "AAVE", "compound-governance-token": "COMP",
    "maker": "MKR", "worldcoin-wld": "WLD", "tron": "TRX",
}

def fetch_hl_candles(coin: str, interval: str = "15m", lookback_hours: int = 12) -> List[Dict[str,Any]]:
    """Fetch recent candles from Hyperliquid. interval: 1m, 5m, 15m, 1h, 4h, 1d"""
    end_time = int(time.time() * 1000)
    start_time = end_time - (lookback_hours * 3600 * 1000)
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": start_time,
            "endTime": end_time,
        }
    }
    try:
        r = requests.post(f"{HL_BASE}/info", json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        candles = []
        for c in data:
            # HL format dict: {t, T, s, i, o, c, h, l, v, n}
            candles.append({
                "t": c["t"],
                "o": float(c["o"]),
                "h": float(c["h"]),
                "l": float(c["l"]),
                "c": float(c["c"]),
                "v": float(c["v"]),
            })
        return candles
    except Exception as exc:
        logger.warning("HL candle fetch failed for %s: %s", coin, exc)
        return []


def refresh_cache(lookback_hours: int = 12, interval: str = "15m") -> dict:
    """Refresh cg_ohlc_cache.json using Hyperliquid candles instead of CoinGecko."""
    cache = {"meta": {"updated_at": datetime.now(timezone.utc).isoformat()}, "data": {}}
    for cg_id, coin in CG_TO_HL.items():
        candles = fetch_hl_candles(coin, interval, lookback_hours)
        if candles:
            cache["data"][cg_id] = {"cg_id": cg_id, "ohlc": candles}
            logger.info("%s: %d candles", cg_id, len(candles))
        else:
            logger.warning("%s: no candles", cg_id)
    out_path = STATE_DIR / "cg_ohlc_cache.json"
    out_path.write_text(json.dumps(cache, indent=2))
    return cache


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = refresh_cache()
    print(json.dumps({"coins": len(result["data"]), "updated": result["meta"]["updated_at"]}, indent=2))
    sys.exit(0)
