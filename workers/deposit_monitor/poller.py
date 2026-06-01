#!/usr/bin/env python3
"""
Deposit monitor cron — checks Solana (and EVM) addresses for incoming transfers.
Runs continuously in background via cronjob.
"""
import os
import json
import sqlite3
import sys
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

# Load .env from repo root before anything else
from dotenv import load_dotenv
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

import requests

# ---------------------------------------------------------------------------
# Logging setup: normal activity -> file, errors only -> stdout
# ---------------------------------------------------------------------------
logger = logging.getLogger("deposit_poller")
logger.setLevel(logging.DEBUG)
logger.propagate = False

# File handler logs everything
file_handler = logging.FileHandler("/tmp/deposit_poller.log")
file_handler.setLevel(logging.DEBUG)
file_formatter = logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)

# Stdout handler logs only errors
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.ERROR)
stdout_formatter = logging.Formatter("%(levelname)s: %(message)s")
stdout_handler.setFormatter(stdout_formatter)
logger.addHandler(stdout_handler)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).parent.parent.parent / "data_store" / "deposits.db"
POLL_INTERVAL = int(os.environ.get("DESK_DEPOSIT_POLL_INTERVAL", "60"))

# ---------------------------------------------------------------------------
# Solana polling (placeholder — full implementation uses Helius/QuickNode)
# ---------------------------------------------------------------------------
def poll_solana(address: str) -> list:
    """Poll Solana for recent deposits to address.
    Returns list of dicts: {tx_hash, from_addr, amount, token, confirmed_at}."""
    # In production: use Helius API or Solana JSON-RPC
    helius_key = os.environ.get("HELIUS_API_KEY")
    if not helius_key:
        return []
    try:
        url = f"https://api.helius.xyz/v0/addresses/{address}/transactions?api-key={helius_key}&limit=5"
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        txs = r.json()
        deposits = []
        for tx in txs:
            if tx.get("type") == "TRANSFER" and tx.get("nativeBalanceChange", 0) > 0:
                deposits.append({
                    "tx_hash": tx.get("signature"),
                    "from_addr": tx.get("sourceAddress"),
                    "amount": tx.get("nativeBalanceChange") / 1e9,  # SOL
                    "token": "SOL",
                    "confirmed_at": datetime.now(timezone.utc).isoformat(),
                })
        return deposits
    except Exception:
        logger.error("Helius API call failed", exc_info=True)
        return []

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _ensure_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS deposits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chain TEXT NOT NULL,
        address TEXT NOT NULL,
        expected_amount REAL,
        token TEXT DEFAULT 'SOL',
        status TEXT DEFAULT 'pending',
        tx_hash TEXT,
        from_addr TEXT,
        amount REAL,
        created_at TEXT,
        confirmed_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_deposits_addr ON deposits(address);
    CREATE TABLE IF NOT EXISTS deposit_state (
        chain TEXT PRIMARY KEY,
        last_poll TEXT,
        last_tx TEXT
    );
    """)
    conn.commit()
    conn.close()

def update_deposit_state(chain: str, last_poll: str, last_tx: str):
    _ensure_db()
    conn = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.execute(
        "INSERT OR REPLACE INTO deposit_state (chain, last_poll, last_tx) VALUES (?, ?, ?)",
        (chain, last_poll, last_tx),
    )
    conn.commit()
    conn.close()

def record_deposit(chain: str, address: str, tx_hash: str, from_addr: str, amount: float, token: str):
    _ensure_db()
    conn = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO deposits (chain, address, tx_hash, from_addr, amount, token, status, confirmed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (chain, address, tx_hash, from_addr, amount, token, "confirmed", now),
    )
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    sol_addr = os.environ.get("DESK_SOLANA_ADDRESS")
    if not sol_addr:
        logger.info("DESK_SOLANA_ADDRESS not set. Sleeping until configured.")
    else:
        logger.info("Tracking Solana deposits to %s", sol_addr)

    _ensure_db()
    RUN_ONCE = os.environ.get("DESK_RUN_ONCE", "0") == "1"

    last_tx = ""
    while True:
        try:
            if sol_addr:
                deposits = poll_solana(sol_addr)
                for dep in deposits:
                    if dep["tx_hash"] != last_tx:
                        record_deposit(
                            "solana", sol_addr, dep["tx_hash"],
                            dep["from_addr"], dep["amount"], dep["token"]
                        )
                        logger.info(
                            "Confirmed deposit: %s %s from %s... tx=%s...",
                            dep["amount"],
                            dep["token"],
                            dep["from_addr"][:8],
                            dep["tx_hash"][:16],
                        )
                        last_tx = dep["tx_hash"]
                update_deposit_state("solana", datetime.now(timezone.utc).isoformat(), last_tx)

            if RUN_ONCE:
                break
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            break
        except Exception:
            if RUN_ONCE:
                raise
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
