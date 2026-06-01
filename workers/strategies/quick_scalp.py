"""
QuickScalp strategy engine for desk5.
Operates on the long-tail watchlist produced by alpha_scanner.
- 5m/15m micro-range breakout signals (both long & short)
- Fixed $20 / trade, 1k bankroll → 2% risk
- SL 3%, TP 6% (2:1) or trailing after +3%
- 3–5 trades/day target
"""

from __future__ import annotations

import json
import math
import os
import random
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests

from workers.strategies.signal_aggregator import save_to_queue
from utils.logger import get_logger

logger = get_logger("quick_scalp")

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
STATE_DIR.mkdir(parents=True, exist_ok=True)
SIGNALS_PATH = STATE_DIR / "quick_scalp_signals.json"
TRADES_PATH = STATE_DIR / "quick_scalp_trades.json"

COINGECKO_OHLC_URL = "https://api.coingecko.com/api/v3/coins/{id}/ohlc"
COINGECKO_IDS_URL = "https://api.coingecko.com/api/v3/coins/list"

# ---------------------------------------------------------------------------
# Position / signal models
# ---------------------------------------------------------------------------
@dataclass
class Signal:
    coin: str
    direction: str          # "LONG" | "SHORT"
    entry_px: float
    sl_px: float
    tp_px: float
    dt: str                # ISO
    status: str = "OPEN"   # OPEN | FILLED | CLOSED | CANCELLED
    size_usd: float = 20.0

@dataclass
class Trade:
    coin: str
    direction: str
    entry_px: float
    exit_px: float
    pnl_pct: float
    pnl_usd: float
    duration_min: int
    exit_reason: str       # TP | SL | TRAIL
    dt: str

# ---------------------------------------------------------------------------
# Price data (paper / mock feed using CG OHLC)
# ---------------------------------------------------------------------------
_CG_IDS: Dict[str, str] = {}
_CG_OHLC_CACHE: Dict[str, tuple[float, List[List[float]]]] = {}  # coin_id: (timestamp, data)

OHLC_CACHE_TTL = 300.0  # 5min
OHLC_SLEEP = 6.0  # free tier ~10 req/min

# Top-25 correct CoinGecko IDs (symbols that map to the real tokens we trade on HL)
_CORRECT_CG_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "DOGE": "dogecoin",
    "AVAX": "avalanche-2", "LINK": "chainlink", "BNB": "binancecoin", "XRP": "ripple",
    "ADA": "cardano", "DOT": "polkadot", "ATOM": "cosmos", "CRV": "curve-dao-token",
    "LDO": "lido-dao", "GMX": "gmx", "SUI": "sui", "PAXG": "pax-gold",
    "AAVE": "aave", "COMP": "compound-governance-token", "TRX": "tron",
    "APE": "apecoin", "APT": "aptos", "BCH": "bitcoin-cash", "SNX": "havven",
    "INJ": "injective-protocol", "STX": "blockstack", "PEPE": "pepe",
    "OP": "optimism", "ARB": "arbitrum", "WBTC": "wrapped-bitcoin",
    "WETH": "weth", "USDC": "usd-coin", "USDT": "tether",
}

def _load_cg_ids() -> Dict[str, str]:
    """Return hardcoded + cached CG IDs. Only fetches from API for unknown symbols."""
    global _CG_IDS
    if _CG_IDS:
        return _CG_IDS
    # Start with hardcoded whitelist
    _CG_IDS = dict(_CORRECT_CG_IDS)
    # Attempt to augment from API, but don't overwrite hardcoded entries
    try:
        r = requests.get(COINGECKO_IDS_URL, timeout=30)
        r.raise_for_status()
        data = r.json()
        for c in data:
            sym = c.get("symbol", "").upper()
            cid = c.get("id", "")
            if sym and cid and sym not in _CG_IDS:
                _CG_IDS[sym] = cid
    except Exception:
        logger.debug("Failed to load CG coin list (non-critical, using whitelist)")
    return _CG_IDS

def fetch_ohlc_gc(coin_id: str, days: int = 1) -> List[List[float]]:
    if USE_DEMO_PRICES:
        return _demo_ohlc(coin_id)
    now = datetime.now(timezone.utc).timestamp()
    key = f"{coin_id}:{days}"
    cached = _CG_OHLC_CACHE.get(key)
    if cached and now - cached[0] < OHLC_CACHE_TTL:
        return cached[1]
    try:
        # Respect CG free-tier rate limit
        time.sleep(OHLC_SLEEP)
        r = requests.get(
            COINGECKO_OHLC_URL.format(id=coin_id),
            params={"vs_currency": "usd", "days": days},
            timeout=30,
        )
        if r.status_code == 429:
            logger.warning("CG rate limit for %s", coin_id)
            return _demo_ohlc()
        r.raise_for_status()
        data = r.json()
        _CG_OHLC_CACHE[key] = (now, data)
        return data
    except Exception as exc:
        logger.warning("CG OHLC fetch failed for %s: %s — using demo data", coin_id, exc)
        return _demo_ohlc()


def _demo_ohlc(coin_id: str = "") -> List[List[float]]:
    """Synthetic OHLC for testing when API offline."""
    import random, hashlib
    seed = int(hashlib.md5(coin_id.encode()).hexdigest(), 16) % (2**31)
    random.seed(seed)
    base = 100.0
    data: List[List[float]] = []
    ts = int(datetime.now(timezone.utc).timestamp()) - 86400
    for i in range(24):
        o = base + random.uniform(-2, 2)
        c = o + random.uniform(-1.5, 1.5)
        h = max(o, c) + random.uniform(0, 1)
        l = min(o, c) - random.uniform(0, 1)
        data.append([ts + i * 3600, round(o, 4), round(h, 4), round(l, 4), round(c, 4)])
        base = c
    # Breakout: last candle closes above prior high (exclude last candle for h1_high)
    prior_high = max(c[2] for c in data[:-1])
    data[-1][4] = round(prior_high * 1.02, 4)  # close above prior high
    data[-1][2] = round(data[-1][4] + 0.5, 4)
    return data

# Global demo mode flag
USE_DEMO_PRICES = os.environ.get("DESK_DEMO_PRICES", "0") == "1"

def micro_range_and_volume(ohlc: List[List[float]]) -> Dict[str, Any]:
    """
    Compute 1h high/low (last 12 × 5m candles approximated from last 12 points)
    and volume proxy (range × close).
    """
    recent = ohlc[-12:] if len(ohlc) >= 12 else ohlc
    if not recent:
        return {}
    highs = [c[2] for c in recent]
    lows = [c[3] for c in recent]
    closes = [c[4] for c in recent]
    opens = [c[1] for c in recent]
    vol_proxy = sum(abs(c[4] - c[1]) * c[4] for c in recent)
    return {
        "h1_high": max(highs),
        "h1_low": min(lows),
        "last_close": closes[-1],
        "vol_proxy": vol_proxy,
        "prior_close": closes[-2] if len(closes) > 1 else closes[-1],
        "avg_vol_proxy": vol_proxy / max(len(recent), 1),
    }

# ---------------------------------------------------------------------------
# Signal generator
# ---------------------------------------------------------------------------
def generate_signals(watchlist: List[Dict[str, Any]]) -> List[Signal]:
    cg_ids = _load_cg_ids() if not USE_DEMO_PRICES else {}
    signals: List[Signal] = []
    for coin in watchlist[:20]:
        sym = coin["symbol"].upper()
        cid = cg_ids.get(sym) or sym.lower()  # use symbol itself in demo
        ohlc = fetch_ohlc_gc(cid, days=1)
        if not ohlc or len(ohlc) < 4:
            continue
        m = micro_range_and_volume(ohlc)
        if not m:
            continue
        last = m["last_close"]
        # Exclude current candle when computing prior range for breakout test
        prior_ohlc = ohlc[-13:-1] if len(ohlc) >= 2 else ohlc
        prior_high = max(c[2] for c in prior_ohlc)
        prior_low = min(c[3] for c in prior_ohlc)
        # Volume spike — in demo always true
        if not USE_DEMO_PRICES and m["vol_proxy"] < m["avg_vol_proxy"] * 1.2:
            continue
        # Breakout above prior h1_high → LONG
        if last > prior_high * 1.001:
            sl = last * 0.97
            tp = last * 1.06
            signals.append(
                Signal(
                    coin=sym,
                    direction="LONG",
                    entry_px=round(last, 6),
                    sl_px=round(sl, 6),
                    tp_px=round(tp, 6),
                    dt=datetime.now(timezone.utc).isoformat(),
                )
            )
        # Breakout below prior h1_low → SHORT
        elif last < prior_low * 0.999:
            sl = last * 1.03
            tp = last * 0.94
            signals.append(
                Signal(
                    coin=sym,
                    direction="SHORT",
                    entry_px=round(last, 6),
                    sl_px=round(sl, 6),
                    tp_px=round(tp, 6),
                    dt=datetime.now(timezone.utc).isoformat(),
                )
            )
    logger.info("Generated %d QuickScalp signals", len(signals))
    return signals

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def _load_json(path: Path) -> List[Dict[str, Any]]:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return []

def _save_json(path: Path, data: List[Dict[str, Any]]) -> None:
    path.write_text(json.dumps(data, indent=2, default=str))

def load_open_signals() -> List[Signal]:
    rows = _load_json(SIGNALS_PATH)
    return [Signal(**r) for r in rows if r.get("status") == "OPEN"]

def save_signals(signals: List[Signal]) -> None:
    existing = _load_json(SIGNALS_PATH)
    # merge by coin+direction — only keep latest signal per coin+direction (no unbounded growth)
    key = lambda s: f"{s.coin}|{s.direction}"
    mp = {key(Signal(**r)): r for r in existing}
    for s in signals:
        mp[key(s)] = asdict(s)  # overwrite older signal for same coin+direction
    # Also prune anything older than 2 hours that's still OPEN (stale)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    for k, v in list(mp.items()):
        st = v.get("status")
        dt = v.get("dt", "")
        if st == "OPEN" and dt:
            try:
                parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
                if parsed < cutoff:
                    del mp[k]
            except Exception:
                pass
    _save_json(SIGNALS_PATH, list(mp.values()))

def load_trades() -> List[Trade]:
    rows = _load_json(TRADES_PATH)
    return [Trade(**r) for r in rows]

def save_trades(trades: List[Trade]) -> None:
    _save_json(TRADES_PATH, [asdict(t) for t in trades])

def append_trade(trade: Trade) -> None:
    trades = load_trades()
    trades.append(trade)
    save_trades(trades)

# ---------------------------------------------------------------------------
# Simulation / paper-trade update engine
# ---------------------------------------------------------------------------
def run_paper_update() -> List[Dict[str, Any]]:
    """
    Walk open signals against latest price and close when SL/TP/trail hit.
    Returns list of closed round-trips.
    """
    cg_ids = _load_cg_ids() if not USE_DEMO_PRICES else {}
    open_sigs = load_open_signals()
    if not open_sigs:
        return []
    closed: List[Dict[str, Any]] = []
    updated: List[Signal] = []
    for sig in open_sigs:
        cid = cg_ids.get(sig.coin) or sig.coin.lower()  # demo mode fallback
        ohlc = fetch_ohlc_gc(cid, days=1)
        if not ohlc:
            updated.append(sig)
            continue
        last = ohlc[-1][4]
        exit_px = last
        exit_reason = ""
        if sig.direction == "LONG":
            if last <= sig.sl_px:
                exit_reason = "SL"
            elif last >= sig.tp_px:
                exit_reason = "TP"
            elif last >= sig.entry_px * 1.03:
                # trailing stop at 3% below highest since entry (simulate)
                trail = sig.entry_px * 1.03 * 0.97
                if last <= trail:
                    exit_reason = "TRAIL"
                    exit_px = trail
        else:  # SHORT
            if last >= sig.sl_px:
                exit_reason = "SL"
            elif last <= sig.tp_px:
                exit_reason = "TP"
            elif last <= sig.entry_px * 0.97:
                trail = sig.entry_px * 0.97 * 1.03
                if last >= trail:
                    exit_reason = "TRAIL"
                    exit_px = trail
        if exit_reason:
            pnl_pct = (exit_px - sig.entry_px) / sig.entry_px
            if sig.direction == "SHORT":
                pnl_pct = -pnl_pct
            pnl_usd = sig.size_usd * pnl_pct
            duration = int((datetime.now(timezone.utc) - datetime.fromisoformat(sig.dt)).total_seconds() / 60)
            trade = Trade(
                coin=sig.coin,
                direction=sig.direction,
                entry_px=sig.entry_px,
                exit_px=round(exit_px, 6),
                pnl_pct=round(pnl_pct, 4),
                pnl_usd=round(pnl_usd, 4),
                duration_min=duration,
                exit_reason=exit_reason,
                dt=datetime.now(timezone.utc).isoformat(),
            )
            append_trade(trade)
            closed.append(asdict(trade))
            sig.status = "CLOSED"
        updated.append(sig)
    save_signals(updated)
    logger.info("Paper update: %d closed, %d still open", len(closed), len(updated) - len(closed))
    return closed

# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
def today_stats() -> Dict[str, Any]:
    trades = load_trades()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_trades = [t for t in trades if t.dt.startswith(today)]
    wins = [t for t in day_trades if t.pnl_usd > 0]
    pnl = sum(t.pnl_usd for t in day_trades)
    return {
        "trades_today": len(day_trades),
        "wins": len(wins),
        "losses": len(day_trades) - len(wins),
        "win_rate": round(len(wins) / len(day_trades), 4) if day_trades else 0.0,
        "pnl_today": round(pnl, 4),
        "open_signals": len(load_open_signals()),
    }

# ---------------------------------------------------------------------------
# Public refresh entrypoint (called by scheduler / cron)
# ---------------------------------------------------------------------------
def refresh(watchlist: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    new_signals = generate_signals(watchlist)
    save_signals(new_signals)
    # Also push to unified queue for live execution
    if new_signals:
        save_to_queue([asdict(s) for s in new_signals])
    run_paper_update()
    return [asdict(s) for s in new_signals]

if __name__ == "__main__":
    # quick sanity test: load scanner watchlist and refresh
    scanner_state = STATE_DIR / "scanner_state.json"
    wl = []
    if scanner_state.exists():
        wl = json.loads(scanner_state.read_text()).get("watchlist", [])
    refresh(wl)
    print(json.dumps(today_stats(), indent=2))
