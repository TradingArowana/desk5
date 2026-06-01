"""
PAXG Commodity Scalper for Desk5.
Scalps gold (PAXG) perp on Hyperliquid using hourly volatility breakouts
informed by Brent/WTI macro signals from commodity_sentinel.
"""
import asyncio
import json
import logging
import math
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
STATE_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = STATE_DIR / "paxg_history.json"
SIGNALS_FILE = STATE_DIR / "paxg_scalp_signals.json"
SIM_FILE = STATE_DIR / "paxg_sim_report.json"

# ---------------------------------------------------------------------------
# Hyperliquid fetchers
# ---------------------------------------------------------------------------

HL_INFO = "https://api.hyperliquid.xyz/info"

def hl_post(payload: dict, timeout: int = 15) -> dict:
    r = requests.post(HL_INFO, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_candle_range(coin: str, interval: str, start_ms: int, end_ms: int) -> List[List[float]]:
    """Fetch candleSnapshot from Hyperliquid. Returns list of [t,o,h,l,c,v]."""
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        }
    }
    try:
        data = hl_post(payload)
        if isinstance(data, list):
            return data
        logger.warning("HL candleSnapshot returned non-list: %s", type(data))
        return []
    except Exception as exc:
        logger.warning("HL candle fetch failed: %s", exc)
        return []


def build_paxg_history(days: int = 100, interval: str = "1h") -> pd.DataFrame:
    """Build {days} of candles from HL."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    # HL has 15-min granularity limit for 100-day range
    # For hourly: ~100 days * 24h = 2400 candles, well within limits
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days+2)).timestamp() * 1000)
    raw = fetch_candle_range("PAXG", interval, start_ms, now_ms)
    if not raw:
        logger.error("No PAXG candle data from HL")
        return pd.DataFrame()
    
    # Hyperliquid candleSnapshot can return EITHER:
    # 1) Array format: [timestamp_ms, open, high, low, close, volume]
    # 2) Object format: {"t": ts, "T": endTs, "s": symbol, "i": interval, "o": open, "c": close, "h": high, "l": low, "v": volume}
    if isinstance(raw[0], dict):
        # Named field format
        df = pd.DataFrame(raw)
        df = df.rename(columns={"t": "timestamp_ms", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    else:
        # Array format
        df = pd.DataFrame(raw, columns=["timestamp_ms", "open", "high", "low", "close", "volume"])
    
    df["dt"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df = df.sort_values("dt").reset_index(drop=True)
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    
    logger.info(f"Got {len(df)} {interval} candles, range ${df['close'].min():.2f} - ${df['close'].max():.2f}")
    return df


def save_history(df: pd.DataFrame):
    df.to_json(HISTORY_FILE, orient="records", date_format="iso", indent=2)


def load_history() -> Optional[pd.DataFrame]:
    if not HISTORY_FILE.exists():
        return None
    try:
        df = pd.read_json(HISTORY_FILE)
        return df
    except Exception as exc:
        logger.warning("History load failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Indicator calculations
# ---------------------------------------------------------------------------

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add ATR, Bollinger, and volatility metrics."""
    df = df.copy()
    
    # ATR (true range)
    df["prev_close"] = df["close"].shift(1)
    df["tr1"] = df["high"] - df["low"]
    df["tr2"] = (df["high"] - df["prev_close"]).abs()
    df["tr3"] = (df["low"] - df["prev_close"]).abs()
    df["tr"] = df[["tr1", "tr2", "tr3"]].max(axis=1)
    df["atr_14"] = df["tr"].rolling(window=14).mean()
    
    # Short-window Bollinger (volatility contraction/expansion)
    df["sma_20"] = df["close"].rolling(window=20).mean()
    df["std_20"] = df["close"].rolling(window=20).std()
    df["upper_bb"] = df["sma_20"] + 2 * df["std_20"]
    df["lower_bb"] = df["sma_20"] - 2 * df["std_20"]
    
    # Volatility metrics (hourly-scale)
    df["roc_1h"] = (df["close"] / df["close"].shift(1) - 1) * 100
    df["roc_4h"] = (df["close"] / df["close"].shift(4) - 1) * 100
    df["atr_pct"] = (df["atr_14"] / df["close"]) * 100
    
    # BB width (lower = squeeze = setup)
    df["bb_width"] = (df["upper_bb"] - df["lower_bb"]) / df["sma_20"]
    df["bb_pct"] = (df["close"] - df["lower_bb"]) / (df["upper_bb"] - df["lower_bb"])
    
    # Volatility regime: 20-period BB width vs avg
    df["bb_width_ma20"] = df["bb_width"].rolling(window=20).mean()
    df["bb_width_ratio"] = df["bb_width"] / df["bb_width_ma20"]
    
    # ATR vs SMA trend (trending?)
    df["ema_8"] = df["close"].ewm(span=8).mean()
    df["ema_21"] = df["close"].ewm(span=21).mean()
    
    return df


# ---------------------------------------------------------------------------
# Signal generation — hourly scalping on PAXG
# ---------------------------------------------------------------------------

def generate_signals(df: pd.DataFrame, atr_mult_sl: float = 1.5, atr_mult_tp: float = 3.0) -> List[Dict[str, Any]]:
    """Generate PAXG hourly scalping signals."""
    signals = []
    
    # We need at least 25 bars of history
    min_lookback = 25
    
    for i in range(min_lookback, len(df)):
        row = df.iloc[i]
        if pd.isna(row["atr_14"]) or pd.isna(row["bb_width_ratio"]):
            continue
        
        atr = row["atr_14"]
        close = row["close"]
        
        if pd.isna(atr) or atr <= 0:
            continue
        
        # Skip if volatility too low (dead market)
        if row["atr_pct"] < 0.1:
            continue
        
    # --- SIGNAL 1: BB Squeeze Breakout (high confidence only) ---
    squeeze = row["bb_width_ratio"] < 0.7
    
    if squeeze:
        bb_pct = row["bb_pct"]
        # Strong confirmation: close well outside band + aligned EMA
        if close > row["upper_bb"] and row["ema_8"] > row["ema_21"] and row["roc_1h"] > 0.2:
            entry = close
            sl = entry - atr * 2.0  # wider SL for squeeze breakout
            tp = entry + atr * 4.0  # 2:1 R/R
            signals.append({
                "dt": row["dt"].isoformat(),
                "direction": "LONG",
                "entry_px": round(entry, 2),
                "sl_px": round(sl, 2),
                "tp_px": round(tp, 2),
                "signal_type": "bb_squeeze_long",
                "confidence": min(1.0, abs(row["roc_1h"]) / 0.8) if pd.notna(row["roc_1h"]) else 0.5,
                "atr_14": round(atr, 2),
                "bb_ratio": round(row["bb_width_ratio"], 3),
            })
        elif close < row["lower_bb"] and row["ema_8"] < row["ema_21"] and row["roc_1h"] < -0.2:
            entry = close
            sl = entry + atr * 2.0
            tp = entry - atr * 4.0
            signals.append({
                "dt": row["dt"].isoformat(),
                "direction": "SHORT",
                "entry_px": round(entry, 2),
                "sl_px": round(sl, 2),
                "tp_px": round(tp, 2),
                "signal_type": "bb_squeeze_short",
                "confidence": min(1.0, abs(row["roc_1h"]) / 0.8) if pd.notna(row["roc_1h"]) else 0.5,
                "atr_14": round(atr, 2),
                "bb_ratio": round(row["bb_width_ratio"], 3),
            })
    
    # --- SIGNAL 2: Volatility Expansion Momentum (tighter thresholds) ---
    vol_spike = row["bb_width_ratio"] > 2.0  # needs real expansion
    
    if vol_spike and row["roc_1h"] > 0.5 and row["ema_8"] > row["ema_21"]:  # 0.5% hourly move minimum
        entry = close
        sl = entry - atr * 1.5
        tp = entry + atr * 3.0
        signals.append({
            "dt": row["dt"].isoformat(),
            "direction": "LONG",
            "entry_px": round(entry, 2),
            "sl_px": round(sl, 2),
            "tp_px": round(tp, 2),
            "signal_type": "vol_momentum_long",
            "confidence": min(1.0, abs(row["roc_1h"]) / 1.0) if pd.notna(row["roc_1h"]) else 0.5,
            "atr_14": round(atr, 2),
            "bb_ratio": round(row["bb_width_ratio"], 3),
        })
    elif vol_spike and row["roc_1h"] < -0.5 and row["ema_8"] < row["ema_21"]:
        entry = close
        sl = entry + atr * 1.5
        tp = entry - atr * 3.0
        signals.append({
            "dt": row["dt"].isoformat(),
            "direction": "SHORT",
            "entry_px": round(entry, 2),
            "sl_px": round(sl, 2),
            "tp_px": round(tp, 2),
            "signal_type": "vol_momentum_short",
            "confidence": min(1.0, abs(row["roc_1h"]) / 1.0) if pd.notna(row["roc_1h"]) else 0.5,
            "atr_14": round(atr, 2),
            "bb_ratio": round(row["bb_width_ratio"], 3),
        })
        
        # --- SIGNAL 3: Mean Reversion (extreme touch of BB) ---
        if row["bb_pct"] > 0.95 and row["roc_4h"] > 0.5:
            entry = close
            sl = entry + atr * 1.2
            tp = entry - atr * 2.0
            signals.append({
                "dt": row["dt"].isoformat(),
                "direction": "SHORT",
                "entry_px": round(entry, 2),
                "sl_px": round(sl, 2),
                "tp_px": round(tp, 2),
                "signal_type": "mean_reversion_short",
                "confidence": 0.6,
                "atr_14": round(atr, 2),
                "bb_ratio": round(row["bb_width_ratio"], 3),
            })
        elif row["bb_pct"] < 0.05 and row["roc_4h"] < -0.5:
            entry = close
            sl = entry - atr * 1.2
            tp = entry + atr * 2.0
            signals.append({
                "dt": row["dt"].isoformat(),
                "direction": "LONG",
                "entry_px": round(entry, 2),
                "sl_px": round(sl, 2),
                "tp_px": round(tp, 2),
                "signal_type": "mean_reversion_long",
                "confidence": 0.6,
                "atr_14": round(atr, 2),
                "bb_ratio": round(row["bb_width_ratio"], 3),
            })
    
    return signals


# ---------------------------------------------------------------------------
# Paper-trade simulation
# ---------------------------------------------------------------------------

def simulate_strategy(df: pd.DataFrame, signals: List[dict], bankroll: float = 1000.0) -> dict:
    """Simulate hourly scalping on PAXG."""
    total_signals = len(signals)
    if not signals:
        return {
            "days_backtested": len(df),
            "total_signals": 0,
            "trades_simulated": 0,
            "win_rate": 0.0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "final_balance": bankroll,
            "max_drawdown": 0.0,
            "roi_pct": 0.0,
            "trades": [],
        }
    
    trades = []
    current_balance = bankroll
    peak = bankroll
    max_dd = 0.0
    
    for sig in signals:
        # Find signal timestamp in dataframe
        sig_dt = pd.to_datetime(sig["dt"])
        idx = df[df["dt"] >= sig_dt].index
        if len(idx) == 0:
            continue
        entry_idx = idx[0]
        if entry_idx + 1 >= len(df):
            continue
        
        # Entry on next bar open, exit on the bar after that
        entry_row = df.iloc[entry_idx + 1]
        exit_row = df.iloc[min(entry_idx + 2, len(df) - 1)]  # Hold 1-2 bars (1-2 hours)
        
        entry_price = entry_row["open"]
        sl_price = sig["sl_px"]
        tp_price = sig["tp_px"]
        direction = sig["direction"]
        
        # Check exit bar for SL or TP hit
        if direction == "LONG":
            if exit_row["low"] <= sl_price:
                exit_price = sl_price
                result = "SL"
            elif exit_row["high"] >= tp_price:
                exit_price = tp_price
                result = "TP"
            else:
                exit_price = exit_row["close"]
                result = "CLOSE"
            pnl_pct = (exit_price - entry_price) / entry_price
        else:  # SHORT
            if exit_row["high"] >= sl_price:
                exit_price = sl_price
                result = "SL"
            elif exit_row["low"] <= tp_price:
                exit_price = tp_price
                result = "TP"
            else:
                exit_price = exit_row["close"]
                result = "CLOSE"
            pnl_pct = (entry_price - exit_price) / entry_price
        
        # Position sizing: 1.5% risk per trade
        risk_usd = current_balance * 0.015
        sl_dist = abs(entry_price - sl_price) / entry_price
        if sl_dist <= 0.002:
            continue
        size = risk_usd / sl_dist
        pnl_usd = size * pnl_pct
        
        current_balance += pnl_usd
        if current_balance > peak:
            peak = current_balance
        dd = (peak - current_balance) / peak * 100
        if dd > max_dd:
            max_dd = dd
        
        trades.append({
            "entry_dt": sig["dt"],
            "direction": direction,
            "signal_type": sig["signal_type"],
            "entry_px": round(entry_price, 2),
            "exit_px": round(exit_price, 2),
            "pnl_pct": round(pnl_pct * 100, 2),
            "pnl_usd": round(pnl_usd, 2),
            "result": result,
            "balance": round(current_balance, 2),
        })
    
    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    total_pnl = current_balance - bankroll
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    
    report = {
        "days_backtested": len(df) // 24,
        "total_signals": total_signals,
        "trades_simulated": len(trades),
        "win_rate": round(win_rate, 1),
        "wins": len(wins),
        "losses": len(losses),
        "total_pnl": round(total_pnl, 2),
        "final_balance": round(current_balance, 2),
        "max_drawdown": round(max_dd, 2),
        "roi_pct": round((current_balance - bankroll) / bankroll * 100, 2),
        "trades": trades,
    }
    return report


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    
    logger.info("Fetching PAXG 100-day hourly candles from Hyperliquid...")
    df = build_paxg_history(days=100, interval="1h")
    if df.empty:
        logger.error("No data — abort.")
        return None
    
    save_history(df)
    
    logger.info("Computing indicators...")
    df = add_indicators(df)
    
    logger.info("Generating signals...")
    signals = generate_signals(df)
    logger.info(f"Generated {len(signals)} signals")
    
    with open(SIGNALS_FILE, "w") as f:
        json.dump(signals, f, indent=2)
    
    logger.info("Running paper trade simulation...")
    report = simulate_strategy(df, signals)
    
    with open(SIM_FILE, "w") as f:
        json.dump(report, f, indent=2)
    
    logger.info(f"=== Report ===")
    for k, v in report.items():
        if k != "trades":
            logger.info(f"  {k}: {v}")
    
    # Print brief
    print(f"""
🪙 **PAXG Commodity Scalper — 100-Day Paper Trade (Hourly)**
- Hourly candles: {len(df)}
- Signals: {report['total_signals']}, Trades: {report['trades_simulated']}
- Win Rate: {report['win_rate']}%
- ROI: {report['roi_pct']}% (${report['total_pnl']})
- Max Drawdown: {report['max_drawdown']}%
- Final Balance: **${report['final_balance']}**
""")
    
    # Show last few trades
    if report["trades"]:
        print("Last trades:")
        for t in report["trades"][-3:]:
            print(f"  {t['entry_dt']}: {t['direction']} {t['signal_type']} → {t['result']} (${t['pnl_usd']})")
    
    # Signal for today
    latest = df.iloc[-1]
    print(f"\nCurrent PAXG: Close=${latest['close']:.2f}, ATR={latest['atr_14']:.2f}, BB_ratio={latest['bb_width_ratio']:.2f}")
    
    return report


if __name__ == "__main__":
    report = run()
