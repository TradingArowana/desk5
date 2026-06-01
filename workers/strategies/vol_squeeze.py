"""
Vol Squeeze Strategy for Desk5.
Detects Bollinger Band squeezes via CoinGecko OHLC.

Optimised: reads OHLC from the local cache file managed by
workers/caches/cg_ohlc_fetcher.py, guaranteeing <2 s scan latency.
"""
import json
import math
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
STATE_DIR.mkdir(parents=True, exist_ok=True)
SIGNALS_FILE = STATE_DIR / "vol_squeeze_signals.json"
CACHE_FILE = STATE_DIR / "cg_ohlc_cache.json"

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

_CACHE_TTL_SECONDS = 3600
OHLC_CACHE_FILE = STATE_DIR / "ohlc_cache.json"


def _load_cache() -> Dict[str, Any]:
    """Load the local CG OHLC cache if present and not stale."""
    if not CACHE_FILE.exists():
        logger.warning("Cache file missing: %s", CACHE_FILE)
        return {}
    try:
        payload = json.loads(CACHE_FILE.read_text())
        updated_at_str = payload.get("meta", {}).get("updated_at")
        if not updated_at_str:
            return {}
        updated = datetime.fromisoformat(updated_at_str)
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        if age > _CACHE_TTL_SECONDS:
            logger.warning("Cache stale (age %ds > TTL %ds)", age, _CACHE_TTL_SECONDS)
            return {}
        logger.debug("Cache age %ds — valid", age)
        return payload.get("data", {})
    except Exception as exc:
        logger.warning("Cache read error: %s", exc)
        return {}


def _load_ohlc_cache() -> Dict[str, Any]:
    """Load top-20 OHLC prefetch cache if present and not stale."""
    if not OHLC_CACHE_FILE.exists():
        return {}
    try:
        payload = json.loads(OHLC_CACHE_FILE.read_text())
        updated_at_str = payload.get("meta", {}).get("updated_at")
        if not updated_at_str:
            return {}
        updated = datetime.fromisoformat(updated_at_str)
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        if age > _CACHE_TTL_SECONDS:
            logger.debug("OHLC cache stale (age %ds)", age)
            return {}
        logger.debug("OHLC cache age %ds — valid", age)
        return payload.get("data", {})
    except Exception as exc:
        logger.warning("OHLC cache read error: %s", exc)
        return {}


def _merged_ohlc(cg_id: str) -> List[Dict[str, Any]]:
    """Return OHLC for a cg_id, trying ohlc_cache first then cg_ohlc_cache."""
    ohlc = _load_ohlc_cache()
    entry = ohlc.get(cg_id, {})
    if entry.get("ohlc"):
        return entry["ohlc"]
    fallback = _load_cache()
    entry = fallback.get(cg_id, {})
    return entry.get("ohlc", [])


def get_candles(symbol: str) -> List[Dict[str, Any]]:
    """Return OHLC candles for a symbol from the local cache."""
    cg_id = CG_OHLC_MAP.get(symbol, "")
    if not cg_id:
        return []
    return _merged_ohlc(cg_id)


def bb_squeeze(candles: List[dict], mult: float = 2.0) -> dict:
    """Detect squeeze: bandwidth is narrow relative to recent history."""
    if len(candles) < 5:
        return {"valid": False}
    # Use last 20 candles if available
    if len(candles) > 20:
        candles = candles[-20:]
    closes = [c["c"] for c in candles]
    sma = sum(closes) / len(closes)
    variance = sum((c - sma) ** 2 for c in closes) / len(closes)
    std = math.sqrt(variance)
    upper = sma + mult * std
    lower = sma - mult * std
    bandwidth = ((upper - lower) / sma * 100) if sma > 0 else 0
    squeeze = bandwidth < 5.0
    bias = "LONG" if closes[-1] > sma else "SHORT"
    # ATR approx using simple high-low range
    atr = sum(c["h"] - c["l"] for c in candles) / len(candles)
    return {
        "valid": True,
        "sma": round(sma, 4),
        "upper": round(upper, 4),
        "lower": round(lower, 4),
        "bandwidth_pct": round(bandwidth, 4),
        "squeeze": squeeze,
        "bias": bias,
        "atr": round(atr, 4),
        "expected_move_pct": round(atr / sma * 100, 4) if sma > 0 else 0,
    }


def scan_squeezes(coins: List[str]) -> List[Dict[str, Any]]:
    signals = []
    ohlc_cache = _load_ohlc_cache()
    fallback = _load_cache()
    merged = {**fallback, **ohlc_cache}
    for coin in coins:
        cg_id = CG_OHLC_MAP.get(coin, "")
        if not cg_id:
            continue
        entry = merged.get(cg_id, {})
        candles = entry.get("ohlc", [])
        if not candles or len(candles) < 5:
            continue
        sig = bb_squeeze(candles)
        if sig.get("squeeze"):
            last_c = candles[-1]
            entry_px = round(last_c["c"], 4)
            sl_px = round(entry_px - sig["atr"] * 1.5, 4) if sig["bias"] == "LONG" else round(entry_px + sig["atr"] * 1.5, 4)
            tp_px = round(entry_px + sig["atr"] * 3.0, 4) if sig["bias"] == "LONG" else round(entry_px - sig["atr"] * 3.0, 4)
            signals.append({
                "coin": coin,
                "direction": sig["bias"],
                "entry_px": entry_px,
                "sl_px": sl_px,
                "tp_px": tp_px,
                "bandwidth": sig["bandwidth_pct"],
                "expected_move_pct": sig["expected_move_pct"],
                "dt": datetime.now(timezone.utc).isoformat(),
            })
    return signals


def run_cycle() -> dict:
    # Scan all mapped coins with CG IDs (filter to those with CG mapping)
    coins = [c for c in list(CG_OHLC_MAP.keys())[:50] if CG_OHLC_MAP.get(c)]
    signals = scan_squeezes(coins)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if SIGNALS_FILE.exists():
        try:
            existing = json.loads(SIGNALS_FILE.read_text())
        except Exception:
            existing = []
    else:
        existing = []
    existing = existing + signals
    existing = existing[-100:]
    SIGNALS_FILE.write_text(json.dumps(existing, indent=2))

    return {
        "signals": signals,
        "count": len(signals),
        "coins_scanned": len(coins),
        "dt": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import sys
    import time as _time

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    t0 = _time.time()
    result = run_cycle()
    elapsed = round(_time.time() - t0, 3)
    result["elapsed_sec"] = elapsed
    print(json.dumps(result, indent=2))
    sys.exit(0)
