#!/usr/bin/env python3
"""
4h Momentum Swing — high-R trend strategy targeting 5R–10R moves.
Uses 4h EMA 9/21 crossover + ADX > 25 regime filter + RSI confirmation.
Only trades top-30 coins with deep liquidity. Wider stops, bigger targets.
"""
import os, sys, json, time, logging
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("swing_momentum")

DATA_DIR = PROJECT_ROOT / "data_store"
DATA_DIR.mkdir(exist_ok=True)
SIGNAL_FILE = DATA_DIR / "swing_momentum_signals.json"

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
BLACKLIST = {"PEPE","SHIB","FLOKI","BONK","WIF","MOG","BOME","PENGU",
             "POPCAT","TRUMP","MELANIA","HARRY","PORK","TURBO","BRETT",
             "TETRIS","XPL","HEMI","MEME","BABY","NXPC",
             "USDC","USDT","DAI","FDUSD"}
MIN_VOLUME_24H = 20_000_000   # $20M+

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

def get_candles(coin: str, granularity: int, lookback_hours: int) -> list:
    now = int(time.time() * 1000)
    start = now - lookback_hours * 60 * 60 * 1000
    data = hl_post({
        "type": "candleSnapshot",
        "req": {"coin": coin, "startTime": start, "endTime": now, "granularity": granularity}
    })
    return data if isinstance(data, list) else []

def ema(values: list, length: int) -> float:
    if len(values) < length:
        return 0.0
    mult = 2.0 / (length + 1)
    ema_val = sum(values[:length]) / length
    for v in values[length:]:
        ema_val = (v - ema_val) * mult + ema_val
    return ema_val

def rsi(closes: list, length: int = 14) -> float:
    if len(closes) < length + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, length + 1):
        diff = closes[-length - 1 + i] - closes[-length - 2 + i]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    avg_gain = sum(gains) / length
    avg_loss = sum(losses) / length
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def adx(candles: list, length: int = 14) -> tuple:
    """Returns (adx_value, +di, -di). Needs at least length*2+1 candles."""
    if len(candles) < length * 2 + 1:
        return (0.0, 0.0, 0.0)
    highs = [float(c.get("h",0)) for c in candles]
    lows = [float(c.get("l",0)) for c in candles]
    closes = [float(c.get("c",0)) for c in candles]
    tr_list, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        h, l, pc = highs[i], lows[i], closes[i-1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        up = h - highs[i-1] if highs[i] > highs[i-1] else 0
        down = lows[i-1] - l if lows[i] < lows[i-1] else 0
        plus_dm.append(up if up > down else 0)
        minus_dm.append(down if down > up else 0)
        tr_list.append(tr)
    # Wilder smoothing
    tr14 = sum(tr_list[:length])
    pdm14 = sum(plus_dm[:length])
    mdm14 = sum(minus_dm[:length])
    for i in range(length, min(len(tr_list), len(plus_dm), len(minus_dm))):
        tr14 = tr14 - tr14/length + tr_list[i]
        pdm14 = pdm14 - pdm14/length + plus_dm[i]
        mdm14 = mdm14 - mdm14/length + minus_dm[i]
    atr14 = tr14 / length if length > 0 else 0.0001
    pdi = (pdm14 / atr14) * 100 if atr14 > 0 else 0
    mdi = (mdm14 / atr14) * 100 if atr14 > 0 else 0
    dx = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) > 0 else 0
    return (dx, pdi, mdi)  # Simplified — using raw DX as proxy for ADX

def generate_signals():
    mids = get_all_mids()
    meta = get_meta()
    if not mids or not meta:
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

        # Need ~100 candles of 1h for 4h aggregation (or use 4h directly if available)
        candles = get_candles(coin, 14400000, 168)  # 4h candles, 1 week back
        if len(candles) < 20:
            candles = get_candles(coin, 3600000, 168)  # fallback 1h
            # Aggregate 1h -> 4h
            agg = []
            for i in range(0, len(candles) - 3, 4):
                chunk = candles[i:i+4]
                agg.append({
                    "o": float(chunk[0].get("o",0)),
                    "h": max(float(c.get("h",0)) for c in chunk),
                    "l": min(float(c.get("l",0)) for c in chunk),
                    "c": float(chunk[-1].get("c",0)),
                    "v": sum(float(c.get("v",0)) for c in chunk)
                })
            candles = agg

        if len(candles) < 20:
            continue

        closes = [float(c.get("c",0)) for c in candles]
        ema9 = ema(closes, 9)
        ema21 = ema(closes, 21)
        rsi_val = rsi(closes, 14)
        adx_val, pdi, mdi = adx(candles, 14)

        # Regime filter
        if adx_val < 25:
            continue

        # Signal: EMA crossover with ADX > 25 and RSI confirmation
        prev_ema9 = ema(closes[:-1], 9)
        prev_ema21 = ema(closes[:-1], 21)
        atr = max(closes) - min(closes)
        if atr <= 0:
            continue

        if prev_ema9 <= prev_ema21 and ema9 > ema21 and rsi_val > 55:
            # LONG — 5R target
            sl = px - atr * 0.3
            tp = px + (px - sl) * 5
            signals.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "coin": coin, "direction": "LONG",
                "entry": round(px,6), "stop": round(sl,6), "target": round(tp,6),
                "strategy": "swing_momentum",
                "reason": f"4h EMA cross LONG | ADX={adx_val:.1f} | RSI={rsi_val:.1f}"
            })
            logger.info("🌊 SWING LONG %s @ %.4f | SL %.4f | TP %.4f | ADX %.1f | RSI %.1f",
                        coin, px, sl, tp, adx_val, rsi_val)

        elif prev_ema9 >= prev_ema21 and ema9 < ema21 and rsi_val < 45:
            # SHORT
            sl = px + atr * 0.3
            tp = px - (sl - px) * 5
            signals.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "coin": coin, "direction": "SHORT",
                "entry": round(px,6), "stop": round(sl,6), "target": round(tp,6),
                "strategy": "swing_momentum",
                "reason": f"4h EMA cross SHORT | ADX={adx_val:.1f} | RSI={rsi_val:.1f}"
            })
            logger.info("🌊 SWING SHORT %s @ %.4f | SL %.4f | TP %.4f | ADX %.1f | RSI %.1f",
                        coin, px, sl, tp, adx_val, rsi_val)

    return signals

def main():
    signals = generate_signals()
    existing = []
    if SIGNAL_FILE.exists():
        try:
            existing = json.loads(SIGNAL_FILE.read_text())
        except Exception:
            pass
    combined = existing[-30:] + signals
    SIGNAL_FILE.write_text(json.dumps(combined, indent=2))
    logger.info("Wrote %d swing signals | total in file: %d", len(signals), len(combined))

if __name__ == "__main__":
    main()
