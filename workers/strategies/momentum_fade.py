"""
Momentum Fade (Mean Reversion) Strategy for Desk5.
Detects RSI-based mean reversion signals on CoinGecko OHLC data.

- RSI divergence (price makes new local extreme but RSI doesn't)
- Volume divergence support
- Z-score / SMA deviation extremes

Reads from the local cg_ohlc_cache.json managed by workers/caches/cg_ohlc_fetcher.py.
"""
import json
import math
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any

try:
    import requests
except Exception:
    requests = None  # type: ignore

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
STATE_DIR.mkdir(parents=True, exist_ok=True)
SIGNALS_FILE = STATE_DIR / "momentum_fade_signals.json"
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


# ---------------------------------------------------------------------------
# OHLC helpers
# ---------------------------------------------------------------------------
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


def fetch_cg_ohlc(symbol: str, days: int = 30, vs_currency: str = "usd") -> List[Dict[str, Any]]:
    """Fetch OHLC from CoinGecko API (fallback when cache is empty/stale)."""
    cg_id = CG_OHLC_MAP.get(symbol, "")
    if not cg_id or requests is None:
        return []
    url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc"
    params = {"vs_currency": vs_currency, "days": days}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            logger.warning("CG API error for %s: %s", symbol, data)
            return []
        return [{"o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4])} for c in data]
    except Exception as exc:
        logger.warning("CG fetch failed for %s: %s", symbol, exc)
        return []


def get_candles(symbol: str) -> List[Dict[str, Any]]:
    """Return OHLC candles for a symbol from the local cache ONLY (no live fetch to avoid 429s)."""
    cg_id = CG_OHLC_MAP.get(symbol, "")
    if not cg_id:
        return []
    cache = _load_cache()
    entry = cache.get(cg_id, {})
    ohlc = entry.get("ohlc", [])
    if not ohlc:
        logger.info("Cache miss for %s — skipping (no live fetch)", symbol)
    return ohlc


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def rsi(closes: List[float], period: int = 14) -> List[float]:
    """Standard Wilder RSI. Returns a list aligned with *closes* (None for first period+1 entries)."""
    if len(closes) < period + 1:
        return [None] * len(closes)
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rsis: List[float] = [None] * (period + 1)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsis.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsis.append(100.0 - (100.0 / (1.0 + rs)))
    return rsis


def volume_divergence(candles: List[dict], lookback: int = 10) -> Dict[str, Any]:
    """
    Estimate volume divergence from OHLC range behavior.
    Returns a dict with divergent flag and supporting metrics.
    """
    if len(candles) < lookback + 1:
        return {"divergent": False, "reason": "insufficient_data"}

    # Proxy volume = candle range (high-low) as a % of close
    vols = []
    for c in candles:
        close = c["c"]
        rng = c["h"] - c["l"]
        vols.append((rng / close) * 100.0 if close > 0 else 0.0)

    closes = [c["c"] for c in candles]
    idx = len(candles) - 1
    prev_high_idx = max(range(idx - lookback, idx), key=lambda i: candles[i]["h"])
    prev_low_idx = min(range(idx - lookback, idx), key=lambda i: candles[i]["l"])

    # Bearish divergence: price higher high but volume (range) lower
    bearish = candles[idx]["h"] > candles[prev_high_idx]["h"] and vols[idx] < vols[prev_high_idx]
    # Bullish divergence: price lower low but volume (range) higher
    bullish = candles[idx]["l"] < candles[prev_low_idx]["l"] and vols[idx] > vols[prev_low_idx]

    return {
        "divergent": bearish or bullish,
        "bearish": bearish,
        "bullish": bullish,
        "latest_vol_pct": round(vols[idx], 4),
        "prev_high_vol_pct": round(vols[prev_high_idx], 4),
        "prev_low_vol_pct": round(vols[prev_low_idx], 4),
        "reason": "bearish_vol_div" if bearish else ("bullish_vol_div" if bullish else "none"),
    }


def _calc_sma_std(values: List[float], period: int = 20):
    """Rolling SMA and population standard deviation."""
    smas: List[float] = [None] * (period - 1)
    stds: List[float] = [None] * (period - 1)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        sma = sum(window) / period
        variance = sum((x - sma) ** 2 for x in window) / period
        std = math.sqrt(variance)
        smas.append(sma)
        stds.append(std)
    return smas, stds


def _calc_roc(closes: List[float], period: int) -> List[float]:
    """Simple rate-of-change: (close - close[N]) / close[N] * 100."""
    rocs: List[float] = [None] * period
    for i in range(period, len(closes)):
        prev = closes[i - period]
        if prev == 0:
            rocs.append(0.0)
        else:
            rocs.append((closes[i] - prev) / prev * 100.0)
    return rocs


def _calc_atr(candles: List[dict], period: int = 14) -> List[float]:
    """Average true range approximated by simple high-low mean."""
    ranges = [c["h"] - c["l"] for c in candles]
    if len(ranges) < period:
        return [None] * len(ranges)
    atrs: List[float] = [None] * (period - 1)
    for i in range(period - 1, len(ranges)):
        atrs.append(sum(ranges[i - period + 1 : i + 1]) / period)
    return atrs


# ---------------------------------------------------------------------------
# Strategy logic
# ---------------------------------------------------------------------------
def momentum_fade(candles: List[dict]) -> dict:
    """
    Detect momentum-reversal setups:
      – RSI divergence (price makes new local extreme but RSI doesn't).
      – Volume divergence confirming exhaustion.
      – Overextension check (RSI > 70 / < 30 or price > ±2 std dev from 20d SMA).
    """
    MIN_LEN = 45
    if len(candles) < MIN_LEN:
        return {"valid": False, "reason": "insufficient_data"}

    closes = [c["c"] for c in candles]
    rsis = rsi(closes)
    smas, stds = _calc_sma_std(closes)
    rocs7 = _calc_roc(closes, 7)
    rocs30 = _calc_roc(closes, 30)
    atrs = _calc_atr(candles)
    vol_div = volume_divergence(candles)

    idx = len(candles) - 1
    close = closes[idx]
    r = rsis[idx]
    high = candles[idx]["h"]
    low = candles[idx]["l"]
    sma = smas[idx]
    std = stds[idx]
    atr = atrs[idx] if atrs[idx] is not None else 0.0

    if r is None or sma is None or std is None:
        return {"valid": False, "reason": "indicator_calc_error"}

    z_score = (close - sma) / std if std > 0 else 0.0

    # Overextension thresholds
    overextended_up = r > 70.0 or z_score > 2.0
    overextended_down = r < 30.0 or z_score < -2.0

    # RSI divergence
    lookback = 10
    window_start = idx - lookback
    if window_start < 0:
        return {"valid": False, "reason": "lookback_out_of_range"}

    prev_high_idx = max(range(window_start, idx), key=lambda i: candles[i]["h"])
    bearish_div = high > candles[prev_high_idx]["h"] and r < rsis[prev_high_idx]

    prev_low_idx = min(range(window_start, idx), key=lambda i: candles[i]["l"])
    bullish_div = low < candles[prev_low_idx]["l"] and r > rsis[prev_low_idx]

    # ROC divergence (7-day vs 30-day)
    roc7 = rocs7[idx] if rocs7[idx] is not None else 0.0
    roc30 = rocs30[idx] if rocs30[idx] is not None else 0.0
    roc_div_short = roc7 > 0.0 and roc30 < 0.0
    roc_div_long = roc7 < 0.0 and roc30 > 0.0

    # Build signal
    direction = None
    reasons: List[str] = []

    if overextended_up and bearish_div:
        direction = "SHORT"
        reasons.append("rsi_bearish_div")
    elif overextended_down and bullish_div:
        direction = "LONG"
        reasons.append("rsi_bullish_div")

    if roc_div_short:
        if direction == "SHORT":
            reasons.append("roc_divergence")
        elif direction is None:
            direction = "SHORT"
            reasons.append("roc_divergence")
    elif roc_div_long:
        if direction == "LONG":
            reasons.append("roc_divergence")
        elif direction is None:
            direction = "LONG"
            reasons.append("roc_divergence")

    if vol_div["divergent"]:
        if direction is None:
            direction = "SHORT" if vol_div["bearish"] else "LONG"
            reasons.append(vol_div["reason"])
        elif (direction == "SHORT" and vol_div["bearish"]) or (direction == "LONG" and vol_div["bullish"]):
            reasons.append(vol_div["reason"])

    if direction is None:
        return {"valid": False, "reason": "no_divergence"}

    entry_px = round(close, 4)
    if direction == "LONG":
        sl_px = round(entry_px - atr * 1.5, 4)
        tp_px = round(entry_px + atr * 3.0, 4)
    else:
        sl_px = round(entry_px + atr * 1.5, 4)
        tp_px = round(entry_px - atr * 3.0, 4)

    return {
        "valid": True,
        "direction": direction,
        "entry_px": entry_px,
        "sl_px": sl_px,
        "tp_px": tp_px,
        "indicators": {
            "rsi": round(r, 2),
            "sma20": round(sma, 4),
            "std20": round(std, 4),
            "z_score": round(z_score, 4),
            "atr": round(atr, 4),
            "roc7": round(roc7, 4),
            "roc30": round(roc30, 4),
            "bearish_div": bearish_div,
            "bullish_div": bullish_div,
            "roc_div_short": roc_div_short,
            "roc_div_long": roc_div_long,
            "volume_divergence": vol_div,
            "reasons": reasons,
        },
    }


def scan_fades(coins: List[str]) -> List[Dict[str, Any]]:
    """Scan a list of coin symbols for momentum fade signals."""
    signals = []
    for coin in coins:
        cg_id = CG_OHLC_MAP.get(coin, "")
        if not cg_id:
            continue
        candles = get_candles(coin)
        if not candles:
            continue
        sig = momentum_fade(candles)
        if sig.get("valid"):
            signals.append({
                "coin": coin,
                "direction": sig["direction"],
                "entry_px": sig["entry_px"],
                "sl_px": sig["sl_px"],
                "tp_px": sig["tp_px"],
                "indicators": sig["indicators"],
                "dt": datetime.now(timezone.utc).isoformat(),
            })
    return signals


def run_cycle() -> dict:
    """Main entrypoint: scan mapped coins and persist signals."""
    coins = [c for c in list(CG_OHLC_MAP.keys())[:50] if CG_OHLC_MAP.get(c)]
    signals = scan_fades(coins)
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
