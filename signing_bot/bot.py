"""Desk5 Local Signing Bot — polls signal feed, validates, signs, submits.
Private key NEVER leaves this machine.
"""
import os
import sys
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from config import (
    DESK5_SERVER_URL,
    AUTO_EXECUTE,
    POLL_INTERVAL_SEC,
)
from state import load_state, save_state, reset_daily, already_acted, record_act, add_position
from safety import check_safety, compute_size
from hyperliquid_signer import submit_order, get_positions, get_account_value

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("signing_bot")


def poll_signals(since: str = "") -> list:
    """Fetch signals from Desk5 server."""
    url = f"{DESK5_SERVER_URL}/api/live/signal-feed"
    if since:
        url += f"?since={since}"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data.get("signals", [])
    except Exception as exc:
        logger.error("Signal poll failed: %s", exc)
        return []


def process_signal(signal: dict, bankroll: float, open_count: int) -> dict:
    """Validate + optionally submit a single signal."""
    coin = signal.get("coin")
    direction = signal.get("direction")
    if not coin or not direction:
        return {"status": "skipped", "reason": "Missing coin/direction"}

    # Dedup
    state = load_state()
    if already_acted(state, coin, direction, hours=24):
        return {"status": "skipped", "reason": "Already acted on this setup <24h ago"}

    # Safety circuits
    allowed, reason = check_safety(bankroll, open_count)
    if not allowed:
        return {"status": "halted", "reason": reason}

    # Size calculation
    size = compute_size(signal, bankroll)
    if size <= 0:
        return {"status": "skipped", "reason": "Computed size <= 0"}

    if not AUTO_EXECUTE:
        return {
            "status": "pending_manual",
            "coin": coin,
            "direction": direction,
            "size": size,
            "entry": signal.get("entry_px"),
            "sl": signal.get("sl_px"),
            "tp": signal.get("tp_px"),
        }

    # Submit
    result = submit_order(signal, size)
    if result.get("status") == "filled":
        record_act(state, coin, direction)
        add_position(state, {
            "coin": coin,
            "direction": direction,
            "size": result.get("size"),
            "entry_px": result.get("entry"),
            "sl_px": result.get("sl"),
            "tp_px": result.get("tp"),
            "status": "OPEN",
            "opened_at": datetime.now(timezone.utc).isoformat(),
        })
        logger.info("✅ FILLED %s %s @ %s size=%s", coin, direction, result.get("entry"), result.get("size"))
    else:
        logger.warning("❌ REJECTED %s %s — %s", coin, direction, result.get("reason"))

    return result


def main_loop():
    logger.info("=" * 50)
    logger.info("Desk5 Local Signing Bot started")
    logger.info("Server: %s", DESK5_SERVER_URL)
    logger.info("Auto-execute: %s", AUTO_EXECUTE)
    logger.info("=" * 50)

    state = load_state()
    reset_daily(state)
    last_since = ""

    while True:
        try:
            # Refresh account info
            acct = get_account_value()
            bankroll = acct.get("total", 0)
            positions = get_positions()
            open_count = len(positions)

            logger.info("Bankroll: $%.2f | Open positions: %d", bankroll, open_count)

            # Poll signals
            signals = poll_signals(since=last_since)
            if signals:
                last_since = max(
                    (s.get("dt") or s.get("opened_at") or "")
                    for s in signals
                )
                logger.info("Received %d signal(s)", len(signals))

                for sig in signals:
                    result = process_signal(sig, bankroll, open_count)
                    if result.get("status") in ("filled", "rejected", "error"):
                        open_count += 1 if result.get("status") == "filled" else 0
            else:
                logger.info("No new signals.")

            # Sleep until next poll
            logger.info("Sleeping %d seconds...", POLL_INTERVAL_SEC)
            time.sleep(POLL_INTERVAL_SEC)

        except KeyboardInterrupt:
            logger.info("Shutdown requested.")
            break
        except Exception as exc:
            logger.exception("Main loop error: %s", exc)
            time.sleep(10)


if __name__ == "__main__":
    main_loop()
