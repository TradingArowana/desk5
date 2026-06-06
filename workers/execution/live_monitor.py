"""
Live Position Monitor — tracks real HL positions and auto-halts on drawdown.
"""
import json, logging, math, sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any
from workers.execution.hl_bridge import get_positions, get_all_marks, get_account_value

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
MONITOR_STATE = STATE_DIR / "live_monitor_state.json"

MAX_DRAWDOWN_PCT = 20.0
MAX_DAILY_LOSS_USD = 50.0
START_BANKROLL = 1000.0


def _load_state() -> dict:
    if MONITOR_STATE.exists():
        try:
            return json.loads(MONITOR_STATE.read_text())
        except Exception:
            pass
    return {
        "start_bankroll": START_BANKROLL,
        "peak_bankroll": START_BANKROLL,
        "current_bankroll": START_BANKROLL,
        "daily_loss": 0.0,
        "today": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "halted": False,
        "halt_reason": None,
        "last_alert": None,
    }


def _save_state(state: dict):
    MONITOR_STATE.write_text(json.dumps(state, indent=2))


def check_positions(address: str = "") -> dict:
    state = _load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    if state.get("today") != today:
        state["daily_loss"] = 0.0
        state["today"] = today
        state["halted"] = False
        state["halt_reason"] = None
    
    positions = get_positions(address)
    marks = get_all_marks()
    account = get_account_value(address)
    total_balance = account["total"]  # unified perp + spot
    
    total_unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)
    realized_estimate = total_balance - START_BANKROLL - total_unrealized
    
    # Update current bankroll estimate from unified account value
    state["current_bankroll"] = total_balance
    if state["current_bankroll"] > state["peak_bankroll"]:
        state["peak_bankroll"] = state["current_bankroll"]
    
    # Check drawdown
    peak = state["peak_bankroll"]
    current = state["current_bankroll"]
    dd_pct = (peak - current) / peak * 100 if peak > 0 else 0
    
    alerts = []
    
    if dd_pct >= MAX_DRAWDOWN_PCT and not state.get("halted"):
        state["halted"] = True
        state["halt_reason"] = f"MAX DRAWDOWN {dd_pct:.1f}% REACHED"
        alerts.append(f"🛑 HALT: {state['halt_reason']}\nClose ALL positions immediately. Bankroll: ${current:.2f}")
    
    if state["daily_loss"] >= MAX_DAILY_LOSS_USD and not state.get("halted"):
        state["halted"] = True
        state["halt_reason"] = f"DAILY LOSS CAP ${state['daily_loss']:.2f} REACHED"
        alerts.append(f"🛑 HALT: {state['halt_reason']}\nStop trading for today.")
    
    _save_state(state)
    
    return {
        "positions": positions,
        "position_count": len(positions),
        "total_unrealized": round(total_unrealized, 2),
        "current_bankroll": round(current, 2),
        "peak_bankroll": round(peak, 2),
        "drawdown_pct": round(dd_pct, 2),
        "halted": state["halted"],
        "halt_reason": state["halt_reason"],
        "daily_loss": round(state["daily_loss"], 2),
        "alerts": alerts,
        "perp_balance": account["perp"],
        "spot_balance": account["spot"],
        "unified_total": account["total"],
        "dt": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = check_positions()
    print(json.dumps(result, indent=2))
    sys.exit(0)
