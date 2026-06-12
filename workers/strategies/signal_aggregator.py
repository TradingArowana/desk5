"""
Unified Signal Aggregator for Desk5.
Collects signals from all strategies and feeds them to the executor.
"""
import json
import logging
import math
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
STATE_DIR.mkdir(parents=True, exist_ok=True)
UNIFIED_QUEUE = STATE_DIR / "all_signals.json"
SIGNAL_TTL_SECONDS = 900  # 15 min
MAX_QUEUE_SIZE = 10       # Top 10 only

# ── HARD GUARDS — these are the LAST LINE OF DEFENSE before execution ──
MIN_COIN_PRICE = 0.10              # Reject anything below $0.10
MIN_VOLUME_24H_USD = 1_000_000     # Require $1M+ daily volume — no shit liquidity
MAX_SLIPPAGE_PCT = 2.0             # Reject if spread implies >2% slippage

# Known meme / shitcoin / manipulated tickers — BLOCKED regardless of filters
SHITCOIN_BLACKLIST = {
    # Memecoins with no fundamentals
    "PEPE", "SHIB", "FLOKI", "BONK", "WIF",
    "MOG", "BOME", "PENGU", "POPCAT", "TRUMP", "MELANIA",
    "HARRY", "PORK", "TURBO", "BRETT", "TETRIS",
    # Extremely low-float / pump-and-dump prone
    "XPL", "HEMI", "MEME", "BABY", "NXPC",
    # Stablecoins (shouldn't be in perps but just in case)
    "USDC", "USDT", "DAI", "TUSD",
}

# Tradeable coins cache (Hyperliquid perps)
_HL_COINS: set = set()


def _refresh_tradeable() -> set:
    """Fetch list of tradeable perps from Hyperliquid."""
    global _HL_COINS
    try:
        import requests
        r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "meta"}, timeout=15)
        r.raise_for_status()
        universe = r.json().get("universe", [])
        _HL_COINS = {c["name"] for c in universe}
        logger.info("Tradeable perps: %d", len(_HL_COINS))
    except Exception as exc:
        logger.warning("Failed to refresh tradeable coins: %s", exc)
    return _HL_COINS


def is_tradeable(coin: str) -> bool:
    if not _HL_COINS:
        _refresh_tradeable()
    return coin in _HL_COINS


# Strategy signal files — ALL active strategies
STRATEGY_FILES = [
    STATE_DIR / "hl_breakout_signals.json",
    STATE_DIR / "hl_vol_squeeze_signals.json",
    STATE_DIR / "quick_scalp_signals.json",
    STATE_DIR / "gold_scalp_signals.json",
    STATE_DIR / "bell_scalp_signals.json",
    STATE_DIR / "momentum_fade_signals.json",
    STATE_DIR / "fib_1m_scalp_signals.json",   # NEW: Fibonacci golden-zone 1m scalper
]


def _load_json(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def _score_signal(s: dict) -> float:
    """Score a signal. Higher = better."""
    entry = float(s.get('entry_px', 0))
    sl = float(s.get('sl_px', 0))
    tp = float(s.get('tp_px', 0))
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    rr = tp_dist / sl_dist if sl_dist > 0 else 0

    vol = abs(float(s.get('vol_24h', 0)))
    volume = float(s.get('volume_24h', 0))

    # R:R × 40 + volatility bonus × 30 + volume score × 30
    score = (rr * 40) + (min(vol, 50) * 0.6) + (min(math.log10(volume + 1), 9) * 3.33)

    # Apply auto-learning weight multiplier
    coin = s.get('coin', '')
    weights = _load_learning_weights()
    if coin in weights:
        multiplier = weights[coin]
        score *= multiplier
    # If blacklisted (weight near 0), score collapses
    if coin in weights and weights[coin] < 0.4:
        score = -1000  # force drop

    return round(score, 2)

def _load_learning_weights() -> dict:
    """Load per-coin dynamic weights from auto-learning engine."""
    weights_path = STATE_DIR / "strategy_weights.json"
    if not weights_path.exists():
        return {}
    try:
        data = json.loads(weights_path.read_text())
        return data.get("weights", {})
    except Exception:
        return {}


def aggregate_signals() -> List[Dict]:
    """Combine signals from all strategies, score them, return ranked list."""
    all_sigs = []

    for sig_file in STRATEGY_FILES:
        sigs = _load_json(sig_file)
        for s in sigs:
            if hasattr(s, '__dict__'):
                s = s.__dict__
            s['_source'] = sig_file.name
            all_sigs.append(s)

    scored = []
    dropped = 0
    for s in all_sigs:
        coin = s.get('coin', '')
        if not is_tradeable(coin):
            dropped += 1
            continue
        # ── HARD GUARD #1: SHITCOIN BLACKLIST ──
        if coin.upper() in SHITCOIN_BLACKLIST:
            dropped += 1
            logger.warning("Signal %s BLACKLISTED — meme/manipulated coin, dropped", coin)
            continue
        # ── HARD GUARD #2: PRICE FLOOR ──
        entry = float(s.get('entry_px', 0))
        if entry < MIN_COIN_PRICE:
            dropped += 1
            logger.warning("Signal %s entry $%.6f below $%.2f — dropped", coin, entry, MIN_COIN_PRICE)
            continue
        # ── HARD GUARD #3: VOLUME FILTER ──
        vol_24h = float(s.get('vol_24h', 0)) or float(s.get('volume_24h', 0))
        if vol_24h > 0 and vol_24h < MIN_VOLUME_24H_USD:
            dropped += 1
            logger.warning("Signal %s volume $%.0f below $%.0f — dropped", coin, vol_24h, MIN_VOLUME_24H_USD)
            continue
        s['_score'] = _score_signal(s)
        s['_rr'] = round(abs(float(s.get('tp_px', 0)) - float(s.get('entry_px', 0))) /
                          abs(float(s.get('entry_px', 0)) - float(s.get('sl_px', 0))), 2) if float(s.get('sl_px', 0)) > 0 else 0
        scored.append(s)

    if dropped > 0:
        logger.info("Dropped %d non-tradeable signals", dropped)

    scored.sort(key=lambda x: x['_score'], reverse=True)
    return scored


def save_to_queue(signals):
    """Save one or more signals to unified execution queue."""
    # Normalize single dict → list
    if isinstance(signals, dict):
        signals = [signals]

    # HARD GUARD: reject any signal below $0.10 at queue level
    filtered = []
    for s in signals:
        entry = float(s.get('entry_px', 0))
        if entry < MIN_COIN_PRICE:
            logger.warning("REJECTED %s entry $%.6f below $%.2f — not queued", s.get('coin', '?'), entry, MIN_COIN_PRICE)
            continue
        # ── HARD GUARD: SHITCOIN BLACKLIST at queue level ──
        coin = s.get('coin', '')
        if coin.upper() in SHITCOIN_BLACKLIST:
            logger.warning("REJECTED %s — BLACKLISTED meme/manipulated coin", coin)
            continue
        # ── HARD GUARD: VOLUME at queue level ──
        vol_24h = float(s.get('vol_24h', 0)) or float(s.get('volume_24h', 0))
        if vol_24h > 0 and vol_24h < MIN_VOLUME_24H_USD:
            logger.warning("REJECTED %s volume $%.0f below $%.0f — not queued", coin, vol_24h, MIN_VOLUME_24H_USD)
            continue
        filtered.append(s)
    signals = filtered
    if not signals:
        return []

    existing = _load_json(UNIFIED_QUEUE)

    for s in signals:
        if hasattr(s, '__dict__'):
            s = s.__dict__
        # Auto-score if missing
        if '_score' not in s:
            s['_score'] = _score_signal(s)
        s['_rr'] = round(abs(float(s.get('tp_px', 0)) - float(s.get('entry_px', 0))) /
                        abs(float(s.get('entry_px', 0)) - float(s.get('sl_px', 0))), 2) if float(s.get('sl_px', 0)) > 0 else 0
        s['queued_at'] = datetime.now(timezone.utc).isoformat()
        existing.append(s)

    # Deduplicate and keep highest score per coin+direction
    seen = {}
    for s in existing:
        key = f"{s.get('coin', '?')}|{s.get('direction', '?')}"
        if key not in seen or s.get('_score', 0) > seen[key].get('_score', 0):
            seen[key] = s

    final = list(seen.values())
    final.sort(key=lambda x: x.get('_score', 0), reverse=True)
    # Keep only top N
    if len(final) > MAX_QUEUE_SIZE:
        final = final[:MAX_QUEUE_SIZE]
    UNIFIED_QUEUE.write_text(json.dumps(final, indent=2))
    logger.info("Queue updated: %d signals (top 10 kept)", len(final))
    return final


def flush_queue():
    """Flush expired signals from unified queue."""
    now = datetime.now(timezone.utc).timestamp()
    queue = _load_json(UNIFIED_QUEUE)
    queue = [s for s in queue if (now - _dt_to_ts(s.get('dt', ''))) < SIGNAL_TTL_SECONDS]
    if len(queue) > MAX_QUEUE_SIZE:
        queue = queue[:MAX_QUEUE_SIZE]
    UNIFIED_QUEUE.write_text(json.dumps(queue, indent=2))
    return queue


def _dt_to_ts(dt_str: str) -> float:
    try:
        dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        return dt.timestamp()
    except Exception:
        return 0.0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sigs = aggregate_signals()
    for s in sigs:
        print(f"  {s.get('coin')} {s.get('direction')} @ {s.get('entry_px')} [{s.get('_source')}]")
