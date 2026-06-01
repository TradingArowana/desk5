#!/usr/bin/env python3
"""
Gold Scalp autonomous signal runner for Desk5.
Runs every 5 minutes during active session hours (London 7-12 UTC, NY 13-17 UTC).
Generates momentum-based scalping signals for PAXG.
"""
import os
import sys
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workers.strategies.gold_scalp import generate_signals

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)8s | %(name)12s | %(message)s",
)
logger = logging.getLogger("gold_runner")

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
SIGNALS_FILE = STATE_DIR / "gold_scalp_signals.json"
RUN_LOG = STATE_DIR / "gold_runner.log"

# Only run during active gold sessions
GOLD_SESSIONS = [(7, 12), (13, 17)]

def is_active_session(dt=None):
    if dt is None:
        dt = datetime.now(timezone.utc)
    for start, end in GOLD_SESSIONS:
        if start <= dt.hour <= end:
            return True
    return False

def load_watchlist():
    wl_path = STATE_DIR / "watchlist.json"
    if wl_path.exists():
        try:
            return json.loads(wl_path.read_text())
        except Exception:
            pass
    return [{"symbol": "PAXG", "perp": True}]

def main():
    now = datetime.now(timezone.utc)
    
    if not is_active_session(now):
        # Silent - no action outside session hours
        return 0

    try:
        watchlist = load_watchlist()
        signals = generate_signals(watchlist)
        
        if signals:
            for s in signals:
                line = f"[{now.isoformat()}] GOLD SIGNAL: {s.coin} {s.direction} @ {s.entry_px} SL={s.sl_px} TP={s.tp_px} | {s.rationale}"
                logger.info(line)
                # Append to run log
                with open(RUN_LOG, "a") as f:
                    f.write(line + "\n")
        else:
            logger.debug("No gold signal this cycle")
            
        return 0
    except Exception as exc:
        logger.error("Gold runner error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
