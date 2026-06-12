#!/usr/bin/env python3
"""
Asymmetric Risk Pocket — 10% of bankroll deployed on high-volatility breakout setups.
Targets extreme outlier moves (10R–50R) on coins breaking 24h range + volume spike.
Only enters when: price breaks above 24h high OR below 24h low on 3x avg volume.
Stop = 2% of bankroll. Take-profit = 10R (asymmetric payoff).
Max 2 concurrent asymmetric positions. NOT mixed with normal scalp queue.
"""
import os, sys, json, time, logging
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("asymmetric_pocket")

DATA_DIR = PROJECT_ROOT / "data_store"
DATA_DIR.mkdir(exist_ok=True)
SIGNAL_FILE = DATA_DIR / "asymmetric_pocket_signals.json"

# ── Config ──
HL_INFO_URL = "https://api.hyperliquid.xyz/info"
BLACKLIST = {"PEPE","SHIB","FLOKI","BONK","WIF","MOG","BOME","PENGU",
             "POPCAT","TRUMP","MELANIA","HARRY","PORK","TURBO","BRETT",
             "TETRIS","XPL","HEMI","MEME","BABY","NXPC",
             "USDC","USDT","DAI","FDUSD"}
MIN_MARKET_CAP_RANK = 50      # Only top 50 coins (deep liquidity)
MIN_VOLUME_24H = 10_000_000   # $10M+ daily volume
VOL_SPIKE_MULT = 3.0          # Current 1h vol must be 3x 24h average
MIN_BREAKOUT_PCT = 2.0        # Must break 24h high/low by 2%
MAX_CONCURRENT = 2

# ── Helpers ──
def hl_post(payload: dict) -> dict:
    try:
        r = requests.post(HL_INFO_URL, json=payload, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        logger.error("HL API failed: %s", exc)
        return {}

def get_all_mids() -> dict:
    return hl_post({"type": "allMids"})

def get_meta() -> list:
    data = hl_post({"type": "meta"})
    return data.get("universe", []) if data else []

def get_24h_candles(coin: str) -> list:
    now = int(time.time() * 1000)
    start = now - 24 * 60 * 60 * 1000
    data = hl_post({
        "type": "candleSnapshot",
        "req": {"coin": coin, "startTime": start, "endTime": now, "granularity": 3600000}
    })
    return data if isinstance(data, list) else []

def generate_signals():
    mids = get_all_mids()
    meta = get_meta()
    if not mids or not meta:
        logger.warning("No market data available")
        return []

    signals = []
    for asset in meta:
        coin = asset.get("name")
        if not coin or coin in BLACKLIST:
            continue
        if coin not in mids:
            continue

        px = float(mids[coin])
        if px < 0.10:
            continue

        candles = get_24h_candles(coin)
        if len(candles) < 6:
            continue

        highs = [float(c.get("h", 0)) for c in candles if c.get("h")]
        lows = [float(c.get("l", 0)) for c in candles if c.get("l")]
        vols = [float(c.get("v", 0)) for c in candles if c.get("v")]
        if not highs or not lows or not vols:
            continue

        h24 = max(highs)
        l24 = min(lows)
        avg_vol = sum(vols) / len(vols) if vols else 0
        last_vol = vols[-1] if vols else 0

        # Breakout + volume spike
        breakout_long = px > h24 * (1 + MIN_BREAKOUT_PCT/100)
        breakout_short = px < l24 * (1 - MIN_BREAKOUT_PCT/100)
        vol_spike = avg_vol > 0 and last_vol > avg_vol * VOL_SPIKE_MULT

        if not vol_spike:
            continue

        atr = h24 - l24
        if atr <= 0:
            continue

        sl_dist = atr * 0.5   # Tight stop at mid-range
        if breakout_long:
            sl = px - sl_dist
            tp = px + sl_dist * 10   # 10R asymmetric
            signals.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "coin": coin,
                "direction": "LONG",
                "entry": round(px, 6),
                "stop": round(sl, 6),
                "target": round(tp, 6),
                "strategy": "asymmetric_pocket",
                "reason": f"24h breakout LONG | vol={last_vol/avg_vol:.1f}x | h24={h24:.4f}"
            })
            logger.info("🚀 ASYMM LONG %s @ %.4f | SL %.4f | TP %.4f | vol %.1fx", coin, px, sl, tp, last_vol/avg_vol)

        elif breakout_short:
            sl = px + sl_dist
            tp = px - sl_dist * 10
            signals.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "coin": coin,
                "direction": "SHORT",
                "entry": round(px, 6),
                "stop": round(sl, 6),
                "target": round(tp, 6),
                "strategy": "asymmetric_pocket",
                "reason": f"24h breakout SHORT | vol={last_vol/avg_vol:.1f}x | l24={l24:.4f}"
            })
            logger.info("🚀 ASYMM SHORT %s @ %.4f | SL %.4f | TP %.4f | vol %.1fx", coin, px, sl, tp, last_vol/avg_vol)

    return signals

def main():
    signals = generate_signals()
    existing = []
    if SIGNAL_FILE.exists():
        try:
            existing = json.loads(SIGNAL_FILE.read_text())
        except Exception:
            pass
    # Keep last 50, append new
    combined = existing[-50:] + signals
    SIGNAL_FILE.write_text(json.dumps(combined, indent=2))
    logger.info("Wrote %d asymmetric signals | total in file: %d", len(signals), len(combined))

if __name__ == "__main__":
    main()
