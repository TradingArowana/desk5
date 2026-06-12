"""
Hyperliquid Autonomous Executor — places signed orders via hyperliquid-python-sdk.
PRIVATE KEY loaded from .env.  Only places/cancels perp orders — no withdrawals.
Safety circuits hardcoded and enforced before every action.
"""
import os, json, logging, sys, math
from pathlib import Path
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from hyperliquid.exchange import Exchange

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scaling safety constants — ALL scale with live bankroll for compounding
# ---------------------------------------------------------------------------
MAX_DRAWDOWN_PCT   = 20.0     # WARNING mode — logs but does NOT halt trading
MAX_LEVERAGE       = 5        # 5x leverage — stays fixed
RISK_PER_TRADE_PCT = 0.02     # 2% risk per trade (scales with bankroll)
_START_BANKROLL    = 6344.0   # Actual deposited principal — recovery baseline

# ── NEW: Hard guards against micro-cap / penny-coin sizing disaster ──
MIN_COIN_PRICE     = 0.10     # Reject any coin below $0.10
MAX_NOTIONAL_USD   = 5000.0   # ABSOLUTE hard cap per trade
POSITION_PCT_OF_BR = 0.15     # 15% of live bankroll per trade (compounding)
MAX_POSITIONS      = 8        # Hard cap regardless of bankroll tier
COOLDOWN_MINUTES   = 15       # No re-entry on same coin within 15 minutes (faster)

# Dynamic scalers — scale with live bankroll for compounding
def _max_positions(br: float) -> int:
    """Max concurrent positions scales with bankroll tier — HARD CAPPED at MAX_POSITIONS."""
    if br >= 50000:
        return min(12, MAX_POSITIONS)
    if br >= 20000:
        return min(10, MAX_POSITIONS)
    if br >= 10000:
        return min(8, MAX_POSITIONS)
    if br >= 5000:
        return min(7, MAX_POSITIONS)
    if br >= 3000:
        return min(6, MAX_POSITIONS)
    return MAX_POSITIONS

def _min_notional(br: float) -> float:
    """Min trade notional: 3% of bankroll, floor $75, soft cap at $300."""
    return min(300.0, max(75.0, br * 0.03))

STATE_DIR = PROJECT_ROOT / "data_store"
EXEC_STATE = STATE_DIR / "exec_state.json"
LEDGER     = STATE_DIR / "live_ledger.json"

# ---------------------------------------------------------------------------
# Asset metadata cache (szDecimals, tick size, isDelisted)
# ---------------------------------------------------------------------------
_ASSET_META: Dict[str, dict] = {}

def _refresh_asset_meta() -> dict:
    global _ASSET_META
    try:
        import requests
        r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "meta"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        _ASSET_META = {c["name"]: c for c in data.get("universe", [])}
        return _ASSET_META
    except Exception as exc:
        logger.warning("Asset meta refresh failed: %s", exc)
        return _ASSET_META

def _asset_meta(coin: str) -> Optional[dict]:
    if coin not in _ASSET_META:
        _refresh_asset_meta()
    return _ASSET_META.get(coin)

def _round_sz(coin: str, raw: float) -> float:
    meta = _asset_meta(coin)
    if not meta:
        return round(raw, 4)
    dec = meta.get("szDecimals", 4)
    q = Decimal(1).scaleb(-dec)   # 10^-dec
    d = Decimal(str(raw)).quantize(q, rounding=ROUND_DOWN)
    return float(d)

def _round_px(coin: str, px: float) -> float:
    meta = _asset_meta(coin)
    if not meta:
        return round(px, 4)
    dec = meta.get("szDecimals", 4)
    # Per SDK: round(float(f"{px:.5g}"), 6 - szDecimals)
    tick_dec = max(0, 6 - dec)
    return round(float(f"{px:.5g}"), tick_dec)

def _is_delisted(coin: str) -> bool:
    meta = _asset_meta(coin)
    return meta.get("isDelisted", False) if meta else False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_env_key() -> str:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        raise RuntimeError(".env not found")
    for line in env_path.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            k, _, v = line.partition("=")
            if k.strip() == "HL_API_KEY":
                return v.strip()
    raise RuntimeError("HL_API_KEY missing in .env")

def _derive_address(key_hex: str) -> str:
    from eth_account import Account
    acct = Account.from_key(key_hex)
    return acct.address

def _load_exec_state() -> dict:
    if EXEC_STATE.exists():
        try:
            return json.loads(EXEC_STATE.read_text())
        except Exception:
            pass
    return {
        "today": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "day_start_equity": 0.0,
        "daily_loss": 0.0,
        "daily_wins": 0.0,
        "halted": False,
        "halt_reason": None,
        "positions_open": 0,
        "total_trades": 0,
        "total_wins": 0,
        "peak_bankroll": _START_BANKROLL,
    }

def _save_exec_state(state: dict):
    EXEC_STATE.write_text(json.dumps(state, indent=2))

def _load_ledger() -> List[dict]:
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text())
        except Exception:
            pass
    return []

def _save_ledger(ledger: List[dict]):
    LEDGER.write_text(json.dumps(ledger, indent=2))

# ---------------------------------------------------------------------------
# Exchange client
# ---------------------------------------------------------------------------

def _exchange() -> Exchange:
    key = _load_env_key()
    from eth_account import Account
    wallet = Account.from_key(key)
    return Exchange(wallet, base_url="https://api.hyperliquid.xyz")


def cancel_order(coin: str, oid: int) -> dict:
    """Cancel a single open order on Hyperliquid."""
    try:
        ex = _exchange()
        result = ex.cancel(coin, oid)
        logger.info("Cancel %s oid=%s: %s", coin, oid, result)
        return {"status": "cancelled", "coin": coin, "oid": oid, "result": result}
    except Exception as exc:
        logger.error("Cancel failed for %s oid=%s: %s", coin, oid, exc)
        return {"status": "error", "reason": str(exc), "coin": coin, "oid": oid}


def cancel_all_open_orders(exchange: Exchange = None) -> dict:
    """Query and cancel ALL unfilled open orders for the wallet."""
    from workers.execution.hl_bridge import get_open_orders
    orders = get_open_orders()
    if not orders:
        return {"status": "ok", "cancelled": 0, "failed": 0, "orders": [], "errors": []}
    ex = exchange or _exchange()
    cancelled = []
    failed = []
    for o in orders:
        try:
            result = ex.cancel(o["coin"], o["oid"])
            cancelled.append({"coin": o["coin"], "oid": o["oid"], "result": result})
            logger.info("Cancelled %s oid=%s", o["coin"], o["oid"])
        except Exception as exc:
            failed.append({"coin": o["coin"], "oid": o["oid"], "error": str(exc)})
            logger.error("Failed to cancel %s oid=%s: %s", o["coin"], o["oid"], exc)
    return {"status": "ok", "cancelled": len(cancelled), "failed": len(failed), "orders": cancelled, "errors": failed}


# ---------------------------------------------------------------------------
# Safety circuit
# ---------------------------------------------------------------------------

def _safety_check(state: dict) -> tuple[bool, str]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("today") != today:
        # New day: snapshot equity and reset all counters
        state["today"] = today
        state["daily_loss"] = 0.0
        state["daily_wins"] = 0.0
        state["halted"] = False
        state["halt_reason"] = None
        # Snapshot real equity at day open; fall back to peak if fetch fails
        try:
            br = _estimate_bankroll(state)
            state["day_start_equity"] = br if br > 0 else state.get("peak_bankroll", _START_BANKROLL)
        except Exception:
            state["day_start_equity"] = state.get("peak_bankroll", _START_BANKROLL)
        _save_exec_state(state)

    if state.get("halted"):
        return False, state.get("halt_reason", "Halted")

    # Fixed daily loss cap — 10% of day-start equity, never floating
    day_start = state.get("day_start_equity", _START_BANKROLL)
    if day_start <= 0:
        day_start = _START_BANKROLL
    daily_loss_cap = max(50.0, day_start * 0.10)

    # Net daily P&L (wins - losses). A profitable day does NOT halt.
    daily_net = state.get("daily_wins", 0) - state.get("daily_loss", 0)
    if daily_net <= -daily_loss_cap:
        state["halted"] = True
        state["halt_reason"] = f"Daily net PnL ${daily_net:.2f} hit cap -${daily_loss_cap:.2f} (10% of day-start ${day_start:.2f})"
        _save_exec_state(state)
        return False, state["halt_reason"]

    # Profit-lock logic removed — no throttle for winning days
    max_pos = _max_positions(_estimate_bankroll(state))
    reason = "OK"

    open_count = len([p for p in _load_ledger() if p.get("status") == "OPEN"])
    if open_count >= max_pos:
        return False, f"Max {max_pos} positions open ({open_count})" + (f" | {reason}" if "PROFIT" in reason else "")

    # - Live reconciliation guard -
    try:
        from workers.execution.hl_bridge import get_positions
        api_positions = get_positions()
        api_coins = {(p.get("coin"), p.get("side")) for p in api_positions}
        ledger = _load_ledger()
        ledger_open = [p for p in ledger if p.get("status") == "OPEN"]
        ledger_coins = {(p.get("coin"), p.get("direction")) for p in ledger_open}

        if api_coins != ledger_coins:
            logger.warning("Reconciliation diff detected: API=%s vs LEDGER=%s — auto-reconciling", api_coins, ledger_coins)
            live_dict = {p["coin"]: p for p in api_positions}
            any_change = False
            for pos in ledger:
                if pos.get("status") != "OPEN":
                    continue
                coin = pos["coin"]
                if coin not in live_dict:
                    pos["status"] = "CLOSED"
                    pos["closed_at"] = datetime.now(timezone.utc).isoformat()
                    pos["exit_reason"] = "RECONCILED_GONE"
                    # Phantom P&L does NOT hit daily_loss — only real closes do
                    any_change = True
            ledger_dict = {p["coin"]: p for p in ledger if p.get("status") == "OPEN"}
            for lp in api_positions:
                coin = lp["coin"]
                if coin not in ledger_dict:
                    ledger.append({
                        "id": f"{coin}_RECON_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                        "coin": coin,
                        "direction": lp["side"],
                        "entry_px": lp["entry_px"],
                        "size": lp["size"],
                        "sl_px": round(lp["entry_px"] * 0.98, 6) if lp["side"] == "LONG" else round(lp["entry_px"] * 1.02, 6),
                        "tp_px": round(lp["entry_px"] * 1.05, 6) if lp["side"] == "LONG" else round(lp["entry_px"] * 0.95, 6),
                        "status": "OPEN",
                        "open_at": datetime.now(timezone.utc).isoformat(),
                        "mark_px": lp["mark_px"],
                        "unrealized_pnl": lp["unrealized_pnl"],
                        "leverage": lp.get("leverage", 1),
                        "reconciled": True,
                    })
                    any_change = True
            if any_change:
                _save_ledger(ledger)
                state["positions_open"] = len([p for p in ledger if p.get("status") == "OPEN"])
                _save_exec_state(state)
            ledger_coins = {(p.get("coin"), p.get("direction")) for p in ledger if p.get("status") == "OPEN"}
            # Retry count: only halt after 3 failed reconciliation cycles in same run
            retry_key = "recon_retries"
            if api_coins != ledger_coins:
                state[retry_key] = state.get(retry_key, 0) + 1
                if state[retry_key] >= 3:
                    state["halted"] = True
                    state["halt_reason"] = f"RECONCILIATION MISMATCH PERSISTS (3x): API={api_coins} vs LEDGER={ledger_coins}"
                    _save_exec_state(state)
                    logger.error(state["halt_reason"])
                    return False, state["halt_reason"]
                logger.warning("Recon mismatch #%d — will retry next cycle", state[retry_key])
                return True, "RECON_RETRY"
            else:
                state[retry_key] = 0
    except Exception as exc:
        logger.warning("Reconciliation check failed: %s — proceeding with caution", exc)

    return True, reason

def _estimate_bankroll(state: dict) -> float:
    """Use TOTAL account equity for sizing — perp + free spot in unified margin.
    Falls back to latest known good value from capital tracker."""
    from workers.execution.hl_bridge import get_account_value
    account = get_account_value()
    live = account.get("total", 0)
    # If API returns 0, use the last known good balance
    if live > 0:
        return live
    ct = PROJECT_ROOT / "data_store" / "capital_tracker.json"
    if ct.exists():
        try:
            d = json.loads(ct.read_text())
            cached = max(d.get("last_balance", 0), d.get("peak", 0))
            if cached > 0:
                return cached
        except Exception:
            pass
    # Absolute emergency fallback only
    return _START_BANKROLL

def _check_drawdown(state: dict) -> tuple[bool, str]:
    br = _estimate_bankroll(state)
    if br > state.get("peak_bankroll", _START_BANKROLL):
        state["peak_bankroll"] = br
        _save_exec_state(state)
    peak = state.get("peak_bankroll", _START_BANKROLL)
    dd = (peak - br) / peak * 100 if peak > 0 else 0
    if dd >= MAX_DRAWDOWN_PCT:
        # CEO OVERRIDE: drawdown already realized. Trade to recover.
        # Log warning but NEVER halt — capital is already deployed.
        logger.warning("DRAWDOWN WARNING: %.1f%% (bankroll $%.2f vs peak $%.2f). Trading continues to recover.", dd, br, peak)
        _save_exec_state(state)
        return True, f"DD {dd:.1f}% — RECOVERY MODE"
    return True, f"DD {dd:.2f}%"

# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def _order_accepted(hl_response: dict) -> bool:
    """Check if Hyperliquid actually accepted the order."""
    if not isinstance(hl_response, dict):
        return False
    if hl_response.get("status") != "ok":
        return False
    resp = hl_response.get("response", {})
    data = resp.get("data", {})
    statuses = data.get("statuses", [])
    if not statuses:
        return False
    st = statuses[0]
    if isinstance(st, dict) and ("error" in st or ("resting" not in st and "filled" not in st)):
        return False
    return True


def place_order(signal: dict, exchange: Exchange = None) -> dict:
    """Execute a live perp order on Hyperliquid."""
    state = _load_exec_state()

    ok, reason = _safety_check(state)
    if not ok:
        return {"status": "rejected", "reason": reason, "signal": signal}

    ok, reason = _check_drawdown(state)
    if not ok:
        return {"status": "rejected", "reason": reason, "signal": signal}

    coin = signal["coin"]

    # Delisted filter
    if _is_delisted(coin):
        return {"status": "rejected", "reason": f"{coin} is delisted / trading halted", "signal": signal}

    direction = signal["direction"]
    entry = float(signal.get("entry_px", 0))
    sl = float(signal.get("sl_px", 0))
    tp = float(signal.get("tp_px", 0))

    # ── HARD GUARD #1: reject micro-cap / penny coins ──
    if entry < MIN_COIN_PRICE:
        return {"status": "rejected", "reason": f"{coin} entry ${entry:.6f} below MIN_COIN_PRICE ${MIN_COIN_PRICE}", "signal": signal}

    # ── HARD GUARD #2: max positions cap ──
    ledger = _load_ledger()
    open_count = len([p for p in ledger if p.get("status") == "OPEN"])
    if open_count >= MAX_POSITIONS:
        return {"status": "rejected", "reason": f"MAX_POSITIONS {MAX_POSITIONS} reached ({open_count} open)", "signal": signal}

    # ── HARD GUARD #3: cooldown — no re-entry on same coin within 30 min ──
    now = datetime.now(timezone.utc)
    for pos in ledger:
        if pos.get("coin") == coin and pos.get("status") in ("OPEN", "CLOSED"):
            try:
                opened = datetime.fromisoformat(pos.get("open_at") or pos.get("opened_at", ""))
                if (now - opened).total_seconds() < COOLDOWN_MINUTES * 60:
                    return {"status": "rejected", "reason": f"{coin} on cooldown ({COOLDOWN_MINUTES}min)", "signal": signal}
            except Exception:
                pass

    # ── Sizing: live bankroll compounding, ignore signal size ──
    br = _estimate_bankroll(state)
    if br > state.get("peak_bankroll", _START_BANKROLL):
        state["peak_bankroll"] = br
        _save_exec_state(state)
        logger.info("📈 Bankroll compounded → $%.2f", br)

    # Target notional = % of live bankroll (compounding engine)
    strategy = signal.get("strategy", "")
    if strategy == "asymmetric_pocket":
        # 10% asymmetric risk pocket — dedicated allocation for moonshots
        target_notional = br * 0.10
        logger.info("ASYMMETRIC POCKET sizing for %s | target=$%.2f (10%% of br)", coin, target_notional)
    else:
        target_notional = br * POSITION_PCT_OF_BR

    # Risk-based ceiling: never risk more than RISK_PER_TRADE_PCT of bankroll
    risk_usd = br * RISK_PER_TRADE_PCT
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return {"status": "rejected", "reason": "SL too close or zero", "signal": signal}
    risk_based_size = risk_usd / sl_dist
    risk_based_notional = risk_based_size * entry

    # Final notional: target, capped by risk-based ceiling and absolute hard cap
    size_usd = min(target_notional, risk_based_notional, MAX_NOTIONAL_USD)

    # Enforce minimum notional (scales with bankroll)
    min_n = _min_notional(br)
    if size_usd < min_n:
        size_usd = min_n

    # ── HARD GUARD #4: max notional cap ──
    if size_usd > MAX_NOTIONAL_USD:
        size_usd = MAX_NOTIONAL_USD
        logger.info("Notional capped for %s: $%.2f → $%.2f", coin, size_usd, MAX_NOTIONAL_USD)

    size = size_usd / entry
    logger.info("Sizing %s | strategy=%s | target=$%.2f risk_based=$%.2f final=$%.2f (br=$%.2f)",
                coin, strategy, target_notional, risk_based_notional, size_usd, br)
    _lev = MAX_LEVERAGE
    try:
        ex = exchange or _exchange()
        ex.update_leverage(_lev, coin)
        logger.info("Set %s leverage to %dx", coin, _lev)
    except Exception as lev_exc:
        logger.warning("Leverage update failed for %s: %s", coin, lev_exc)

    size = _round_sz(coin, size)
    if size <= 0:
        return {"status": "rejected", "reason": "Calculated size <= 0", "signal": signal}

    # Check if already have position in this coin
    ledger = _load_ledger()
    existing = [p for p in ledger if p.get("status") == "OPEN" and p.get("coin") == coin]
    if existing:
        return {"status": "rejected", "reason": f"Already have {len(existing)} open position(s) in {coin}", "signal": signal}

    # Cancel any existing unfilled open orders for this coin (prevents double-stacking)
    try:
        from workers.execution.hl_bridge import get_open_orders
        open_orders = get_open_orders()
        coin_orders = [o for o in open_orders if o.get("coin") == coin]
        if coin_orders:
            ex = exchange or _exchange()
            for o in coin_orders:
                try:
                    ex.cancel(o["coin"], o["oid"])
                    logger.info("Pre-trade cancel: %s oid=%s", o["coin"], o["oid"])
                except Exception as cexc:
                    logger.warning("Pre-trade cancel failed for %s: %s", coin, cexc)
    except Exception as pre_exc:
        logger.warning("Pre-trade open-order check failed: %s", pre_exc)

    is_buy = direction == "LONG"
    px = _round_px(coin, entry)

    try:
        result = ex.order(
            coin,
            is_buy,
            size,
            px,
            {"limit": {"tif": "Gtc"}},
            reduce_only=False,
        )
        logger.info("Order result: %s", result)

        accepted = _order_accepted(result)

        pos = {
            "id": f"{coin}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            "coin": coin,
            "direction": direction,
            "entry_px": px,
            "size": float(size),
            "initial_size": float(size),
            "sl_px": round(sl, 6),
            "tp_px": round(tp, 6),
            "status": "OPEN" if accepted else "REJECTED",
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "hl_response": result,
            "pnl": 0.0,
            "pnl_partial": 0.0,
            "highest_seen": float(px),
            "lowest_seen": float(px),
            "partial_closed_at": None,
            "sl_breakeven_moved": False,
        }
        ledger.append(pos)
        _save_ledger(ledger)

        state["positions_open"] = len([p for p in ledger if p.get("status") == "OPEN"])
        if accepted:
            state["total_trades"] = state.get("total_trades", 0) + 1
        _save_exec_state(state)

        return {
            "status": "filled" if accepted else "rejected",
            "coin": coin,
            "direction": direction,
            "size": float(size),
            "entry": px,
            "sl": round(sl, 6),
            "tp": round(tp, 6),
            "hl_response": result,
            "reason": None if accepted else (result.get("response", {}).get("data", {}).get("statuses", [{}])[0].get("error", "unknown")),
        }
    except Exception as exc:
        logger.error("Order execution failed: %s", exc)
        return {"status": "error", "reason": str(exc), "signal": signal}

# ---------------------------------------------------------------------------
# Position sync / SL-TP monitoring
# ---------------------------------------------------------------------------

def sync_positions() -> dict:
    """Sync HL clearinghouse positions → ledger, reconcile orphaned entries, check SL/TP."""
    from workers.execution.hl_bridge import get_all_marks, get_positions
    marks = get_all_marks()
    ledger = _load_ledger()
    state = _load_exec_state()
    alerts = []
    any_closed = False

    # 0) Cancel stale unfilled open orders (older than 2 hours)
    try:
        from workers.execution.hl_bridge import get_open_orders
        open_orders = get_open_orders()
        now_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        stale_orders = [o for o in open_orders if (now_ts - o.get("timestamp", 0)) > 2*3600*1000]
        if stale_orders:
            logger.info("Found %d stale unfilled orders (older than 2h)", len(stale_orders))
            for o in stale_orders:
                try:
                    ex = _exchange()
                    ex.cancel(o["coin"], o["oid"])
                    logger.info("Auto-cancelled stale order %s oid=%s", o["coin"], o["oid"])
                    alerts.append(f"🗑️ Auto-cancelled stale {o['coin']} order (unfilled >2h)")
                except Exception as cexc:
                    logger.error("Auto-cancel failed for %s oid=%s: %s", o["coin"], o["oid"], cexc)
    except Exception as order_exc:
        logger.warning("Open order check failed: %s", order_exc)

    # 1) Reconcile with live clearinghouse state
    live_positions = get_positions()
    live_coins = {p["coin"]: p for p in live_positions}

    # Mark ledger entries as CLOSED if no longer on HL
    for pos in ledger:
        if pos.get("status") != "OPEN":
            continue
        coin = pos["coin"]
        if coin not in live_coins:
            # Position gone from HL — assume filled/liquidated externally
            pos["status"] = "CLOSED"
            pos["closed_at"] = datetime.now(timezone.utc).isoformat()
            pos["exit_reason"] = "RECONCILED_GONE"
            pos["pnl"] = 0  # phantom P&L does NOT count toward daily
            any_closed = True
            alerts.append(f"⚠️ {coin} {pos.get('direction','?')} marked CLOSED (no longer on HL)")

    # Add missing positions from HL into ledger
    ledger_coins = {p["coin"]: p for p in ledger if p.get("status") == "OPEN"}
    for lp in live_positions:
        coin = lp["coin"]
        if coin not in ledger_coins:
            # Reconstruct minimal ledger entry from HL data
            entry = {
                "id": f"{coin}_RECON_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                "coin": coin,
                "direction": lp["side"],
                "entry_px": lp["entry_px"],
                "size": lp["size"],
                "initial_size": lp["size"],
                "sl_px": round(lp["entry_px"] * 0.98, 6) if lp["side"] == "LONG" else round(lp["entry_px"] * 1.02, 6),
                "tp_px": round(lp["entry_px"] * 1.05, 6) if lp["side"] == "LONG" else round(lp["entry_px"] * 0.95, 6),
                "status": "OPEN",
                "open_at": datetime.now(timezone.utc).isoformat(),
                "mark_px": lp["mark_px"],
                "unrealized_pnl": lp["unrealized_pnl"],
                "leverage": lp.get("leverage", 1),
                "reconciled": True,
                "highest_seen": lp["entry_px"],
                "lowest_seen": lp["entry_px"],
                "pnl_partial": 0.0,
                "partial_closed_at": None,
                "sl_breakeven_moved": False,
            }
            ledger.append(entry)
            state["positions_open"] = state.get("positions_open", 0) + 1
            alerts.append(f"🔄 RECONCILED {coin} {lp['side']} @ {lp['entry_px']} (size {lp['size']})")

    # 2) Trailing-stop + partial-profit logic on reconciled ledger
    for pos in ledger:
        if pos.get("status") != "OPEN":
            continue
        coin = pos["coin"]
        mark = marks.get(coin)
        if not mark:
            continue
        pos["mark_px"] = round(mark, 6)
        direction = pos["direction"]
        entry = pos["entry_px"]
        size = pos["size"]
        sl = pos["sl_px"]
        tp = pos["tp_px"]
        initial_size = pos.get("initial_size", size)

        # Track extremes since entry (for trailing stop)
        if direction == "LONG":
            pos["highest_seen"] = round(max(pos.get("highest_seen", entry), mark), 6)
            highest = pos["highest_seen"]
            pnl = (mark - entry) * size
            sl_dist = abs(entry - sl) if abs(entry - sl) > 0 else entry * 0.02
            r_multiple = (mark - entry) / sl_dist
            hit_sl = mark <= sl
            hit_tp = mark >= tp
        else:
            pos["lowest_seen"] = round(min(pos.get("lowest_seen", entry), mark), 6)
            lowest = pos["lowest_seen"]
            pnl = (entry - mark) * size
            sl_dist = abs(sl - entry) if abs(sl - entry) > 0 else entry * 0.02
            r_multiple = (entry - mark) / sl_dist
            hit_sl = mark >= sl
            hit_tp = mark <= tp

        pos["unrealized_pnl"] = round(pnl, 4)

        # --- PARTIAL CLOSE AT +2R (if full size still on) ---
        if r_multiple >= 2.0 and pos.get("partial_closed_at") is None:
            half_qty = _round_sz(coin, size * 0.50)
            if half_qty <= 0:
                logger.warning("Partial close skipped for %s: calculated half_qty = 0", coin)
            else:
                try:
                    ex = _exchange()
                    res = ex.market_close(coin, sz=half_qty)
                    logger.info("Partial close %s %s: %s", coin, direction, res)
                    # Update position bookkeeping
                    pos["size"] = round(size - half_qty, 6)
                    pos["pnl_partial"] = round(pos.get("pnl_partial", 0) + (mark - entry) * half_qty if direction == "LONG" else (entry - mark) * half_qty, 4)
                    pos["partial_closed_at"] = round(mark, 6)
                    alerts.append(
                        f"💰 {coin} {direction} PARTIAL CLOSE 50% @ {mark:.4f}\n"
                        f"Locked: ${pos['pnl_partial']:+.2f} | Remaining: {pos['size']:.4f}"
                    )
                    # Continue — do NOT full-close yet; let trail run on remainder
                except Exception as pexc:
                    logger.error("Partial close FAILED for %s: %s", coin, pexc)
                    # If partial close fails, keep full size and continue

        # --- TRAILING STOP ADJUSTMENT ---
        if r_multiple >= 1.0 and not pos.get("sl_breakeven_moved", False):
            # Move SL to breakeven once +1R reached
            pos["sl_px"] = round(entry, 6)
            pos["sl_breakeven_moved"] = True
            alerts.append(f"🔒 {coin} {direction} SL moved to BREAKEVEN @ {entry:.4f} (reached +1R)")

        if r_multiple >= 2.0:
            # Trail at 1R distance behind extreme (tighter after partial)
            if direction == "LONG":
                trail = round(highest - sl_dist, 6)
                if trail > sl:
                    pos["sl_px"] = trail
                    alerts.append(f"📉 {coin} LONG trail raised to {trail:.4f} (1R below high {highest:.4f})")
            else:
                trail = round(lowest + sl_dist, 6)
                if trail < sl:
                    pos["sl_px"] = trail
                    alerts.append(f"📈 {coin} SHORT trail lowered to {trail:.4f} (1R above low {lowest:.4f})")

        # Re-fetch SL after trail updates for final close check
        sl = pos["sl_px"]

        if hit_sl or hit_tp:
            close_qty = _round_sz(coin, pos["size"])
            if close_qty <= 0:
                pos["status"] = "CLOSED"
                pos["closed_at"] = datetime.now(timezone.utc).isoformat()
                pos["pnl"] = round(pnl, 4)
                pos["exit_px"] = round(mark, 6)
                pos["exit_reason"] = "SL" if hit_sl else "TP"
                any_closed = True
                # Book total = realized partial + final close
                total_pnl = pos.get("pnl_partial", 0) + pnl
                if total_pnl > 0:
                    state["daily_wins"] = state.get("daily_wins", 0) + total_pnl
                else:
                    state["daily_loss"] = state.get("daily_loss", 0) + abs(total_pnl)
                alerts.append(
                    f"{'🛑' if hit_sl else '🎯'} {coin} {direction} CLOSED @ {mark:.4f}\n"
                    f"PnL: ${total_pnl:+.2f} ({'SL' if hit_sl else 'TP'}) | "
                    f"Partial: ${pos.get('pnl_partial', 0):+.2f}"
                )
                continue

            try:
                ex = _exchange()
                close_result = ex.market_close(coin, sz=close_qty)
                logger.info("SL/TP close %s %s: %s", coin, direction, close_result)
            except Exception as close_exc:
                logger.error("FAILED to close %s on SL/TP: %s", coin, close_exc)
                continue

            pos["status"] = "CLOSED"
            pos["closed_at"] = datetime.now(timezone.utc).isoformat()
            pos["pnl"] = round(pnl, 4)
            pos["exit_px"] = round(mark, 6)
            pos["exit_reason"] = "SL" if hit_sl else "TP"
            any_closed = True
            total_pnl = pos.get("pnl_partial", 0) + pnl
            if total_pnl > 0:
                state["daily_wins"] = state.get("daily_wins", 0) + total_pnl
            else:
                state["daily_loss"] = state.get("daily_loss", 0) + abs(total_pnl)
            alerts.append(
                f"{'🛑' if hit_sl else '🎯'} {coin} {direction} CLOSED @ {mark:.4f}\n"
                f"PnL: ${total_pnl:+.2f} ({'SL' if hit_sl else 'TP'}) | "
                f"Partial: ${pos.get('pnl_partial', 0):+.2f}"
            )

    if any_closed:
        _save_ledger(ledger)
        _check_drawdown(state)
        _save_exec_state(state)

    open_count = len([p for p in ledger if p.get("status") == "OPEN"])
    unrealized = sum(p.get("unrealized_pnl", 0) for p in ledger if p.get("status") == "OPEN")
    realized = sum(p.get("pnl", 0) for p in ledger if p.get("status") == "CLOSED")
    # Live account value, not hardcoded start
    live_br = _estimate_bankroll(state)
    bankroll = live_br
    # Sync peak if we're at new highs
    if bankroll > state.get("peak_bankroll", _START_BANKROLL):
        state["peak_bankroll"] = bankroll
        _save_exec_state(state)
    open_positions = [p for p in ledger if p.get("status") == "OPEN"]

    return {
        "open_count": open_count,
        "positions": open_positions,
        "total_realized_pnl": round(realized, 2),
        "total_unrealized_pnl": round(unrealized, 2),
        "estimated_bankroll": round(bankroll, 2),
        "alerts": alerts,
        "dt": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = _load_exec_state()
    ok, reason = _safety_check(state)
    print(json.dumps({"safety_ok": ok, "reason": reason, "state": state}, indent=2))
    try:
        addr = _derive_address(_load_env_key())
        print(f"Derived address: {addr}")
    except Exception as e:
        print(f"Key error: {e}")
    # Test meta refresh
    meta = _refresh_asset_meta()
    for c in ["ETH", "AAVE", "XRP"]:
        m = meta.get(c)
        if m:
            print(f"{c}: szDecimals={m['szDecimals']}, delisted={m.get('isDelisted', False)}")
            print(f"  _round_sz(1.2345) = {_round_sz(c, 1.2345)}")
            print(f"  _round_px(100.12345) = {_round_px(c, 100.12345)}")
