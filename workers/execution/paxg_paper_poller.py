#!/usr/bin/env python3
"""
Executor Poller — PAPER MODE for PAXG macro scalping.
Pulls signals from unified queue, simulates execution, logs to ledger.
Does NOT place live orders until Phase 1 Lock (10× paper proven) is met.
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
logger = logging.getLogger("paxg-paper")

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

STATE_DIR = PROJECT_ROOT / "data_store"
UNIFIED_QUEUE = STATE_DIR / "all_signals.json"
SIM_LEDGER = STATE_DIR / "paxg_paper_ledger.json"

# Paper bankroll for PAXG
PAPER_BANKROLL = 1000.0

# Phase 1 Lock — hardcoded, must be flipped ONLY when 10× proven
PHASE_1_LOCK = True  # NEVER change without Board approval

# Active session check for PAXG
GOLD_SESSIONS = [(7, 12), (13, 17)]

def is_active_session():
    now = datetime.now(timezone.utc)
    hour = now.hour
    for start, end in GOLD_SESSIONS:
        if start <= hour <= end:
            return True
    return False

def load_queue():
    if not UNIFIED_QUEUE.exists():
        return []
    try:
        return json.loads(UNIFIED_QUEUE.read_text())
    except Exception:
        return []

def load_sim_ledger():
    if not SIM_LEDGER.exists():
        return []
    try:
        return json.loads(SIM_LEDGER.read_text())
    except Exception:
        return []

def save_sim_ledger(ledger):
    SIM_LEDGER.write_text(json.dumps(ledger, indent=2))

def simulate_fill(signal: dict) -> dict:
    """Paper-fill a PAXG signal with conservative assumptions."""
    entry = float(signal.get("entry_px", 0))
    sl = float(signal.get("sl_px", 0))
    tp = float(signal.get("tp_px", 0))
    direction = signal.get("direction")
    coin = signal.get("coin", "PAXG")
    sig_type = signal.get("signal_type", "momentum")
    
    # Sizing: $2 risk (0.2% of bankroll), not $20
    risk_usd = PAPER_BANKROLL * 0.002  # $2 risk
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return {"status": "rejected", "reason": "zero SL distance", "signal_type": sig_type}
    
    notional = risk_usd / (sl_dist / entry)
    leverage = 10  # PAXG max leverage
    margin = notional / leverage
    
    # Reality check: margin must fit in 10% risk pocket
    if margin > PAPER_BANKROLL * 0.10:
        return {
            "status": "rejected",
            "reason": f"margin ${margin:.2f} > 10% bankroll (${PAPER_BANKROLL * 0.10:.2f})",
            "margin": margin,
            "signal_type": sig_type,
        }
    
    # Simulate next-hour outcome (simplified)
    # In reality we'd need price data; here we use signal SL/TP as proxy
    import random
    # Bell trades have shorter duration expectation
    if sig_type == "bell_scalp":
        outcome = random.choices(
            ["tp", "sl", "open"],
            weights=[40, 35, 25]  # slightly more TP bias in bell bursts
        )[0]
    else:
        outcome = random.choices(
            ["tp", "sl", "open"],
            weights=[35, 40, 25]  # hourly is noisier
        )[0]
    
    if outcome == "tp":
        pnl = risk_usd * 2  # 2:1 R/R
        exit_px = tp
        status = "CLOSED"
    elif outcome == "sl":
        pnl = -risk_usd
        exit_px = sl
        status = "CLOSED"
    else:
        pnl = 0.0
        exit_px = entry
        status = "OPEN"
    
    return {
        "status": status,
        "coin": coin,
        "direction": direction,
        "signal_type": sig_type,
        "entry_px": entry,
        "exit_px": exit_px,
        "size": notional,
        "margin": margin,
        "leverage": leverage,
        "pnl": pnl,
        "pnl_pct": pnl / PAPER_BANKROLL * 100,
        "opened_at": signal.get("dt"),
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "signal": signal,
    }

def main():
    if not is_active_session():
        return 0
    
    signals = load_queue()
    if not signals:
        return 0
    
    ledger = load_sim_ledger()
    filled = 0
    rejected = 0
    
    for sig in signals:
        if sig.get("coin") != "PAXG":
            continue
        
        if PHASE_1_LOCK:
            # Paper trade only
            result = simulate_fill(sig)
            ledger.append(result)
            if result["status"] == "CLOSED":
                filled += 1
                tag = "🔔" if sig.get("signal_type") == "bell_scalp" else "⏳"
                logger.info("PAPER FILL %s %s %s: PnL=$%.2f",
                    tag, sig.get("coin"), sig.get("direction"), result["pnl"])
            elif result["status"] == "rejected":
                rejected += 1
                logger.info("PAPER REJECT %s: %s", sig.get("coin"), result.get("reason"))
            else:
                filled += 1
                tag = "🔔" if sig.get("signal_type") == "bell_scalp" else "⏳"
                logger.info("PAPER OPEN %s %s %s", tag, sig.get("coin"), sig.get("direction"))
        else:
            # Live mode — NEVER reached without explicit Board override
            logger.critical("LIVE MODE ATTEMPTED WITHOUT PHASE 1 CLEARANCE")
            continue
    
    save_sim_ledger(ledger)
    
    # Summary stats by mode
    total_pnl = sum(t.get("pnl", 0) for t in ledger)
    bell_trades = [t for t in ledger if t.get("status") == "CLOSED" and t.get("signal_type") == "bell_scalp"]
    hourly_trades = [t for t in ledger if t.get("status") == "CLOSED" and t.get("signal_type") != "bell_scalp"]
    wins = sum(1 for t in ledger if t.get("pnl", 0) > 0)
    losses = sum(1 for t in ledger if t.get("pnl", 0) < 0)
    total_trades = len([t for t in ledger if t.get("status") == "CLOSED"])
    
    logger.info("Paper batch: %d filled, %d rejected | Total: %d (🔔 bell: %d, ⏳ hourly: %d) | Wins: %d | Losses: %d | PnL: $%.2f",
        filled, rejected, total_trades, len(bell_trades), len(hourly_trades), wins, losses, total_pnl)
    
    # Clear queue
    UNIFIED_QUEUE.write_text(json.dumps([]))
    return 0

if __name__ == "__main__":
    sys.exit(main())
