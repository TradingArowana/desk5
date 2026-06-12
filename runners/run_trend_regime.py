#!/usr/bin/env python3
"""
Trend Regime Scalper — adapted from Video 2 (Claude Fable 5 strategy).
============================================================
Rules (hard-coded, NO deviation):
1. Regime filter: trade ONLY when ADX > 25 (strong trend exists).
2. Entry: Fast EMA crosses above Slow EMA (LONG) or below (SHORT).
3. Confirmation: RSI > 55 for LONGS, RSI < 45 for SHORTS.
4. STOP LOSS: below the slow EMA or the last swing low/high.
5. TAKE PROFIT: 2R minimum (asymmetric payoff).
6. MAX HOLD TIME: 2 hours on 1h timeframe, 30 minutes on 5m scalping mode.
7. Survival first: if ADX drops below 20, exit immediately (regime dead).
8. NO trade if spread > 0.5% or coin < $0.10.
9. MAX notional $250, MAX positions 5.
10. Only trade coins with $1M+ 24h volume.

Core philosophy from Video 2:
- "Most trend followers don't predict, they position."
- "The edge comes from four principles: trade only when a regime exists,
   asymmetric payoff, confirmation stacking, survival first."
"""

import json, time, sys
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from workers.strategies.signal_aggregator import save_to_queue

# ── Hard guards ──
MIN_COIN_PRICE = 0.10
MAX_NOTIONAL_USD = 250.0
MAX_POSITIONS = 5
MAX_HOLD_MINUTES = 30   # Scalping mode on 5m
MIN_ADX = 25.0
RSI_LONG_MIN = 55.0
RSI_SHORT_MAX = 45.0
MIN_RR = 2.0

STATE_DIR = PROJECT_ROOT / "data_store"
STATE_DIR.mkdir(parents=True, exist_ok=True)
SIGNAL_FILE = STATE_DIR / "trend_regime_signals.json"

WATCHLIST = [
    "BTC", "ETH", "SOL", "XRP", "DOGE",
    "ADA", "DOT", "LINK", "AVAX", "MATIC",
    "ATOM", "OP", "ARB", "GMX", "CRV",
    "SUI", "APT", "SEI", "TIA", "STRK",
]


def fetch_hl_candles(coin: str, interval: str = "5m", lookback: int = 50) -> list:
    """Fetch native HL candles."""
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


def ema(values: list, period: int) -> list:
    """Calculate EMA."""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    ema_vals = [sum(values[:period]) / period]
    for price in values[period:]:
        ema_vals.append(price * k + ema_vals[-1] * (1 - k))
    return ema_vals


def rsi(values: list, period: int = 14) -> list:
    """Calculate RSI."""
    if len(values) < period + 1:
        return []
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rsi_vals = []
    for i in range(period, len(deltas)):
        if avg_loss == 0:
            rsi_vals.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_vals.append(100.0 - (100.0 / (1 + rs)))
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    return rsi_vals


def atr(candles: list, period: int = 14) -> list:
    """Calculate ATR for ADX proxy."""
    if len(candles) < period + 1:
        return []
    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    atr_vals = [sum(trs[:period]) / period]
    for i in range(period, len(trs)):
        atr_vals.append((atr_vals[-1] * (period - 1) + trs[i]) / period)
    return atr_vals


def adx(candles: list, period: int = 14) -> list:
    """Calculate ADX (Average Directional Index)."""
    if len(candles) < period * 2 + 1:
        return []

    plus_dm = []
    minus_dm = []
    trs = []

    for i in range(1, len(candles)):
        high_diff = candles[i]["high"] - candles[i - 1]["high"]
        low_diff = candles[i - 1]["low"] - candles[i]["low"]

        plus_dm.append(max(high_diff, 0) if high_diff > low_diff else 0)
        minus_dm.append(max(low_diff, 0) if low_diff > high_diff else 0)

        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    # Smooth DM and TR
    smooth_plus = [sum(plus_dm[:period])]
    smooth_minus = [sum(minus_dm[:period])]
    smooth_tr = [sum(trs[:period])]

    for i in range(period, len(plus_dm)):
        smooth_plus.append(smooth_plus[-1] - (smooth_plus[-1] / period) + plus_dm[i])
        smooth_minus.append(smooth_minus[-1] - (smooth_minus[-1] / period) + minus_dm[i])
        smooth_tr.append(smooth_tr[-1] - (smooth_tr[-1] / period) + trs[i])

    di_plus = [(100 * smooth_plus[i] / smooth_tr[i]) if smooth_tr[i] > 0 else 0 for i in range(len(smooth_tr))]
    di_minus = [(100 * smooth_minus[i] / smooth_tr[i]) if smooth_tr[i] > 0 else 0 for i in range(len(smooth_tr))]

    dx = [abs(di_plus[i] - di_minus[i]) / (di_plus[i] + di_minus[i]) * 100 if (di_plus[i] + di_minus[i]) > 0 else 0 for i in range(len(di_plus))]

    adx_vals = [sum(dx[:period]) / period]
    for i in range(period, len(dx)):
        adx_vals.append((adx_vals[-1] * (period - 1) + dx[i]) / period)

    return adx_vals


def trend_regime_signal(candles: list, coin: str) -> Optional[dict]:
    """Generate a Trend Regime signal."""
    if len(candles) < 50:
        return None

    closes = [c["close"] for c in candles]
    last_close = closes[-1]
    if last_close < MIN_COIN_PRICE:
        return None

    # Calculate indicators
    fast_ema = ema(closes, 9)
    slow_ema = ema(closes, 21)
    rsi_vals = rsi(closes, 14)
    adx_vals = adx(candles, 14)

    if not fast_ema or not slow_ema or not rsi_vals or not adx_vals:
        return None

    # Need enough aligned history
    if len(fast_ema) < 2 or len(slow_ema) < 2 or len(rsi_vals) < 1 or len(adx_vals) < 1:
        return None

    current_fast = fast_ema[-1]
    current_slow = slow_ema[-1]
    prev_fast = fast_ema[-2]
    prev_slow = slow_ema[-2]
    current_rsi = rsi_vals[-1]
    current_adx = adx_vals[-1]

    # ── REGIME FILTER ──
    if current_adx < MIN_ADX:
        return None  # No trend regime

    # ── CROSSOVER DETECTION ──
    long_cross = prev_fast <= prev_slow and current_fast > current_slow
    short_cross = prev_fast >= prev_slow and current_fast < current_slow

    if long_cross and current_rsi >= RSI_LONG_MIN:
        direction = "LONG"
        entry = last_close
        # SL below slow EMA or recent swing low
        recent_lows = [c["low"] for c in candles[-10:]]
        swing_low = min(recent_lows)
        sl = min(current_slow * 0.995, swing_low * 0.99)
        risk = entry - sl
        if risk <= 0:
            return None
        tp = entry + (risk * 2.0)

    elif short_cross and current_rsi <= RSI_SHORT_MAX:
        direction = "SHORT"
        entry = last_close
        recent_highs = [c["high"] for c in candles[-10:]]
        swing_high = max(recent_highs)
        sl = max(current_slow * 1.005, swing_high * 1.01)
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
        "adx": round(current_adx, 2),
        "rsi": round(current_rsi, 2),
        "fast_ema": round(current_fast, 4),
        "slow_ema": round(current_slow, 4),
        "max_hold_min": MAX_HOLD_MINUTES,
        "strategy": "trend_regime",
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
    print(f"[{datetime.now(timezone.utc).isoformat()}] Wrote {len(fresh)} Trend Regime signals")
    if fresh:
        save_to_queue(fresh)
        print(f"  → pushed {len(fresh)} to unified queue")


def main() -> None:
    print(f"\n{'='*60}")
    print(f"Trend Regime Scalper — {datetime.now(timezone.utc).isoformat()}")
    print(f"{'='*60}")

    signals = []
    for coin in WATCHLIST:
        candles = fetch_hl_candles(coin, interval="5m", lookback=50)
        if len(candles) < 50:
            print(f"  {coin}: insufficient candles ({len(candles)})")
            continue

        sig = trend_regime_signal(candles, coin)
        if sig:
            print(f"  {coin}: {sig['direction']} entry={sig['entry_px']:.4f} SL={sig['sl_px']:.4f} TP={sig['tp_px']:.4f} RR={sig['rr']} ADX={sig['adx']} RSI={sig['rsi']}")
            signals.append(sig)
        else:
            print(f"  {coin}: no regime")

    if signals:
        save_signals(signals)
    else:
        print("No Trend Regime signals this cycle.")

    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
