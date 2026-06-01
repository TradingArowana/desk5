"""
HL Vol Squeeze — uses native Hyperliquid candles.
Detects Bollinger Band squeezes: low volatility compression before expansion.
Complements hl_breakout (catches moves BEFORE they happen).
"""
import json, logging, sys
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workers.caches.hl_candle_fetcher import fetch_hl_candles
from workers.strategies.signal_aggregator import is_tradeable, save_to_queue

logger = logging.getLogger("hl_vol_squeeze")

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
STATE_DIR.mkdir(parents=True, exist_ok=True)
SIGNALS_PATH = STATE_DIR / "hl_vol_squeeze_signals.json"

# Top HL perps to scan
WATCHLIST = [
    "BTC","ETH","SOL","DOGE","AVAX","LINK","BNB","XRP","ADA","DOT",
    "ATOM","CRV","LDO","GMX","SUI","AAVE","COMP","TRX","APE","APT",
    "BCH","SNX","INJ","STX","OP","ARB","FTM","RNDR","MKR","WLD",
    "MATIC","LTC","DYDX","CFX","PEPE","WIF","BONK","FLOKI",
]


def bb_squeeze(candles: List[dict], mult: float = 2.0) -> dict:
    """Detect squeeze: bandwidth narrow relative to recent history."""
    if len(candles) < 20:
        return {"valid": False}
    
    closes = [c["c"] for c in candles]
    sma = sum(closes) / len(closes)
    variance = sum((c - sma) ** 2 for c in closes) / len(closes)
    std = variance ** 0.5
    upper = sma + mult * std
    lower = sma - mult * std
    bandwidth = ((upper - lower) / sma * 100) if sma > 0 else 0
    
    # Need 20 candles for bandwidth history
    if len(candles) < 20:
        return {"valid": False}
    
    # Rolling bandwidth on prior 20 candles
    bandwidths = []
    for i in range(20, len(candles) + 1):
        window = closes[i-20:i]
        w_sma = sum(window) / len(window)
        w_var = sum((c - w_sma) ** 2 for c in window) / len(window)
        w_std = w_var ** 0.5
        w_upper = w_sma + mult * w_std
        w_lower = w_sma - mult * w_std
        w_band = ((w_upper - w_lower) / w_sma * 100) if w_sma > 0 else 0
        bandwidths.append(w_band)
    
    if len(bandwidths) < 2:
        return {"valid": False}
    
    current_bw = bandwidths[-1]
    avg_bw = sum(bandwidths) / len(bandwidths)
    lowest_bw = min(bandwidths)
    
    # Squeeze = bandwidth in lowest 20% of recent range AND below threshold
    squeeze = current_bw < 5.0 or current_bw < lowest_bw * 1.2
    
    # Bias based on price vs SMA and recent momentum
    bias = "LONG" if closes[-1] > sma and closes[-1] > closes[-5] else "SHORT"
    
    # ATR for SL/TP sizing
    atr = sum(c["h"] - c["l"] for c in candles[-20:]) / 20
    
    return {
        "valid": True,
        "sma": round(sma, 6),
        "upper": round(upper, 6),
        "lower": round(lower, 6),
        "bandwidth_pct": round(current_bw, 4),
        "avg_bandwidth": round(avg_bw, 4),
        "squeeze": squeeze,
        "bias": bias,
        "atr": round(atr, 6),
        "expected_move_pct": round(atr / sma * 100, 4) if sma > 0 else 0,
    }


def scan_squeezes(coins: List[str]) -> List[Dict[str, Any]]:
    signals = []
    for coin in coins:
        if not is_tradeable(coin):
            continue
            
        try:
            candles = fetch_hl_candles(coin, "15m", 50)
        except Exception as exc:
            logger.debug("Skip %s — fetch error: %s", coin, exc)
            continue
            
        if not candles or len(candles) < 20:
            continue
            
        sig = bb_squeeze(candles)
        if sig.get("valid") and sig.get("squeeze"):
            last_c = candles[-1]
            entry_px = round(last_c["c"], 6)
            atr = sig["atr"]
            
            if sig["bias"] == "LONG":
                sl_px = round(entry_px - atr * 1.5, 6)
                tp_px = round(entry_px + atr * 3.0, 6)
            else:
                sl_px = round(entry_px + atr * 1.5, 6)
                tp_px = round(entry_px - atr * 3.0, 6)
            
            signals.append({
                "coin": coin,
                "direction": sig["bias"],
                "entry_px": entry_px,
                "sl_px": sl_px,
                "tp_px": tp_px,
                "bandwidth": sig["bandwidth_pct"],
                "expected_move_pct": sig["expected_move_pct"],
                "dt": datetime.now(timezone.utc).isoformat(),
                "_source": "hl_vol_squeeze",
            })
            logger.info("SQUEEZE %s %s @ %.4f (bw=%.2f%%)", coin, sig["bias"], entry_px, sig["bandwidth_pct"])
    
    return signals


def main():
    """One-shot signal generation + queue push."""
    signals = scan_squeezes(WATCHLIST)
    
    # Save to strategy file
    if signals:
        SIGNALS_PATH.write_text(json.dumps(signals, indent=2, default=str))
        
    # Push to unified execution queue
    for sig in signals:
        save_to_queue(sig)
        
    logger.info("Generated %d vol-squeeze signals → queue", len(signals))
    return signals


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = main()
    print(json.dumps({"count": len(result), "signals": result}, indent=2))
    sys.exit(0)
