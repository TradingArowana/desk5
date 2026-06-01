"""Hyperliquid order signing + submission.
Runs entirely locally — private key never leaves this file."""
import logging
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Any, Optional

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from config import HL_API_KEY, MAX_LEVERAGE

logger = logging.getLogger(__name__)

_HL_EXCHANGE: Optional[Exchange] = None
_HL_INFO: Optional[Info] = None
_ASSET_META: Dict[str, dict] = {}


def _exchange() -> Exchange:
    global _HL_EXCHANGE
    if _HL_EXCHANGE is None:
        wallet = Account.from_key(HL_API_KEY)
        _HL_EXCHANGE = Exchange(wallet, base_url="https://api.hyperliquid.xyz")
    return _HL_EXCHANGE


def _info() -> Info:
    global _HL_INFO
    if _HL_INFO is None:
        _HL_INFO = Info(base_url="https://api.hyperliquid.xyz")
    return _HL_INFO


def _refresh_meta():
    global _ASSET_META
    try:
        meta = _info().meta()
        _ASSET_META = {c["name"]: c for c in meta.get("universe", [])}
    except Exception as exc:
        logger.warning("Meta refresh failed: %s", exc)


def _asset_meta(coin: str) -> Optional[dict]:
    if coin not in _ASSET_META:
        _refresh_meta()
    return _ASSET_META.get(coin)


def _round_sz(coin: str, raw: float) -> float:
    meta = _asset_meta(coin)
    if not meta:
        return round(raw, 4)
    dec = meta.get("szDecimals", 4)
    q = Decimal(1).scaleb(-dec)
    d = Decimal(str(raw)).quantize(q, rounding=ROUND_DOWN)
    return float(d)


def _round_px(coin: str, px: float) -> float:
    meta = _asset_meta(coin)
    if not meta:
        return round(px, 4)
    dec = meta.get("szDecimals", 4)
    tick_dec = max(0, 6 - dec)
    return round(float(f"{px:.5g}"), tick_dec)


def _order_accepted(hl_response: dict) -> bool:
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


def submit_order(signal: dict, size: float) -> dict:
    """Sign and submit a perp order to Hyperliquid.
    Returns dict with status, hl_response, and any error reason."""
    coin = signal["coin"]
    direction = signal["direction"]
    entry = float(signal.get("entry_px", 0))
    sl = float(signal.get("sl_px", 0))
    tp = float(signal.get("tp_px", 0))

    is_buy = direction == "LONG"
    px = _round_px(coin, entry)
    sz = _round_sz(coin, size)
    if sz <= 0:
        return {"status": "rejected", "reason": "Calculated size <= 0"}

    ex = _exchange()

    # Force 2× leverage
    try:
        ex.update_leverage(MAX_LEVERAGE, coin)
        logger.info("Set %s leverage to %dx", coin, MAX_LEVERAGE)
    except Exception as lev_exc:
        logger.warning("Leverage update failed for %s: %s", coin, lev_exc)

    try:
        result = ex.order(
            coin,
            is_buy,
            sz,
            px,
            {"limit": {"tif": "Gtc"}},
            reduce_only=False,
        )
        logger.info("HL order result: %s", result)

        accepted = _order_accepted(result)
        return {
            "status": "filled" if accepted else "rejected",
            "coin": coin,
            "direction": direction,
            "size": sz,
            "entry": px,
            "sl": round(sl, 6),
            "tp": round(tp, 6),
            "hl_response": result,
            "reason": None if accepted else (
                result.get("response", {}).get("data", {}).get("statuses", [{}])[0].get("error", "unknown")
            ),
        }
    except Exception as exc:
        logger.error("Order submission failed: %s", exc)
        return {"status": "error", "reason": str(exc)}


def get_positions() -> list:
    """Fetch live positions from Hyperliquid clearinghouse."""
    try:
        wallet = Account.from_key(HL_API_KEY)
        addr = wallet.address
        state = _info().clearinghouse_state(addr)
        positions = []
        for pos in state.get("assetPositions", []):
            p = pos.get("position", {})
            coin = p.get("coin", "")
            sz = float(p.get("szi", 0))
            side = "LONG" if sz > 0 else "SHORT"
            positions.append({
                "coin": coin,
                "side": side,
                "size": abs(sz),
                "entry_px": float(p.get("entryPx", 0)),
                "leverage": p.get("leverage", {}).get("value", 1),
            })
        return positions
    except Exception as exc:
        logger.error("Failed to fetch positions: %s", exc)
        return []


def get_account_value() -> dict:
    """Return total account value breakdown."""
    try:
        wallet = Account.from_key(HL_API_KEY)
        addr = wallet.address
        state = _info().clearinghouse_state(addr)
        return {
            "total": float(state.get("marginSummary", {}).get("accountValue", 0)),
            "perp": float(state.get("marginSummary", {}).get("totalMarginUsed", 0)),
        }
    except Exception as exc:
        logger.error("Failed to fetch account value: %s", exc)
        return {"total": 0.0, "perp": 0.0}
