"""
Hyperliquid API Bridge.
Reads positions, funding rates, and mark prices.
API key loaded from ./.env
"""
import os, json, logging, time
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
import requests

logger = logging.getLogger(__name__)

BASE = "https://api.hyperliquid.xyz"

# ---------------------------------------------------------------------------
# Response cache (prevents redundant API calls during rapid polling)
# TTL in seconds — short enough for freshness, long enough for rate-limit safety
# ---------------------------------------------------------------------------
_CACHE: Dict[str, tuple] = {}  # key -> (timestamp, data)
CACHE_TTL_SEC = 1.0            # 1-second cache for position/mark data
META_CACHE_TTL = 3.0           # 3-second for relatively-static metadata
_STATE_CACHE_TTL = 1.0         # 1-second for clearinghouse state
_LAST_CALL_TIME = 0.0
_MIN_CALL_INTERVAL = 0.5       # throttle: max 2 calls per second to HL

def _get_cache(key: str, ttl: float = CACHE_TTL_SEC):
    """Return cached data if still fresh, else None."""
    if key not in _CACHE:
        return None
    ts, data = _CACHE[key]
    if (time.monotonic() - ts) >= ttl:
        del _CACHE[key]
        return None
    return data

def _set_cache(key: str, data: Any):
    """Store result in cache with current timestamp."""
    _CACHE[key] = (time.monotonic(), data)

def _throttle():
    """Enforce minimum interval between HTTP calls to HL."""
    global _LAST_CALL_TIME
    elapsed = time.monotonic() - _LAST_CALL_TIME
    if elapsed < _MIN_CALL_INTERVAL:
        time.sleep(_MIN_CALL_INTERVAL - elapsed)
    _LAST_CALL_TIME = time.monotonic()

def _post(endpoint: str, payload: dict, timeout: int = 15) -> requests.Response:
    """Throttled POST to Hyperliquid with 429 handling."""
    _throttle()
    try:
        r = requests.post(f"{BASE}{endpoint}", json=payload, timeout=timeout)
        return r
    except requests.exceptions.ConnectionError as exc:
        logger.warning("HL connection error: %s", exc)
        raise
    except requests.exceptions.Timeout as exc:
        logger.warning("HL timeout: %s", exc)
        raise


def _env():
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

def _key() -> Optional[str]:
    _env()
    return os.environ.get("HL_API_KEY")


# ---------------------------------------------------------------------------
# Cached fetchers
# ---------------------------------------------------------------------------

def get_funding_rates() -> List[Dict[str, Any]]:
    cached = _get_cache("funding_rates", META_CACHE_TTL)
    if cached is not None:
        return cached

    try:
        r = _post("/info", {"type": "metaAndAssetCtxs"}, timeout=15)
        if r.status_code == 429:
            logger.warning("HL rate limit (429) metaAndAssetCtxs — retrying once after 2s")
            time.sleep(2)
            r = _post("/info", {"type": "metaAndAssetCtxs"}, timeout=15)
        if r.status_code == 429:
            logger.warning("HL rate limit (429) persists — returning cached")
            return _CACHE.get("funding_rates", (0, []))[1] if "funding_rates" in _CACHE else []
        r.raise_for_status()
        data = r.json()
        meta, ctxs = data
        perps = meta["universe"]
        out = []
        for idx, p in enumerate(perps):
            ctx = ctxs[idx]
            out.append({
                "coin": p["name"],
                "funding": float(ctx.get("funding", 0)),
                "mark": float(ctx.get("markPx", 0)),
                "open_interest": float(ctx.get("openInterest", 0)),
                "dayNtlVlm": float(ctx.get("dayNtlVlm", 0)),
            })
        _set_cache("funding_rates", out)
        return out
    except Exception as exc:
        logger.warning("HL funding fetch failed: %s", exc)
        return _CACHE.get("funding_rates", (0, []))[1] if "funding_rates" in _CACHE else []


def get_open_orders(address: str = "") -> List[Dict[str, Any]]:
    """Query open orders for the wallet."""
    _env()
    addr = address or os.environ.get("HL_ADDRESS", "")
    if not addr:
        return []
    cache_key = f"open_orders_{addr}"
    cached = _get_cache(cache_key, _STATE_CACHE_TTL)
    if cached is not None:
        return cached
    try:
        r = _post("/info", {"type": "openOrders", "user": addr}, timeout=15)
        if r.status_code == 429:
            time.sleep(2)
            r = _post("/info", {"type": "openOrders", "user": addr}, timeout=15)
        if r.status_code == 429:
            return _CACHE.get(cache_key, (0, []))[1] if cache_key in _CACHE else []
        r.raise_for_status()
        data = r.json()
        out = []
        for o in data if isinstance(data, list) else []:
            out.append({
                "coin": o.get("coin", ""),
                "side": o.get("side", ""),  # "B" = Buy/Long, "A" = Ask/Short
                "limitPx": float(o.get("limitPx", 0)),
                "sz": float(o.get("sz", 0)),
                "oid": o.get("oid"),
                "timestamp": o.get("timestamp", 0),
                "origSz": float(o.get("origSz", 0)),
            })
        _set_cache(cache_key, out)
        return out
    except Exception as exc:
        logger.warning("HL openOrders fetch failed: %s", exc)
        return _CACHE.get(cache_key, (0, []))[1] if cache_key in _CACHE else []


def get_account_value(address: str = "") -> dict:
    """Return unified perp + spot account value for unified margin accounts.
    Uses last-known-good total if one leg fails to prevent false drawdown halts."""
    _env()
    addr = address or os.environ.get("HL_ADDRESS", "")
    if not addr:
        return {"perp": 0.0, "spot": 0.0, "total": 0.0}

    cache_key = f"account_value_{addr}"
    cached = _get_cache(cache_key, _STATE_CACHE_TTL)
    last_good = _CACHE.get(cache_key + "_last_good")
    if last_good is not None:
        last_good = last_good[1]  # (timestamp, data)

    perp_ok = False
    spot_ok = False
    perp_val = 0.0
    spot_val = 0.0
    spot_held = 0.0

    # --- Perp fetch ---
    try:
        r = _post("/info", {"type": "clearinghouseState", "user": addr}, timeout=15)
        if r.status_code == 429:
            time.sleep(2)
            r = _post("/info", {"type": "clearinghouseState", "user": addr}, timeout=15)
        if r.status_code != 429:
            r.raise_for_status()
            data = r.json()
            perp_val = float(data.get("marginSummary", {}).get("accountValue", "0"))
            perp_ok = True
    except Exception as exc:
        logger.warning("Perp account value fetch failed: %s", exc)

    # --- Spot fetch ---
    try:
        r2 = _post("/info", {"type": "spotClearinghouseState", "user": addr}, timeout=15)
        if r2.status_code == 429:
            time.sleep(2)
            r2 = _post("/info", {"type": "spotClearinghouseState", "user": addr}, timeout=15)
        if r2.status_code != 429:
            r2.raise_for_status()
            data2 = r2.json()
            for bal in data2.get("balances", []):
                if bal.get("coin") == "USDC":
                    spot_val = float(bal.get("total", "0"))
                    spot_held = float(bal.get("hold", "0"))
                    spot_ok = True
                    break
    except Exception as exc:
        logger.warning("Spot account value fetch failed: %s", exc)

    # --- Robust total calculation ---
    if perp_ok and spot_ok:
        total = perp_val + (spot_val - spot_held)
    elif perp_ok and not spot_ok:
        # Spot missing: use perp + last known spot_available, or just perp if no history
        if last_good:
            total = perp_val + last_good.get("spot_available", 0)
            logger.info("Spot fetch failed — using cached spot_available %.2f", last_good.get("spot_available", 0))
        else:
            total = perp_val
    elif not perp_ok and spot_ok:
        # Perp missing: impossible to know uPnL, use last known total or spot as floor
        if last_good:
            total = last_good.get("total", spot_val)
            logger.info("Perp fetch failed — using last known total %.2f", total)
        else:
            total = spot_val
    else:
        # Both failed: return last known good total, or 0
        if last_good:
            total = last_good.get("total", 0)
            logger.warning("Both fetches failed — returning cached total %.2f", total)
        else:
            total = 0.0

    result = {
        "perp": round(perp_val, 2),
        "spot": round(spot_val, 2),
        "spot_held": round(spot_held, 2),
        "spot_available": round(spot_val - spot_held, 2),
        "total": round(total, 2),
        "perp_ok": perp_ok,
        "spot_ok": spot_ok,
    }
    _set_cache(cache_key, result)
    if total > 0:
        _set_cache(cache_key + "_last_good", result)
    return result


def get_positions(address: str = "") -> List[Dict[str, Any]]:
    _env()
    addr = address or os.environ.get("HL_ADDRESS", "")
    if not addr:
        logger.warning("No HL address configured")
        return []

    cache_key = f"positions_{addr}"
    cached = _get_cache(cache_key, _STATE_CACHE_TTL)
    if cached is not None:
        return cached

    try:
        marks = get_all_marks()
        r = _post("/info", {"type": "clearinghouseState", "user": addr}, timeout=15)
        if r.status_code == 429:
            logger.warning("HL rate limit (429) clearinghouseState positions — retrying once after 2s")
            time.sleep(2)
            r = _post("/info", {"type": "clearinghouseState", "user": addr}, timeout=15)
        if r.status_code == 429:
            logger.warning("HL rate limit (429) persists positions")
            return _CACHE.get(cache_key, (0, []))[1] if cache_key in _CACHE else []
        r.raise_for_status()
        data = r.json()
        pos_list = data.get("assetPositions", [])
        out = []
        for p in pos_list:
            pos = p.get("position", {})
            coin = pos.get("coin", "")
            sz = float(pos.get("szi", 0))
            entry = float(pos.get("entryPx", 0))
            mark = marks.get(coin, 0.0)  # Use real mark price from metaAndAssetCtxs
            side = "LONG" if sz > 0 else "SHORT"
            unrealized = (mark - entry) * abs(sz) if sz and mark else 0
            if side == "SHORT":
                unrealized = -unrealized
            out.append({
                "coin": coin,
                "side": side,
                "size": round(abs(sz), 6),
                "entry_px": round(entry, 6),
                "mark_px": round(mark, 6),
                "unrealized_pnl": round(unrealized, 4),
                "leverage": float(pos.get("leverage", {}).get("value", 1)),
            })
        _set_cache(cache_key, out)
        return out
    except Exception as exc:
        logger.warning("HL positions fetch failed: %s", exc)
        return _CACHE.get(cache_key, (0, []))[1] if cache_key in _CACHE else []


def get_all_marks() -> Dict[str, float]:
    cached = _get_cache("all_marks", META_CACHE_TTL)
    if cached is not None:
        return cached

    out = {}
    for fr in get_funding_rates():
        out[fr["coin"]] = fr["mark"]
    _set_cache("all_marks", out)
    return out


def run_cycle() -> dict:
    funding = get_funding_rates()
    marks = {fr["coin"]: fr["mark"] for fr in funding}
    return {
        "funding_rates": funding,
        "marks": marks,
        "dt": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_cycle()
    print(json.dumps({"funding_count": len(result["funding_rates"]), "marks_count": len(result["marks"])}, indent=2))
    sys.exit(0)
