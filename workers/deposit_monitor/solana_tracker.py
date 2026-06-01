"""
Solana deposit tracker.
Tracks deposits to a user-provided Solana address.
No mnemonic or private keys — address supplied via DESK_SOLANA_ADDRESS env var only.
"""
import os
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Optional, List

DB_PATH = Path(__file__).parent.parent.parent / "data_store" / "deposits.db"


def get_sol_address() -> Optional[str]:
    addr = os.environ.get("DESK_SOLANA_ADDRESS")
    if not addr:
        raise RuntimeError(
            "DESK_SOLANA_ADDRESS not set. "
            "Create a Solana wallet in your own wallet app (Phantom, Solflare, etc.), "
            "then set: export DESK_SOLANA_ADDRESS='<your_address>'"
        )
    return addr


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
    """)
    conn.commit()
    conn.close()


def create_deposit(expected_amount: float = 0.0, token: str = "SOL") -> Dict:
    addr = get_sol_address()
    _ensure_db()
    conn = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cur.execute(
        "INSERT INTO deposits (chain, address, expected_amount, token, status, created_at) VALUES (?,?,?,?,?,?)",
        ("solana", addr, expected_amount, token, "pending", now),
    )
    conn.commit()
    deposit_id = cur.lastrowid
    conn.close()
    return {
        "id": deposit_id,
        "chain": "solana",
        "address": addr,
        "token": token,
        "expected_amount": expected_amount,
        "status": "pending",
        "created_at": now,
    }


def get_deposits(address: Optional[str] = None, limit: int = 50) -> List[Dict]:
    _ensure_db()
    conn = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if address:
        cur.execute("SELECT * FROM deposits WHERE address = ? ORDER BY created_at DESC LIMIT ?", (address, limit))
    else:
        cur.execute("SELECT * FROM deposits ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows
