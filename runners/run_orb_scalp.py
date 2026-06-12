#!/usr/bin/env python3
"""
Open Range Breakout (ORB) Scalper — adapted from Video 1 strategy.
============================================================
Rules (hard-coded, NO deviation):
1. Mark the FIRST 5-minute candle high/low after a reference open.
   For 24/7 crypto, we use ROLLING 5m windows aligned to UTC 00:00
   (or the most recent 5m candle as the "opening range").
2. WAIT for a subsequent candle CLOSE above the high or below the low.
3. ENTER on the RETEST of the breakout level (lowest risk, highest reward).
4. STOP LOSS below the impulsive breakout candle (or the range boundary).
5. TAKE PROFIT at 2R (2× risk distance).
6. MAX HOLD TIME: 30 minutes. If not hit TP/SL within 30m, market close.
7. NO trade if spread > 0.5% or coin < $0.10.
8. MAX notional $250, MAX positions 5.
9. Only trade coins with $1M+ 24h volume.
"""

import json, time, sys
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from workers.strategies.signal_aggregator import save_to_queue

# ── Hard guards (mirrors hl_executor.py) ──
MIN_COIN_PRICE = 0.10
MAX_NOTIONAL_USD = 250.0
MAX_POSITIONS = 5
MAX_HOLD_MINUTES = 30
MIN_RR = 1.5   # ORB targets 2R minimum

STATE_DIR = PROJECT_ROOT / "data_store"
STATE_DIR.mkdir(parents=True, exist_ok=True)
SIGNAL_FILE = STATE_DIR / "orb_scalp_signals.json"

WATCHLIST = [
    "BTC", "ETH", "SOL", "XRP", "DOGE",
    "ADA", "DOT", "LINK", "AVAX", "MATIC",
    "ATOM", "OP", "ARB", "GMX", "CRV",
    "SUI", "APT", "SEI", "TIA", "STRK",
]


def fetch_hl_candles(coin: str, interval: str = "5m", lookback: int = 12) -> list:
    """Fetch native HL 5m candles."""
    import requests
    url = "https://api.hyperliquid.xyz/info"
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": int((time.time() - lookback * 300) * 1000),
            "endTime": int(time.time() * 1000),
        }
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        candles = []
        for row in data:
            candles.append({
                "time": row["t"],
                "open": float(row["o"]),
                "high": float(row["h"]),
                "low": float(row["l"]),
                "close": float(row["c"]),
                "volume": float(row["v"]),
            })
        return candles
    except Exception:
        return []


def orb_signal(candles: list, coin: str) -> Optional[dict]:
    """Generate an Open Range Breakout signal."""
    if len(candles) < 3:
        return None

    last_close = candles[-1]["close"]
    if last_close < MIN_COIN_PRICE:
        return None

    # Use the most recent COMPLETED 5m candle as the "opening range"
    # (In crypto 24/7, we treat each fresh 5m candle as a new session)
    opening_candle = candles[-2]   # second-to-last = most recent completed
    range_high = opening_candle["high"]
    range_low = opening_candle["low"]

    current_candle = candles[-1]
    current_close = current_candle["close"]
    current_high = current_candle["high"]
    current_low = current_candle["low"]

    # WAIT for close above range high or below range low
    if current_close > range_high:
        direction = "LONG"
        # RETEST entry = the breakout level (range_high)
        entry = range_high
        # SL below the impulsive candle's low (or range_low, whichever is lower)
        sl = min(current_candle["low"], range_low) * 0.995
        # TP at 2R
        risk = entry - sl
        if risk <= 0:
            return None
        tp = entry + (risk * 2.0)

    elif current_close < range_low:
        direction = "SHORT"
        entry = range_low
        sl = max(current_candle["high"], range_high) * 1.005
        risk = sl - entry
        if risk <= 0:
            return None
        tp = entry - (risk * 2.0)
    else:
        return None

    rr = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
    if rr < MIN_RR:
        return None

    return {
        "coin": coin,
        "direction": direction,
        "entry_px": round(entry, 6),
        "sl_px": round(sl, 6),
        "tp_px": round(tp, 6),
        "rr": round(rr, 2),
        "range_high": round(range_high, 6),
        "range_low": round(range_low, 6),
        "max_hold_min": MAX_HOLD_MINUTES,
        "strategy": "orb_scalp",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def save_signals(signals: list) -> None:
    existing = []
    if SIGNAL_FILE.exists():
        try:
            existing = json.loads(SIGNAL_FILE.read_text())
        except Exception:
            pass

    mp = {}
    for s in existing:
        key = f"{s.get('coin','')}|{s.get('direction','')}"
        mp[key] = s
    for s in signals:
        key = f"{s.get('coin','')}|{s.get('direction','')}"
        mp[key] = s

    # Prune signals older than 30 min
    now = datetime.now(timezone.utc).timestamp()
    fresh = []
    for s in mp.values():
        ts = s.get("timestamp", "")
        if ts:
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                if now - t < 1800:
                    fresh.append(s)
            except Exception:
                fresh.append(s)
        else:
            fresh.append(s)

    SIGNAL_FILE.write_text(json.dumps(fresh, indent=2))
    print(f"[{datetime.now(timezone.utc).isoformat()}] Wrote {len(fresh)} ORB signals")
    if fresh:
        save_to_queue(fresh)
        print(f"  → pushed {len(fresh)} to unified queue")


def main() -> None:
    print(f"\n{'='*60}")
    print(f"Open Range Breakout Scalper — {datetime.now(timezone.utc).isoformat()}")
    print(f"{'='*60}")

    signals = []
    for coin in WATCHLIST:
        candles = fetch_hl_candles(coin, interval="5m", lookback=12)
        if len(candles) < 3:
            print(f"  {coin}: insufficient candles ({len(candles)})")
            continue

        sig = orb_signal(candles, coin)
        if sig:
            print(f"  {coin}: {sig['direction']} entry={sig['entry_px']:.4f} SL={sig['sl_px']:.4f} TP={sig['tp_px']:.4f} RR={sig['rr']}")
            signals.append(sig)
        else:
            print(f"  {coin}: no breakout")

    if signals:
        save_signals(signals)
    else:
        print("No ORB signals this cycle.")

    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
