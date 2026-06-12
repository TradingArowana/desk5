#!/usr/bin/env python3
"""
Fibonacci 1m Scalper — Live Runner
=====================================
Runs every 5 minutes.  Fetches 1m HL candles for top liquid coins,
computes Fibonacci golden-zone entries, writes signals to
`fib_1m_scalp_signals.json` for the aggregator to pick up.

ALL hard guards are inside `fib_1m_scalp.py`; this file just wires
HL API → strategy → signal file → aggregator.
"""

import json, sys, time
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from workers.strategies.fib_1m_scalp import fib_scalp_signal, MIN_COIN_PRICE, MAX_NOTIONAL_USD
from workers.strategies.signal_aggregator import save_to_queue

STATE_DIR     = PROJECT_ROOT / "data_store"
CACHE_DIR     = PROJECT_ROOT / "data_store" / "hl_1m_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

SIGNAL_FILE   = STATE_DIR / "fib_1m_scalp_signals.json"

# ── Which coins to scan (top liquid perps on HL) ──
WATCHLIST = [
    "BTC", "ETH", "SOL", "XRP", "DOGE",
    "ADA", "DOT", "LINK", "AVAX", "MATIC",
    "ATOM", "OP", "ARB", "GMX", "CRV",
    "SUI", "APT", "SEI", "TIA", "STRK",
]


def fetch_hl_candles(coin: str, interval: str = "1m", lookback: int = 30) -> list:
    """Fetch native HL OHLC via info endpoint."""
    import requests
    # HL info endpoint for candles
    url = "https://api.hyperliquid.xyz/info"
    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": int((time.time() - lookback * 60) * 1000), "endTime": int(time.time() * 1000)}
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        # data is list of [timestamp, open, high, low, close, volume]
        candles = []
        for row in data:
            candles.append({
                "time": row[0],
                "open": float(row[1]),
                "high": float(row[2]),
                "low":  float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })
        return candles
    except Exception as e:
        # Fallback: try reading from local prefetcher cache
        cache_file = CACHE_DIR / f"{coin}_1m.json"
        if cache_file.exists():
            try:
                return json.loads(cache_file.read_text())[-lookback:]
            except Exception:
                pass
        return []


def save_signals(signals: list) -> None:
    """Write signals to strategy file AND push to unified queue."""
    existing = []
    if SIGNAL_FILE.exists():
        try:
            existing = json.loads(SIGNAL_FILE.read_text())
        except Exception:
            pass

    # Merge: keep latest per coin+direction
    mp = {}
    for s in existing:
        key = f"{s.get('coin','')}|{s.get('direction','')}"
        mp[key] = s
    for s in signals:
        key = f"{s.get('coin','')}|{s.get('direction','')}"
        mp[key] = s

    # Prune signals older than 15 min
    now = datetime.now(timezone.utc).timestamp()
    fresh = []
    for s in mp.values():
        ts = s.get("timestamp", "")
        if ts:
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                if now - t < 900:
                    fresh.append(s)
            except Exception:
                fresh.append(s)
        else:
            fresh.append(s)

    SIGNAL_FILE.write_text(json.dumps(fresh, indent=2))
    print(f"[{datetime.now(timezone.utc).isoformat()}] Wrote {len(fresh)} fib_1m signals")

    # Push to unified queue for live execution
    if fresh:
        save_to_queue(fresh)
        print(f"  → pushed {len(fresh)} to unified queue")


def main() -> None:
    print(f"\n{'='*60}")
    print(f"Fibonacci 1m Scalper — {datetime.now(timezone.utc).isoformat()}")
    print(f"{'='*60}")

    signals = []
    for coin in WATCHLIST:
        candles = fetch_hl_candles(coin, interval="1m", lookback=30)
        if len(candles) < 20:
            print(f"  {coin}: insufficient candles ({len(candles)})")
            continue

        sig = fib_scalp_signal(candles, coin)
        if sig:
            print(f"  {coin}: {sig['direction']} entry={sig['entry_px']:.4f} SL={sig['sl_px']:.4f} TP={sig['tp_px']:.4f} RR={sig['rr']}")
            signals.append(sig)
        else:
            print(f"  {coin}: no signal")

    if signals:
        save_signals(signals)
    else:
        print("No signals generated this cycle.")

    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
