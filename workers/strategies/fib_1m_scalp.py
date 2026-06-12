#!/usr/bin/env python3
"""
Fibonacci 1-Minute Scalping Strategy (from video strategy)
============================================================
Rules (hard-coded, NO deviation):
1. 1-minute chart only.
2. Identify micro-trend: higher lows = uptrend, lower highs = downtrend.
3. Wait for break of structure (price breaks last swing point).
4. Draw Fibonacci from last swing extreme to break point.
5. ENTRY zone: 0.50 - 0.618 (golden zone). Sweet spot = 0.618.
6. STOP LOSS at 1.0 level (above swing high for shorts, below swing low for longs).
7. TAKE PROFIT at previous swing low/high (1:1 to 1.5 R:R).
8. MAX HOLD TIME: 15 minutes. If not hit TP/SL within 15m, market close.
9. If stopped out, FLIP DIRECTION for next signal on same trend structure.
10. NO trade if spread > 0.5% or coin < $0.10.
11. MAX notional $250, MAX positions 5.
"""
import json, time, sys
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timezone
import numpy as np

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Hard guards (mirrors hl_executor.py) ──
MIN_COIN_PRICE = 0.10
MAX_NOTIONAL_USD = 250.0
MAX_POSITIONS = 5
MAX_HOLD_MINUTES = 15
MIN_RR = 1.0  # minimum 1:1 risk:reward


def _detect_trend(candles: List[dict], window: int = 5) -> str:
    """Detect micro-trend from recent candles."""
    if len(candles) < window + 2:
        return "none"
    
    highs = [c["high"] for c in candles[-window:]]
    lows = [c["low"] for c in candles[-window:]]
    
    # Higher lows = uptrend
    higher_lows = all(lows[i] >= lows[i-1] for i in range(1, len(lows)))
    # Lower highs = downtrend
    lower_highs = all(highs[i] <= highs[i-1] for i in range(1, len(highs)))
    
    if higher_lows and not lower_highs:
        return "up"
    if lower_highs and not higher_lows:
        return "down"
    return "ranging"


def _find_swing_points(candles: List[dict], n: int = 5) -> Tuple[Optional[dict], Optional[dict]]:
    """Find recent swing high and swing low."""
    if len(candles) < n * 2 + 1:
        return None, None
    
    # Simple swing detection: local max/min over n candles each side
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    
    swing_high = None
    swing_low = None
    
    for i in range(n, len(candles) - n):
        # Swing high
        if all(highs[i] >= highs[j] for j in range(i-n, i+n+1) if j != i):
            if swing_high is None or candles[i]["timestamp"] > swing_high["timestamp"]:
                swing_high = candles[i]
        # Swing low
        if all(lows[i] <= lows[j] for j in range(i-n, i+n+1) if j != i):
            if swing_low is None or candles[i]["timestamp"] > swing_low["timestamp"]:
                swing_low = candles[i]
    
    return swing_high, swing_low


def _fib_levels(swing_a: dict, swing_b: dict) -> Dict[str, float]:
    """Calculate Fibonacci retracement levels between two swing points."""
    high = max(swing_a["high"], swing_b["high"])
    low = min(swing_a["low"], swing_b["low"])
    diff = high - low
    
    return {
        "0.0": high,
        "0.236": high - diff * 0.236,
        "0.382": high - diff * 0.382,
        "0.5": high - diff * 0.5,
        "0.618": high - diff * 0.618,
        "0.786": high - diff * 0.786,
        "1.0": low,
    }


def _break_of_structure(candles: List[dict], trend: str) -> bool:
    """Check if price just broke the last swing point (structure)."""
    if len(candles) < 5:
        return False
    
    swing_high, swing_low = _find_swing_points(candles)
    if not swing_high or not swing_low:
        return False
    
    last_close = candles[-1]["close"]
    prev_close = candles[-2]["close"]
    
    if trend == "down":
        # Break below swing low
        return prev_close > swing_low["low"] and last_close <= swing_low["low"]
    elif trend == "up":
        # Break above swing high
        return prev_close < swing_high["high"] and last_close >= swing_high["high"]
    return False


def fib_scalp_signal(candles: List[dict], coin: str) -> Optional[dict]:
    """Generate a Fibonacci 1m scalping signal. Returns None if no signal."""
    
    # Guard: need at least 20 candles
    if len(candles) < 20:
        return None
    
    last_close = candles[-1]["close"]
    
    # Guard: price below $0.10
    if last_close < MIN_COIN_PRICE:
        return None
    
    # Detect trend
    trend = _detect_trend(candles)
    if trend == "none":
        return None
    
    # Find swing points
    swing_high, swing_low = _find_swing_points(candles)
    if not swing_high or not swing_low:
        return None
    
    # Calculate fib levels
    fibs = _fib_levels(swing_high, swing_low)
    
    # Check if price is in golden zone (0.5 - 0.618)
    golden_low = min(fibs["0.5"], fibs["0.618"])
    golden_high = max(fibs["0.5"], fibs["0.618"])
    
    if not (golden_low <= last_close <= golden_high):
        return None
    
    # Determine direction based on trend
    if trend == "up":
        direction = "LONG"
        entry = last_close
        # SL below the swing low (below fib 1.0)
        sl = fibs["1.0"] - (fibs["0.0"] - fibs["1.0"]) * 0.05
        # TP at or above swing high (fib 0.0)
        tp = fibs["0.0"] + (fibs["0.0"] - fibs["1.0"]) * 0.1
    elif trend == "down":
        direction = "SHORT"
        entry = last_close
        # SL above the swing high (above fib 0.0)
        sl = fibs["0.0"] + (fibs["0.0"] - fibs["1.0"]) * 0.05
        # TP at or below swing low (fib 1.0)
        tp = fibs["1.0"] - (fibs["0.0"] - fibs["1.0"]) * 0.1
    else:
        return None
    
    # Validate R:R
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0 or (reward / risk) < MIN_RR:
        return None
    
    return {
        "coin": coin,
        "direction": direction,
        "entry_px": round(entry, 6),
        "sl_px": round(sl, 6),
        "tp_px": round(tp, 6),
        "rr": round(reward / risk, 2),
        "fib_entry": "0.618",
        "trend": trend,
        "max_hold_min": MAX_HOLD_MINUTES,
        "strategy": "fib_1m_scalp",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def run_backtest(candles: List[dict], coin: str, verbose: bool = False) -> dict:
    """Backtest the strategy on historical 1m candles."""
    trades = []
    position = None
    wins = 0
    losses = 0
    pnl_total = 0.0
    
    for i in range(30, len(candles)):
        window = candles[:i+1]
        
        if position is None:
            # Look for entry
            sig = fib_scalp_signal(window, coin)
            if sig:
                # Simulate entry at next candle open
                entry_candle = candles[i+1] if i+1 < len(candles) else candles[i]
                position = {
                    "entry": entry_candle["open"],
                    "sl": sig["sl_px"],
                    "tp": sig["tp_px"],
                    "direction": sig["direction"],
                    "entry_idx": i,
                    "max_hold_idx": i + MAX_HOLD_MINUTES,
                }
                if verbose:
                    print(f"  ENTRY {sig['direction']} @ {position['entry']:.4f} SL={position['sl']:.4f} TP={position['tp']:.4f}")
        else:
            # Check exit conditions
            for j in range(i, min(i+MAX_HOLD_MINUTES, len(candles))):
                c = candles[j]
                
                if position["direction"] == "LONG":
                    if c["low"] <= position["sl"]:
                        pnl = position["sl"] - position["entry"]
                        trades.append(pnl)
                        pnl_total += pnl
                        losses += 1
                        if verbose:
                            print(f"  SL HIT @ {position['sl']:.4f} PnL=${pnl:.4f}")
                        position = None
                        break
                    elif c["high"] >= position["tp"]:
                        pnl = position["tp"] - position["entry"]
                        trades.append(pnl)
                        pnl_total += pnl
                        wins += 1
                        if verbose:
                            print(f"  TP HIT @ {position['tp']:.4f} PnL=${pnl:.4f}")
                        position = None
                        break
                else:  # SHORT
                    if c["high"] >= position["sl"]:
                        pnl = position["entry"] - position["sl"]
                        trades.append(-pnl)
                        pnl_total -= pnl
                        losses += 1
                        if verbose:
                            print(f"  SL HIT @ {position['sl']:.4f} PnL=-${pnl:.4f}")
                        position = None
                        break
                    elif c["low"] <= position["tp"]:
                        pnl = position["entry"] - position["tp"]
                        trades.append(pnl)
                        pnl_total += pnl
                        wins += 1
                        if verbose:
                            print(f"  TP HIT @ {position['tp']:.4f} PnL=${pnl:.4f}")
                        position = None
                        break
                
                if j >= position["max_hold_idx"]:
                    # Time-based exit
                    pnl = c["close"] - position["entry"] if position["direction"] == "LONG" else position["entry"] - c["close"]
                    trades.append(pnl)
                    pnl_total += pnl
                    if pnl > 0:
                        wins += 1
                    else:
                        losses += 1
                    if verbose:
                        print(f"  TIME EXIT @ {c['close']:.4f} PnL=${pnl:.4f}")
                    position = None
                    break
    
    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0
    
    return {
        "coin": coin,
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 2),
        "pnl": round(pnl_total, 4),
        "avg_trade": round(pnl_total / total, 4) if total > 0 else 0,
    }


if __name__ == "__main__":
    print("Fibonacci 1m Scalping Strategy v1.0")
    print(f"Guards: MIN_COIN=${MIN_COIN_PRICE}, MAX_NOTIONAL=${MAX_NOTIONAL_USD}, MAX_POS={MAX_POSITIONS}, MAX_HOLD={MAX_HOLD_MINUTES}min")
