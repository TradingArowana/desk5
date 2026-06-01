#!/usr/bin/env python3
"""
QuickScalp v2 — Live Hyperliquid Native.
Same breakout logic as v1 but uses HL candles (no CoinGecko)
and saves to unified queue via signal_aggregator.
"""
import json, logging, sys
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workers.caches.hl_candle_fetcher import fetch_hl_candles
from workers.strategies.signal_aggregator import is_tradeable, save_to_queue

logger = logging.getLogger("quick_scalp_v2")

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
STATE_DIR.mkdir(parents=True, exist_ok=True)
SIGNALS_PATH = STATE_DIR / "quick_scalp_signals.json"

# Micro-range config — tighter than hl_breakout for scalp frequency
LOOKBACK_CANDLES = 12       # ~1h of 5m candles
SL_PCT = 0.02               # 2% SL
TP_PCT = 0.05               # 5% TP (2.5:1 R:R)
VOL_MULT = 1.0              # Any volume

# Top coins by volume preference
PREFERRED_COINS = [
    "BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK", "BNB", "XRP",
    "ADA", "SUI", "APT", "PEPE", "OP", "ARB", "INJ", "STX",
    "RNDR", "DYDX", "CRV", "LTC", "MATIC", "TRX", "AAVE", "COMP",
]


def generate_signals() -> List[Dict]:
    signals = []
    for coin in PREFERRED_COINS:
        if not is_tradeable(coin):
            continue

        # Use 5m timeframe for scalp granularity
        candles = fetch_hl_candles(coin, "5m", LOOKBACK_CANDLES + 1)
        if not candles or len(candles) < LOOKBACK_CANDLES:
            continue

        # Exclude current forming candle
        prior = candles[:-1]
        current = candles[-1]

        closes = [c["c"] for c in prior]
        highs = [c["h"] for c in prior]
        lows = [c["l"] for c in prior]

        # Volume proxy
        vol_proxy = sum((c["h"] - c["l"]) * c["c"] for c in prior)
        avg_vol = vol_proxy / len(prior) if prior else 1

        last_close = current["c"]
        prior_high = max(highs) if highs else last_close
        prior_low = min(lows) if lows else last_close

        # LONG: current close breaks above prior high + volume
        if last_close > prior_high and vol_proxy >= avg_vol * VOL_MULT:
            sl = last_close * (1 - SL_PCT)
            tp = last_close * (1 + TP_PCT)
            signals.append({
                "coin": coin,
                "direction": "LONG",
                "entry_px": round(last_close, 6),
                "sl_px": round(sl, 6),
                "tp_px": round(tp, 6),
                "dt": datetime.now(timezone.utc).isoformat(),
                "vol_24h": 0,
                "volume_24h": vol_proxy,
                "_source": "quick_scalp",
                "_rr": round(TP_PCT / SL_PCT, 1),
            })

        # SHORT: current close breaks below prior low + volume
        elif last_close < prior_low and vol_proxy >= avg_vol * VOL_MULT:
            sl = last_close * (1 + SL_PCT)
            tp = last_close * (1 - TP_PCT)
            signals.append({
                "coin": coin,
                "direction": "SHORT",
                "entry_px": round(last_close, 6),
                "sl_px": round(sl, 6),
                "tp_px": round(tp, 6),
                "dt": datetime.now(timezone.utc).isoformat(),
                "vol_24h": 0,
                "volume_24h": vol_proxy,
                "_source": "quick_scalp",
                "_rr": round(TP_PCT / SL_PCT, 1),
            })

    logger.info("QuickScalp v2: generated %d signals", len(signals))
    
    # Save to unified queue
    if signals:
        save_to_queue(signals)
        SIGNALS_PATH.write_text(json.dumps(signals, indent=2))
    else:
        SIGNALS_PATH.write_text("[]")
    
    return signals


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    signals = generate_signals()
    print(json.dumps({"signals_generated": len(signals), "coins": [s["coin"] for s in signals]}, indent=2))
