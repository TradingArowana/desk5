"""
Manual Position Tracker — accepts live entries from user and tracks against HL mark prices.
"""
import json, logging, sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from workers.execution.hl_bridge import get_all_marks

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
LIVE_LEDGER = STATE_DIR / "live_ledger.json"


def _load_ledger() -> List[dict]:
    if LIVE_LEDGER.exists():
        try:
            return json.loads(LIVE_LEDGER.read_text())
        except Exception:
            pass
    return []


def _save_ledger(ledger: List[dict]):
    LIVE_LEDGER.write_text(json.dumps(ledger, indent=2))


def add_position(coin: str, direction: str, entry_px: float, size: float, sl_px: float, tp_px: float, source: str = "manual") -> dict:
    """User confirms a live trade. We track it."""
    ledger = _load_ledger()
    pos = {
        "id": f"{coin}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "coin": coin,
        "direction": direction,
        "entry_px": round(entry_px, 4),
        "size": round(size, 6),
        "sl_px": round(sl_px, 4),
        "tp_px": round(tp_px, 4),
        "source": source,
        "status": "OPEN",
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "closed_at": None,
        "pnl": 0.0,
        "exit_px": None,
        "exit_reason": None,
    }
    ledger.append(pos)
    _save_ledger(ledger)
    logger.info("Live position added: %s %s %.4f @ %.4f", direction, coin, size, entry_px)
    return pos


def update_positions() -> dict:
    """Update all open positions against current mark prices."""
    ledger = _load_ledger()
    marks = get_all_marks()
    open_count = 0
    total_pnl = 0.0
    alerts = []
    
    for pos in ledger:
        if pos["status"] != "OPEN":
            total_pnl += pos.get("pnl", 0)
            continue
        
        coin = pos["coin"]
        mark = marks.get(coin)
        if not mark:
            open_count += 1
            continue
        
        direction = pos["direction"]
        entry = pos["entry_px"]
        size = pos["size"]
        sl = pos["sl_px"]
        tp = pos["tp_px"]
        
        # Calculate unrealized PnL
        if direction == "LONG":
            pnl = (mark - entry) * size
            hit_sl = mark <= sl
            hit_tp = mark >= tp
        else:
            pnl = (entry - mark) * size
            hit_sl = mark >= sl
            hit_tp = mark <= tp
        
        pos["mark_px"] = round(mark, 4)
        pos["unrealized_pnl"] = round(pnl, 4)
        
        if hit_sl or hit_tp:
            pos["status"] = "CLOSED"
            pos["closed_at"] = datetime.now(timezone.utc).isoformat()
            pos["pnl"] = round(pnl, 4)
            pos["exit_px"] = round(mark, 4)
            pos["exit_reason"] = "SL" if hit_sl else "TP"
            total_pnl += pnl
            alerts.append(f"🔴 {coin} {direction} CLOSED @ ${mark:.4f} | PnL: ${pnl:+.2f} | Reason: {pos['exit_reason']}")
            logger.info("Position closed: %s %s pnl=%.4f", coin, direction, pnl)
        else:
            open_count += 1
    
    _save_ledger(ledger)
    
    return {
        "open_count": open_count,
        "total_realized_pnl": round(total_pnl, 2),
        "positions": ledger,
        "alerts": alerts,
        "dt": datetime.now(timezone.utc).isoformat(),
    }


def get_summary() -> dict:
    """Quick summary for dashboard."""
    ledger = _load_ledger()
    open_pos = [p for p in ledger if p["status"] == "OPEN"]
    closed_pos = [p for p in ledger if p["status"] == "CLOSED"]
    realized = sum(p.get("pnl", 0) for p in closed_pos)
    unrealized = sum(p.get("unrealized_pnl", 0) for p in open_pos)
    wins = len([p for p in closed_pos if p.get("pnl", 0) > 0])
    total_closed = len(closed_pos)
    
    return {
        "open_count": len(open_pos),
        "closed_count": len(closed_pos),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(realized + unrealized, 2),
        "win_rate": round(wins / total_closed, 4) if total_closed > 0 else 0,
        "positions": open_pos[-5:],  # Last 5 open
        "dt": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = update_positions()
    print(json.dumps(result, indent=2))
    sys.exit(0)
