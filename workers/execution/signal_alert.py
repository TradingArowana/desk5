"""
Live Signal Alert System for Desk5.
Monitors strategies and sends Telegram alerts when trade setups fire.
Auto-executes if LIVE_MODE=true in .env.
"""
import json, logging, math, sys, os
from pathlib import Path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from workers.strategies.vol_squeeze import scan_squeezes as vs_scan
from workers.strategies.momentum_fade import scan_fades as mf_scan
from workers.execution.hl_bridge import get_all_marks

logger = logging.getLogger(__name__)

LIVE_MODE = os.environ.get("LIVE_MODE", "").lower() == "true"
if LIVE_MODE:
    try:
        from workers.execution.hl_executor import place_order, _load_exec_state, _check_drawdown, _safety_check
        EXECUTOR_AVAILABLE = True
    except Exception as exc:
        logger.error("Executor import failed in live mode: %s", exc)
        EXECUTOR_AVAILABLE = False

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
ALERT_STATE = STATE_DIR / "alert_state.json"

RISK_PER_TRADE_PCT = 0.02  # 2% matches executor

CG_OHLC_MAP = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "AVAX": "avalanche-2",
    "PAXG": "pax-gold", "DOGE": "dogecoin", "LINK": "chainlink",
    "MATIC": "matic-network", "ARB": "arbitrum", "GMX": "gmx", "BNB": "binancecoin",
    "XRP": "ripple", "ADA": "cardano", "DOT": "polkadot", "OP": "optimism",
    "ATOM": "cosmos", "APE": "apecoin", "INJ": "injective-protocol",
    "SUI": "sui", "CRV": "curve-dao-token", "LDO": "lido-dao", "STX": "blockstack",
    "RNDR": "render-token", "FTM": "fantom", "SNX": "havven", "BCH": "bitcoin-cash",
    "APT": "aptos", "AAVE": "aave", "COMP": "compound-governance-token",
    "MKR": "maker", "WLD": "worldcoin-wld", "TRX": "tron",
}


def _load_alert_state() -> dict:
    if ALERT_STATE.exists():
        try:
            return json.loads(ALERT_STATE.read_text())
        except Exception:
            pass
    return {
        "last_alerts": {},
        "today": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def _save_alert_state(state: dict):
    ALERT_STATE.write_text(json.dumps(state, indent=2))


def _check_safety() -> tuple[bool, str]:
    """Delegate all safety checks to hl_executor — single source of truth."""
    if not EXECUTOR_AVAILABLE:
        return False, "Executor unavailable"
    try:
        exec_state = _load_exec_state()
        ok, reason = _safety_check(exec_state)
        if not ok:
            return False, reason
        # Also check drawdown
        ok2, reason2 = _check_drawdown(exec_state)
        if not ok2:
            return False, reason2
        return True, reason
    except Exception as exc:
        logger.error("Safety check failed: %s", exc)
        return False, f"Safety error: {exc}"


def format_alert(signal: dict, marks: Dict[str, float], bankroll: float = 3500) -> str:
    """Format a signal into a Telegram-ready alert message."""
    coin = signal["coin"]
    direction = signal["direction"]
    entry = signal.get("entry_px", 0)
    sl = signal.get("sl_px", 0)
    tp = signal.get("tp_px", 0)
    mark = marks.get(coin, entry)
    risk_usd = bankroll * RISK_PER_TRADE_PCT
    sl_dist = abs(entry - sl)
    size = risk_usd / sl_dist if sl_dist > 0 else 0
    
    msg = f"""🚨 LIVE SIGNAL: {direction} {coin}

Entry: ${entry:.4f} (Mark: ${mark:.4f})
Stop Loss: ${sl:.4f}
Take Profit: ${tp:.4f}

Risk: ${risk_usd:.2f} ({RISK_PER_TRADE_PCT*100:.1f}%)
Suggested Size: {size:.4f} units
R/R: 1:{abs(tp-entry)/abs(entry-sl):.1f}

Signal: {signal.get("source", "unknown")}
"""
    return msg


def run_alert_cycle(bankroll: float = 3500, dry_run: bool = False) -> List[str]:
    """Scan all strategies, execute if live, return alert messages."""
    _live = os.environ.get("LIVE_MODE", "").lower() == "true"
    
    # Unified safety gate
    allowed, reason = _check_safety()
    alerts = []
    
    if not allowed:
        if dry_run:
            alerts.append(f"⛔ HALTED: {reason}")
        return alerts
    
    marks = get_all_marks()
    coins = [c for c in list(CG_OHLC_MAP.keys()) if CG_OHLC_MAP.get(c)]
    state = _load_alert_state()
    
    # Vol Squeeze
    vs_signals = vs_scan(coins)
    for sig in vs_signals:
        coin = sig["coin"]
        key = f"vs_{coin}"
        if key not in state.get("last_alerts", {}):
            sig["source"] = "vol_squeeze"
            msg = format_alert(sig, marks, bankroll)
            alerts.append(msg)
            state["last_alerts"][key] = datetime.now(timezone.utc).isoformat()
            if _live and not dry_run and EXECUTOR_AVAILABLE:
                res = place_order(sig)
                alerts.append(f"🤖 AUTO-EXEC: {coin} {res.get('status','?').upper()}\n{res.get('reason','') or res.get('coin','')}")
    
    # Momentum Fade
    mf_signals = mf_scan(coins)
    for sig in mf_signals:
        coin = sig["coin"]
        key = f"mf_{coin}"
        if key not in state.get("last_alerts", {}):
            sig["source"] = "momentum_fade"
            msg = format_alert(sig, marks, bankroll)
            alerts.append(msg)
            state["last_alerts"][key] = datetime.now(timezone.utc).isoformat()
            if _live and not dry_run and EXECUTOR_AVAILABLE:
                res = place_order(sig)
                alerts.append(f"🤖 AUTO-EXEC: {coin} {res.get('status','?').upper()}\n{res.get('reason','') or res.get('coin','')}")
    
    # Cleanup old alerts (24h expiry)
    now = datetime.now(timezone.utc)
    stale = [k for k, v in state["last_alerts"].items() 
             if (now - datetime.fromisoformat(v)).total_seconds() > 86400]
    for k in stale:
        del state["last_alerts"][k]
    
    _save_alert_state(state)
    return alerts


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    alerts = run_alert_cycle()
    for a in alerts:
        print(a)
        print("---")
    sys.exit(0)
