"""
Shadow Execution Engine — paper trades with real HL mark-price fills.
Phase-1: uses real market prices but never sends live orders.
"""
import os, json, logging, math
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from workers.execution.hl_bridge import get_all_marks

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
PAPER_LOG = STATE_DIR / "paper_log.jsonl"
SHADOW_STATE = STATE_DIR / "shadow_state.json"

# Hardcoded safety limits
MAX_DRAWDOWN_PCT = 20.0
MAX_DAILY_LOSS_USD = 50.0
MAX_POSITIONS = 3
MAX_LEVERAGE = 2.0


def _load_state() -> dict:
    if SHADOW_STATE.exists():
        try:
            return json.loads(SHADOW_STATE.read_text())
        except Exception:
            pass
    return {
        "start_bankroll": 1000.0,
        "current_bankroll": 1000.0,
        "peak_bankroll": 1000.0,
        "open_positions": [],
        "daily_loss_today": 0.0,
        "today_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "halted_until": None,
        "total_trades": 0,
        "total_wins": 0,
    }


def _save_state(state: dict):
    SHADOW_STATE.write_text(json.dumps(state, indent=2))


def _check_circuits(state: dict, proposed_risk_usd: float) -> tuple[bool, str]:
    """Return (allowed, reason)."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if state.get("today_date") != today:
        state["daily_loss_today"] = 0.0
        state["today_date"] = today
    halted = state.get("halted_until")
    if halted and datetime.fromisoformat(halted) > now:
        return False, f"Trading halted until {halted}"
    # Drawdown
    peak = state.get("peak_bankroll", state["start_bankroll"])
    current = state.get("current_bankroll", peak)
    dd = (peak - current) / peak * 100 if peak > 0 else 0
    if dd >= MAX_DRAWDOWN_PCT:
        state["halted_until"] = (now.replace(hour=23, minute=59)).isoformat()
        return False, f"Max drawdown {dd:.1f}% reached. Halted 24h."
    # Daily loss
    if state["daily_loss_today"] + proposed_risk_usd > MAX_DAILY_LOSS_USD:
        return False, f"Daily loss cap ${MAX_DAILY_LOSS_USD} would be exceeded"
    # Position count
    if len(state.get("open_positions", [])) >= MAX_POSITIONS:
        return False, f"Max {MAX_POSITIONS} positions open"
    return True, "OK"


def execute_shadow(signal: dict, marks: Dict[str, float]) -> Optional[dict]:
    """
    Execute a paper trade using real mark price.
    signal: {coin, direction, entry_px, sl_px, tp_px, ...}
    Returns filled trade dict or None if blocked.
    """
    state = _load_state()
    coin = signal.get("coin")
    direction = signal.get("direction", "LONG")
    entry = signal.get("entry_px")
    sl = signal.get("sl_px")
    tp = signal.get("tp_px")
    if not coin or entry is None or sl is None or tp is None:
        logger.warning("Invalid signal: %s", signal)
        return None
    mark = marks.get(coin)
    if not mark:
        logger.warning("No mark price for %s", coin)
        return None
    # Risk 1.5% per trade
    risk_usd = state["current_bankroll"] * 0.015
    # Size = risk / |entry - sl|
    sl_dist = abs(entry - sl)
    if sl_dist == 0:
        return None
    size = risk_usd / sl_dist
    # Use actual mark price as fill (more honest than signal entry)
    fill_px = mark
    allowed, reason = _check_circuits(state, risk_usd)
    if not allowed:
        logger.info("Signal blocked: %s — %s", coin, reason)
        return None
    trade = {
        "id": f"{coin}_{now.isoformat()}"[:40],
        "coin": coin,
        "direction": direction,
        "size": round(size, 6),
        "entry_px": round(fill_px, 4),
        "target_sl": round(sl, 4),
        "target_tp": round(tp, 4),
        "risk_usd": round(risk_usd, 4),
        "status": "OPEN",
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "closed_at": None,
        "pnl": 0.0,
        "source": signal.get("source", "unknown"),
    }
    state["open_positions"].append(trade)
    state["total_trades"] += 1
    _save_state(state)
    # Append to ledger
    PAPER_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PAPER_LOG.open("a") as f:
        f.write(json.dumps({"event": "OPEN", **trade}) + "\n")
    logger.info("Shadow OPEN %s %s @ %.4f size %.4f", direction, coin, fill_px, size)
    return trade


def update_positions(marks: Dict[str, float]) -> dict:
    """Update open positions against current marks, close if SL/TP hit."""
    state = _load_state()
    open_pos = state.get("open_positions", [])
    now = datetime.now(timezone.utc)
    closed = []
    remaining = []
    for pos in open_pos:
        coin = pos["coin"]
        mark = marks.get(coin)
        if not mark:
            remaining.append(pos)
            continue
        direction = pos["direction"]
        entry = pos["entry_px"]
        sl = pos["target_sl"]
        tp = pos["target_tp"]
        size = pos["size"]
        pnl = (mark - entry) * size if direction == "LONG" else (entry - mark) * size
        pos["mark_px"] = round(mark, 4)
        pos["unrealized_pnl"] = round(pnl, 4)
        # Check exits
        hit_sl = (direction == "LONG" and mark <= sl) or (direction == "SHORT" and mark >= sl)
        hit_tp = (direction == "LONG" and mark >= tp) or (direction == "SHORT" and mark <= tp)
        if hit_sl or hit_tp:
            pos["status"] = "CLOSED"
            pos["closed_at"] = now.isoformat()
            pos["pnl"] = round(pnl, 4)
            pos["exit_px"] = round(mark, 4)
            pos["exit_reason"] = "SL" if hit_sl else "TP"
            state["current_bankroll"] += pnl
            if pnl > 0:
                state["total_wins"] += 1
            else:
                state["daily_loss_today"] += abs(pnl)
            closed.append(pos)
            with PAPER_LOG.open("a") as f:
                f.write(json.dumps({"event": "CLOSE", **pos}) + "\n")
            logger.info("Shadow CLOSE %s %s pnl=%.4f reason=%s", coin, direction, pnl, pos["exit_reason"])
        else:
            remaining.append(pos)
    # Update peak
    state["open_positions"] = remaining
    if state["current_bankroll"] > state["peak_bankroll"]:
        state["peak_bankroll"] = state["current_bankroll"]
    _save_state(state)
    return {"closed": len(closed), "remaining": len(remaining), "bankroll": round(state["current_bankroll"], 2), "drawdown_pct": round((state["peak_bankroll"]-state["current_bankroll"])/state["peak_bankroll"]*100, 2)}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    marks = get_all_marks()
    print(f"Loaded {len(marks)} mark prices")
    state = _load_state()
    print(f"Bankroll: ${state['current_bankroll']:.2f}  Open: {len(state['open_positions'])}  Trades: {state['total_trades']}")
    sys.exit(0)
