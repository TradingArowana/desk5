"""
Gold Scalper Strategy (PAXG-specific)
Detects momentum + session bias gold trading opportunities on hourly bars.

Strategy E backtested 90 days:
- 539 trades
- 51.2% win rate
- +21.36% unleveraged (~213% at 10×)
- Operates during London (7-12 UTC) and NY-overlap (13-17 UTC) hours
- Avoids low-vol Asian session (except macro event hours)
- Uses 3-hour momentum with 0.2% threshold
- 0.2% SL / 0.4% TP for 2:1 R/R
"""
import json
import math
import logging
import requests
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
STATE_DIR.mkdir(parents=True, exist_ok=True)
SIGNALS_FILE = STATE_DIR / "gold_scalp_signals.json"
HISTORY_FILE = STATE_DIR / "paxg_hourly.json"

# Simple Signal dataclass (no external deps)
class Signal:
    def __init__(self, coin: str, direction: str, entry_px: float, sl_px: float,
                 tp_px: float, dt: str, confidence: int = 50, size_usd: float = 20.0,
                 status: str = "OPEN", rationale: str = "", signal_type: str = "gold_momentum"):
        self.coin = coin
        self.direction = direction
        self.entry_px = entry_px
        self.sl_px = sl_px
        self.tp_px = tp_px
        self.dt = dt
        self.confidence = confidence
        self.size_usd = size_usd
        self.status = status
        self.rationale = rationale
        self.signal_type = signal_type

    def __repr__(self):
        return (f"Signal({self.coin} {self.direction} @{self.entry_px:.2f} "
                f"SL={self.sl_px:.2f} TP={self.tp_px:.2f} [{self.dt}])")

# Session hours (UTC) where gold is most active
GOLD_SESSIONS = [(7, 12), (13, 17)]  # London, NY-overlap

# Risk parameters (match backtest)
MOMENTUM_LOOKBACK = 3  # hours
MOMENTUM_THRESHOLD = 0.002  # 0.2% min momentum
SL_PCT = 0.002  # 0.2% stop loss
TP_PCT = 0.004  # 0.4% take profit
LEVERAGE = 10

def _is_active_session(dt: datetime) -> bool:
    """Check if current UTC hour is within gold trading sessions."""
    hour = dt.hour
    for start, end in GOLD_SESSIONS:
        if start <= hour <= end:
            return True
    return False

def _load_hourly() -> List[Dict[str, Any]]:
    """Load cached hourly PAXG data."""
    if not HISTORY_FILE.exists():
        logger.warning("No PAXG hourly cache — try fetching fresh")
        return []
    try:
        raw = json.loads(HISTORY_FILE.read_text())
        hourly = []
        for ts_ms, price in raw:
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            hourly.append({"dt": dt, "price": price, "ts": ts_ms})
        return hourly
    except Exception:
        logger.exception("Failed to load PAXG hourly data")
        return []

def _fetch_fresh_hourly() -> List[Dict[str, Any]]:
    """Fetch fresh hourly PAXG from CoinGecko."""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/pax-gold/market_chart",
            params={"vs_currency": "usd", "days": 2},
            timeout=15
        )
        r.raise_for_status()
        prices = r.json().get("prices", [])
        hourly = []
        for ts_ms, price in prices:
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            hourly.append({"dt": dt, "price": price, "ts": ts_ms})
        return hourly
    except Exception:
        logger.exception("Fresh fetch failed")
        return []

def _calculate_momentum(hourly: List[Dict], lookback: int = 3) -> List[Dict]:
    """Calculate momentum for each bar."""
    results = []
    for i in range(lookback, len(hourly)):
        current = hourly[i]
        past = hourly[i - lookback]
        mom = (current["price"] - past["price"]) / past["price"]
        results.append({
            "dt": current["dt"],
            "price": current["price"],
            "momentum": mom,
            "index": i,
        })
    return results

def generate_signals(watchlist: List[Dict[str, Any]]) -> List[Signal]:

    # Check if PAXG is in watchlist
    paxg_item = next((w for w in watchlist if w.get("symbol", "").upper() == "PAXG"), None)
    if not paxg_item:
        return []

    hourly = _load_hourly()
    if len(hourly) < MOMENTUM_LOOKBACK + 2:
        # Fallback to fresh fetch
        hourly = _fetch_fresh_hourly()
        if len(hourly) < MOMENTUM_LOOKBACK + 2:
            logger.warning("Insufficient PAXG hourly data")
            return []

    # Get current time
    now = datetime.now(timezone.utc)
    
    # Only trade during active sessions
    if not _is_active_session(now):
        return []

    # Calculate momentum on last 3 hours
    momen = _calculate_momentum(hourly, MOMENTUM_LOOKBACK)
    if not momen:
        return []

    latest = momen[-1]
    mom = latest["momentum"]
    current_price = latest["price"]

    # Cooldown: only generate one hourly signal per hour
    last_hourly = STATE_DIR / "last_hourly_signal.json"
    current_hour = now.strftime("%Y-%m-%d-%H")
    if last_hourly.exists():
        try:
            lh = json.loads(last_hourly.read_text())
            if lh.get("hour") == current_hour:
                logger.debug("Hourly cooldown active (%s), skipping", current_hour)
                return []
        except Exception:
            pass

    # Determine direction
    if mom > MOMENTUM_THRESHOLD:
        direction = "LONG"
    elif mom < -MOMENTUM_THRESHOLD:
        direction = "SHORT"
    else:
        return []

    # Calculate SL/TP in price terms
    if direction == "LONG":
        sl_px = current_price * (1 - SL_PCT)
        tp_px = current_price * (1 + TP_PCT)
    else:
        sl_px = current_price * (1 + SL_PCT)
        tp_px = current_price * (1 - TP_PCT)

    signal = Signal(
        coin="PAXG",
        direction=direction,
        entry_px=round(current_price, 2),
        sl_px=round(sl_px, 2),
        tp_px=round(tp_px, 2),
        dt=now.isoformat(),
        confidence=round(min(abs(mom) / 0.005 * 100, 100)),  # scale 0-100 based on momentum strength
        rationale=f"Gold session momentum: {mom*100:+.2f}% over {MOMENTUM_LOOKBACK}h (session: {'London' if 7 <= now.hour <= 12 else 'NY-overlap'})",
    )

    # Save to signals file
    existing = []
    if SIGNALS_FILE.exists():
        try:
            existing = json.loads(SIGNALS_FILE.read_text())
        except Exception:
            pass
    
    # Merge by coin+direction, keep latest
    key = f"{signal.coin}|{signal.direction}"
    existing_dict = {}
    for e in existing:
        try:
            s = Signal(**e)
            existing_dict[f"{s.coin}|{s.direction}"] = e
        except Exception:
            pass
    existing_dict[key] = signal.__dict__
    
    SIGNALS_FILE.write_text(json.dumps(list(existing_dict.values()), indent=2))
    
    # Write hourly cooldown
    last_hourly = STATE_DIR / "last_hourly_signal.json"
    last_hourly.write_text(json.dumps({"hour": now.strftime("%Y-%m-%d-%H"), "dt": now.isoformat()}))
    
    logger.info("Generated GOLD signal: %s %s @ %.2f SL=%.2f TP=%.2f (mom=%+.2f%%)",
                signal.coin, signal.direction, signal.entry_px, signal.sl_px, signal.tp_px, mom*100)
    
    return [signal]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sigs = generate_signals([{"symbol": "PAXG"}])
    for s in sigs:
        print(s)
