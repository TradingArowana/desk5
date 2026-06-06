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
TP_PCT = 0.10               # 10% TP (5:1 R:R)
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

        last_close = current["c"]
        prior_high = max(highs)
        prior_low = min(lows)

        # Volume filter: current candle volume must be >= 1.5x average of prior candles
        prior_vols = [(c["h"] - c["l"]) * c["c"] for c in prior[:-1]] if len(prior) > 1 else []
        avg_prior_vol = sum(prior_vols) / len(prior_vols) if prior_vols else 1
        current_vol = (current["h"] - current["l"]) * current["c"]
        vol_elevated = current_vol >= avg_prior_vol * 1.5 if avg_prior_vol > 0 else True
        vol_proxy = current_vol

        # Trend filter: at least 2 of last 3 prior candles close in signal direction
        recent_closes = closes[-3:] if len(closes) >= 3 else closes
        if len(recent_closes) >= 3:
            up_count = sum(1 for i in range(1, len(recent_closes)) if recent_closes[i] > recent_closes[i-1])
            down_count = sum(1 for i in range(1, len(recent_closes)) if recent_closes[i] < recent_closes[i-1])
        else:
            up_count = down_count = 0

        # Body strength: current candle body >= 40% of range (no dojis)
        body = abs(current["c"] - current["o"])
        rng = current["h"] - current["l"] if current["h"] != current["l"] else 1
        strong_body = (body / rng) >= 0.40 if rng > 0 else False

        # LONG: current close breaks above prior high + elevated vol + up trend + strong body
        if last_close > prior_high and vol_elevated and up_count >= 2 and strong_body:
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

        # SHORT: current close breaks below prior low + elevated vol + down trend + strong body
        elif last_close < prior_low and vol_elevated and down_count >= 2 and strong_body:
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
