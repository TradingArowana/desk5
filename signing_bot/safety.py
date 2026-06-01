"""Safety circuit breaker logic — enforces capital preservation."""
import logging
from typing import Tuple

from config import (
    MAX_POSITIONS,
    RISK_PER_TRADE_PCT,
    MAX_DAILY_LOSS_USD,
    MAX_LEVERAGE,
    MAX_DRAWDOWN_PCT,
)
from state import load_state, save_state, reset_daily

logger = logging.getLogger(__name__)


def check_safety(bankroll: float, open_count: int) -> Tuple[bool, str]:
    """Returns (allowed, reason). Updates halt state if triggered."""
    state = load_state()
    reset_daily(state)

    if state.get("halted"):
        return False, state.get("halt_reason", "Halted")

    if state["daily_loss"] >= MAX_DAILY_LOSS_USD:
        state["halted"] = True
        state["halt_reason"] = f"Daily loss ${state['daily_loss']:.2f} >= cap ${MAX_DAILY_LOSS_USD}"
        save_state(state)
        return False, state["halt_reason"]

    if open_count >= MAX_POSITIONS:
        return False, f"Max {MAX_POSITIONS} positions open ({open_count})"

    # Drawdown check
    peak = state.get("peak_bankroll", bankroll)
    if bankroll > peak:
        state["peak_bankroll"] = bankroll
        save_state(state)
        peak = bankroll

    if peak > 0:
        dd = (peak - bankroll) / peak * 100
        if dd >= MAX_DRAWDOWN_PCT:
            state["halted"] = True
            state["halt_reason"] = f"MAX DRAWDOWN {dd:.1f}% (bankroll ${bankroll:.2f})"
            save_state(state)
            return False, state["halt_reason"]

    return True, "OK"


def compute_size(signal: dict, bankroll: float) -> float:
    """Position size from risk budget and stop distance."""
    entry = float(signal.get("entry_px", 0))
    sl = float(signal.get("sl_px", 0))
    risk_usd = bankroll * RISK_PER_TRADE_PCT
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return 0.0
    size = risk_usd / sl_dist
    return round(size, 6)
