#!/usr/bin/env python3
"""
Executor Poller — pulls from unified signal queue and sends to Hyperliquid.
Runs every minute. Only executes during active gold trading hours for PAXG.
"""
import os
import sys
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)8s | %(name)18s | %(message)s",
)
logger = logging.getLogger("exec-poller")

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

STATE_DIR = PROJECT_ROOT / "data_store"
UNIFIED_QUEUE = STATE_DIR / "all_signals.json"

from workers.execution.hl_executor import place_order

# Active session check for PAXG
GOLD_SESSIONS = [(7, 12), (13, 17)]

def is_active_session():
    """All perp markets active 24/7 — removed gold session gate."""
    return True

def load_queue():
    if not UNIFIED_QUEUE.exists():
        return []
    try:
        return json.loads(UNIFIED_QUEUE.read_text())
    except Exception:
        return []

def main():
    # Only run during active hours
    if not is_active_session():
        logger.debug("Outside gold trading hours, skipping execution")
        return 0

    signals = load_queue()
    if not signals:
        return 0

    logger.info("Processing %d signals", len(signals))

    executed = 0
    rejected = 0

    for sig in signals:
        coin = sig.get("coin", "")
        
        # Execute ALL high-confidence signals — removed PAXG-only gate
        try:
            result = place_order(sig)
            if result.get("status") in ("filled", "FILLED"):
                executed += 1
                logger.info("EXECUTED %s %s @ %s", coin, sig.get("direction"), sig.get("entry_px"))
            elif result.get("status") == "rejected":
                rejected += 1
                logger.info("REJECTED %s: %s", coin, result.get("reason", "unknown"))
            else:
                rejected += 1
                logger.info("UNKNOWN STATUS %s: %s", coin, result.get("status"))
        except Exception as exc:
            logger.error("Execution failed for %s: %s", coin, exc)

    # Clear queue
    UNIFIED_QUEUE.write_text(json.dumps([]))

    logger.info("Batch complete: %d executed, %d rejected", executed, rejected)
    return 0


if __name__ == "__main__":
    sys.exit(main())
