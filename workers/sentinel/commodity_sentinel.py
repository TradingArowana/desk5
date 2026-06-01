"""
Commodity Sentinel for Desk5.
Fetches Brent (BZ=F) and WTI (CL=F) spot prices from Yahoo Finance,
pulls PAXG price via CoinGecko, caches results to data_store/commodities.json
with a 30-minute TTL, and computes PAXG vs Brent deviation.
"""
import json
import sys
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional
import requests

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
STATE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = STATE_DIR / "commodities.json"

TTL_SECONDS = 30 * 60  # 30 minutes

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d\u0026range=1d"
PAXG_URL = "https://api.coingecko.com/api/v3/simple/price?ids=pax-gold\u0026vs_currencies=usd"

# Use yfinance if available, otherwise fallback to direct requests
_HAS_YFINANCE = False
try:
    import yfinance as yf  # type: ignore[import]
    _HAS_YFINANCE = True
except Exception:
    pass


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fetch_yahoo_via_requests(symbol: str) -> Dict[str, Any]:
    url = YAHOO_CHART_URL.format(symbol=symbol)
    r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 (Desk5-Sentinel)"})
    r.raise_for_status()
    payload = r.json()
    result = payload.get("chart", {}).get("result", [None])[0]
    if result is None:
        logger.warning("No Yahoo chart result for %s", symbol)
        return {}
    meta = result.get("meta", {})
    price = meta.get("regularMarketPrice")
    out: Dict[str, Any] = {
        "price": float(price) if price is not None else None,
        "prev_close": float(meta["chartPreviousClose"]) if meta.get("chartPreviousClose") is not None else None,
        "day_high": float(meta["regularMarketDayHigh"]) if meta.get("regularMarketDayHigh") is not None else None,
        "day_low": float(meta["regularMarketDayLow"]) if meta.get("regularMarketDayLow") is not None else None,
    }
    return out


def _fetch_yahoo_via_yfinance(symbol: str) -> Dict[str, Any]:
    ticker = yf.Ticker(symbol)
    info = ticker.info or {}
    price = info.get("regularMarketPrice") or info.get("previousClose") or info.get("bid")
    out: Dict[str, Any] = {
        "price": float(price) if price is not None else None,
        "prev_close": float(info["previousClose"]) if info.get("previousClose") is not None else None,
        "day_high": float(info["regularMarketDayHigh"]) if info.get("regularMarketDayHigh") is not None else None,
        "day_low": float(info["regularMarketDayLow"]) if info.get("regularMarketDayLow") is not None else None,
    }
    if out["price"] is None:
        hist = ticker.history(period="1d", interval="1d")
        if not hist.empty:
            out["price"] = float(hist["Close"].iloc[-1])
    return out


def fetch_yahoo_price(symbol: str) -> Dict[str, Any]:
    """Fetch the latest price + metadata for a Yahoo Finance symbol."""
    try:
        if _HAS_YFINANCE:
            return _fetch_yahoo_via_yfinance(symbol)
        return _fetch_yahoo_via_requests(symbol)
    except Exception as exc:
        logger.warning("Yahoo fetch failed for %s: %s", symbol, exc)
        return {}


def fetch_paxg_price() -> Optional[float]:
    """Fetch PAXG USD price from CoinGecko."""
    try:
        r = requests.get(PAXG_URL, timeout=8, headers={"User-Agent": "Mozilla/5.0 (Desk5-Sentinel)"})
        r.raise_for_status()
        payload = r.json()
        price = payload.get("pax-gold", {}).get("usd")
        return float(price) if price is not None else None
    except Exception as exc:
        logger.warning("CoinGecko fetch failed for PAXG: %s", exc)
        return None


def load_cache() -> Optional[Dict[str, Any]]:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text())
        cached_dt = datetime.fromisoformat(data.get("dt", "1970-01-01T00:00:00+00:00"))
        age_seconds = (_now_utc() - cached_dt).total_seconds()
        if age_seconds < TTL_SECONDS:
            return data
    except Exception as exc:
        logger.warning("Cache read failed: %s", exc)
    return None


def save_cache(record: Dict[str, Any]) -> None:
    try:
        CACHE_FILE.write_text(json.dumps(record, indent=2))
    except Exception as exc:
        logger.warning("Cache write failed: %s", exc)


def compute_paxg_spread_pct(paxg: Optional[float], brent: Optional[float]) -> Optional[float]:
    if paxg is None or brent is None or brent == 0:
        return None
    return round(((paxg - brent) / brent) * 100, 2)


def run_cycle() -> Dict[str, Any]:
    cached = load_cache()
    if cached is not None:
        logger.info("Using cached commodity data (age < %ds)", TTL_SECONDS)
        # Still recompute paxg_spread_pct in case formula changes
        cached["paxg_spread_pct"] = compute_paxg_spread_pct(
            cached.get("paxg"), cached.get("brent")
        )
        return cached

    brent_data = fetch_yahoo_price("BZ=F")
    wti_data = fetch_yahoo_price("CL=F")
    paxg = fetch_paxg_price()

    brent = brent_data.get("price")
    wti = wti_data.get("price")

    brent_change = None
    if brent is not None and brent_data.get("prev_close"):
        brent_change = round(((brent - brent_data["prev_close"]) / brent_data["prev_close"]) * 100, 2)
    wti_change = None
    if wti is not None and wti_data.get("prev_close"):
        wti_change = round(((wti - wti_data["prev_close"]) / wti_data["prev_close"]) * 100, 2)

    spread = round(brent - wti, 2) if (brent is not None and wti is not None) else None

    record: Dict[str, Any] = {
        "brent": brent,
        "brent_change_pct": brent_change,
        "brent_day_high": brent_data.get("day_high"),
        "brent_day_low": brent_data.get("day_low"),
        "wti": wti,
        "wti_change_pct": wti_change,
        "wti_day_high": wti_data.get("day_high"),
        "wti_day_low": wti_data.get("day_low"),
        "spread": spread,
        "paxg": paxg,
        "paxg_spread_pct": compute_paxg_spread_pct(paxg, brent),
        "dt": _now_utc().isoformat(),
    }

    save_cache(record)
    return record


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_cycle()
    print(json.dumps(result))
    sys.exit(0)
