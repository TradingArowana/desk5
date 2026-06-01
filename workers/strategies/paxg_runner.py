#!/usr/bin/env python3
"""
Unified PAXG Strategy Runner for Desk5.

During bell windows (market opens/closes): runs ultra-fast 1-minute scalper.
During regular gold session hours: runs hourly momentum scalper.
Outside hours: silent.

Schedule: every 1 minute via cron during: London (7-12 UTC) & NY-overlap (13-17 UTC).
Bell windows are sub-windows inside those sessions.
"""
import os
import sys
import json
import time
import logging
from datetime import datetime, timezone, time as dt_time
from pathlib import Path

# Setup paths
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)8s | %(name)18s | %(message)s",
)
logger = logging.getLogger("paxg-runner")

STATE_DIR = PROJECT_ROOT / "data_store"

# Import both strategies
from workers.strategies.gold_scalp import generate_signals as gen_hourly
from workers.strategies.bell_scalp import generate_bell_signals, is_bell_window
from workers.strategies.signal_aggregator import save_to_queue

# ── Hourly session bounds (UTC) ──
GOLD_SESSIONS = [(7, 12), (13, 17)]


def is_gold_session(now=None):
    if now is None:
        now = datetime.now(timezone.utc)
    hour = now.hour
    for start, end in GOLD_SESSIONS:
        if start <= hour <= end:
            return True
    return False


def load_watchlist():
    scanner = STATE_DIR / "scanner_state.json"
    if scanner.exists():
        try:
            data = json.loads(scanner.read_text())
            return data.get("watchlist", [])
        except Exception:
            pass
    return [{"symbol": "PAXG", "perp": True}]


def main():
    now = datetime.now(timezone.utc)

    # ── Gate: only run during gold sessions ──
    if not is_gold_session(now):
        logger.debug("Outside gold trading hours (%02d:%02d UTC), skipping", now.hour, now.minute)
        return 0

    try:
        watchlist = load_watchlist()
        paxg_wl = [w for w in watchlist if w.get("symbol", "").upper() == "PAXG"]
        if not paxg_wl:
            logger.info("PAXG not in watchlist, skipping")
            return 0

        # ── Decision: bell window → fast 1m scalper ──
        if is_bell_window(now):
            logger.info("🛎️  BELL WINDOW active — switching to 1-minute mode (%02d:%02d UTC)", now.hour, now.minute)
            signals = generate_bell_signals(paxg_wl)
            mode = "BELL"
        else:
            logger.debug("Regular gold session — hourly momentum mode (%02d:%02d UTC)", now.hour, now.minute)
            signals = gen_hourly(paxg_wl)
            mode = "HOUR"

        if signals:
            # Convert to dicts
            sig_dicts = []
            for s in signals:
                sig_dict = getattr(s, '__dict__', s)
                sig_dict['_generated_by'] = 'bell_scalp' if mode == "BELL" else 'gold_scalp'
                sig_dicts.append(sig_dict)

            save_to_queue(sig_dicts)

            for s in sig_dicts:
                logger.info("PAXG %s [%s]: %s %s @ %.2f SL=%.2f TP=%.2f | conf=%d | %s",
                    s.get('signal_type', 'momentum'),
                    mode,
                    s.get('coin'), s.get('direction'), s.get('entry_px'),
                    s.get('sl_px'), s.get('tp_px'),
                    s.get('confidence', 50),
                    s.get('rationale', '')[:80])
        else:
            logger.debug("No PAXG signal (%s mode)", mode)

        return 0

    except Exception as exc:
        logger.error("PAXG runner error: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
