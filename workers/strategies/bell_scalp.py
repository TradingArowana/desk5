"""
Bell Window Scalper for PAXG.

During market open/close bells, gold volatility surges. This module
fetches 1-minute candles from Binance (free, no key) and generates
ultra-fast momentum signals with tight risk parameters.

Bell schedule (UTC, ±15 min windows):
  London Open   08:00  (07:45 – 08:15)
  NY Open       13:30  (13:15 – 13:45)  ← highest vol
  London Close  16:00  (15:45 – 16:15)
  NY Close      21:00  (20:45 – 21:15)

Outside bells: falls back to hourly gold_scalp.
"""
import json
import logging
import math
from datetime import datetime, timezone, time as dt_time
from pathlib import Path
from typing import List, Dict, Any

import requests

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
STATE_DIR.mkdir(parents=True, exist_ok=True)
SIGNALS_FILE = STATE_DIR / "gold_scalp_signals.json"
BELL_SIGNALS_FILE = STATE_DIR / "bell_scalp_signals.json"
# 1-minute cache for Binance candles
BELL_CACHE = STATE_DIR / "paxg_binance_1m.json"

# ── Bell schedule: (hour, minute, name, rank) ──
# rank: higher = more volatile / more important
BELLS = [
    ( 8,  0, "london_open",  3),
    (13, 30, "ny_open",      5),  # highest vol
    (16,  0, "london_close", 2),
    (21,  0, "ny_close",     3),
]
BELL_WINDOW_MIN = 15   # ±15 minutes around bell

# ── Fast risk params ──
FAST_LOOKBACK = 10            # 10 × 1m candles = 10 min momentum
FAST_MOMENTUM_THRESHOLD = 0.0005  # 0.05% → easier trigger during bursts
FAST_SL_PCT = 0.001           # 0.1% stop
FAST_TP_PCT = 0.002           # 0.2% target (2:1 R/R)
FAST_LEVERAGE = 10
FAST_MAX_TRADES_PER_BELL = 3  # avoid overtrading single burst

# Simple Signal class (copy to avoid import issues)
class Signal:
    def __init__(self, coin: str, direction: str, entry_px: float, sl_px: float,
                 tp_px: float, dt: str, confidence: int = 50, size_usd: float = 20.0,
                 status: str = "OPEN", rationale: str = "", signal_type: str = "bell_scalp"):
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_active_bell(now: datetime = None) -> tuple:
    """
    Returns (bell_name, rank, minutes_to_bell, window_active) if we are inside
    a bell window. Otherwise returns (None, 0, 0, False).
    """
    if now is None:
        now = _now()

    closest = None
    min_delta = float('inf')

    for h, m, name, rank in BELLS:
        bell_time = dt_time(h, m)
        # Create a datetime for today
        bell_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        # If bell already passed today, check tomorrow for distance calc
        if bell_dt < now:
            # For distance to next same bell, add 1 day
            pass

        delta_min = abs((now - bell_dt).total_seconds() / 60.0)

        if delta_min < min_delta:
            min_delta = delta_min
            closest = (name, rank, delta_min)

    if closest and min_delta <= BELL_WINDOW_MIN:
        return closest[0], closest[1], min_delta, True
    return None, 0, 0, False


def fetch_binance_1m(limit: int = 60) -> List[Dict]:
    """
    Fetch 1-minute candles from Binance spot (PAXGUSDT).
    No API key required.
    Returns list of {dt, open, high, low, close, volume}.
    """
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": "PAXGUSDT", "interval": "1m", "limit": limit}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        candles = []
        for item in data:
            # Binance kline: [open_time, open, high, low, close, volume, close_time, ...]
            ts_ms = item[0]
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            candles.append({
                "dt": dt,
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]),
                "ts": ts_ms,
            })
        # Cache
        BELL_CACHE.write_text(json.dumps([{"ts": c["ts"], "close": c["close"], "volume": c["volume"]} for c in candles], indent=2))
        return candles
    except Exception:
        logger.exception("Binance 1m fetch failed")
        # Try cache fallback
        if BELL_CACHE.exists():
            try:
                raw = json.loads(BELL_CACHE.read_text())
                candles = []
                for item in raw:
                    candles.append({"dt": datetime.fromtimestamp(item["ts"] / 1000, tz=timezone.utc), "close": item["close"], "volume": item["volume"], "ts": item["ts"]})
                return candles
            except Exception:
                pass
        return []


def calculate_fast_momentum(candles: List[Dict], lookback: int = FAST_LOOKBACK) -> Dict:
    """
    Calculate momentum over the last `lookback` 1m candles.
    Also compute volume surge factor.
    Returns dict with momentum, avg_volume, latest_close.
    """
    if len(candles) < lookback + 2:
        return {"momentum": 0.0, "avg_volume": 0.0, "latest_close": 0.0, "valid": False}

    # Use close prices
    latest = candles[-1]["close"]
    past = candles[-lookback - 1]["close"]
    mom = (latest - past) / past if past else 0.0

    # Volume surge: recent avg vs older avg
    recent_vol = sum(c["volume"] for c in candles[-lookback:]) / lookback
    if len(candles) >= lookback * 2:
        older_vol = sum(c["volume"] for c in candles[-lookback*2:-lookback]) / lookback
        vol_surge = recent_vol / older_vol if older_vol > 0 else 1.0
    else:
        vol_surge = 1.0

    return {
        "momentum": mom,
        "avg_volume": recent_vol,
        "vol_surge": vol_surge,
        "latest_close": latest,
        "valid": True,
        "window_label": f"{lookback}m"
    }


def _trade_count_for_bell(bell_name: str, today: str) -> int:
    """Count how many bell signals already generated today for this bell window."""
    if not BELL_SIGNALS_FILE.exists():
        return 0
    try:
        raw = json.loads(BELL_SIGNALS_FILE.read_text())
        count = 0
        for entry in raw:
            if entry.get("bell_name") == bell_name and entry.get("trade_date") == today:
                count += 1
        return count
    except Exception:
        return 0


def generate_bell_signals(watchlist: List[Dict[str, Any]] = None) -> List[Signal]:
    """
    Main entry point. Returns signals only if inside a bell window AND
    momentum threshold is breached.
    """
    now = _now()
    bell_name, rank, delta_min, active = get_active_bell(now)

    if not active:
        return []

    # Fetch 1m candles
    candles = fetch_binance_1m(limit=60)
    if len(candles) < FAST_LOOKBACK + 2:
        logger.warning("BellScalp: insufficient 1m data (got %d, need %d)", len(candles), FAST_LOOKBACK + 2)
        return []

    # Momentum
    mom_data = calculate_fast_momentum(candles)
    if not mom_data["valid"]:
        return []

    mom = mom_data["momentum"]
    current_price = mom_data["latest_close"]

    # Direction
    if mom > FAST_MOMENTUM_THRESHOLD:
        direction = "LONG"
    elif mom < -FAST_MOMENTUM_THRESHOLD:
        direction = "SHORT"
    else:
        logger.debug("BellScalp: momentum %.4f%% inside %s but below threshold", mom * 100, bell_name)
        return []

    # Size based on bell rank (higher rank = more conviction)
    base_size = 20.0
    size_usd = base_size * (1 + rank * 0.15)  # rank 5 => +75% size
    size_usd = min(size_usd, 50.0)  # cap at $50 per trade

    # Calculate SL/TP
    if direction == "LONG":
        sl_px = current_price * (1 - FAST_SL_PCT)
        tp_px = current_price * (1 + FAST_TP_PCT)
    else:
        sl_px = current_price * (1 + FAST_SL_PCT)
        tp_px = current_price * (1 - FAST_TP_PCT)

    today_str = now.strftime("%Y-%m-%d")
    trade_count = _trade_count_for_bell(bell_name, today_str)
    if trade_count >= FAST_MAX_TRADES_PER_BELL:
        logger.info("BellScalp: already generated %d/%d trades for %s today, skipping",
                    trade_count, FAST_MAX_TRADES_PER_BELL, bell_name)
        return []

    # Cooldown: at least 5 min between bell signals
    last_bell = STATE_DIR / "last_bell_signal.json"
    if last_bell.exists():
        try:
            lb = json.loads(last_bell.read_text())
            last_dt = datetime.fromisoformat(lb.get("dt", "1970-01-01T00:00:00+00:00"))
            if (now - last_dt).total_seconds() < 300:
                logger.debug("Bell cooldown: %ds since last signal, skipping", (now - last_dt).total_seconds())
                return []
        except Exception:
            pass

    signal = Signal(
        coin="PAXG",
        direction=direction,
        entry_px=round(current_price, 2),
        sl_px=round(sl_px, 2),
        tp_px=round(tp_px, 2),
        dt=now.isoformat(),
        confidence=round(min(abs(mom) / 0.002 * 100, 100)),
        size_usd=round(size_usd, 2),
        rationale=(f"Bell window: {bell_name} (rank={rank}, {delta_min:.0f}m from bell). "
                   f"Fast momentum: {mom*100:+.3f}% over {FAST_LOOKBACK}m. "
                   f"Vol surge: {mom_data['vol_surge']:.1f}x. "
                   f"Trade #{trade_count+1}/{FAST_MAX_TRADES_PER_BELL}."),
        signal_type="bell_scalp",
    )

    # Persist with metadata
    existing = []
    if BELL_SIGNALS_FILE.exists():
        try:
            existing = json.loads(BELL_SIGNALS_FILE.read_text())
        except Exception:
            pass

    entry = signal.__dict__.copy()
    entry["bell_name"] = bell_name
    entry["rank"] = rank
    entry["trade_date"] = today_str
    entry["minutes_from_bell"] = round(delta_min, 1)
    existing.append(entry)

    # Keep only last 500 entries
    if len(existing) > 500:
        existing = existing[-500:]

    BELL_SIGNALS_FILE.write_text(json.dumps(existing, indent=2))

    # Write bell cooldown
    last_bell = STATE_DIR / "last_bell_signal.json"
    last_bell.write_text(json.dumps({"dt": now.isoformat(), "bell_name": bell_name}))

    logger.info("BELL SIGNAL [%s]: %s %s @ %.2f SL=%.2f TP=%.2f (mom=%+.3f%%) %s",
                bell_name, signal.coin, signal.direction, signal.entry_px,
                signal.sl_px, signal.tp_px, mom * 100, signal.rationale)

    return [signal]


def is_bell_window(now: datetime = None) -> bool:
    """Utility: are we inside any bell window?"""
    _, _, _, active = get_active_bell(now)
    return active


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sigs = generate_bell_signals()
    for s in sigs:
        print(s)
