"""
Auto-Learning Engine for Desk5.
Reads closed trade ledger, computes rolling performance per coin/strategy,
maintains blacklist/whitelist, and emits dynamic signal weights.
"""
import json
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Dict, List, Any

logger = logging.getLogger("auto_learn")

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
LEDGER_PATH = STATE_DIR / "live_ledger.json"
WEIGHTS_PATH = STATE_DIR / "strategy_weights.json"
LEARN_LOG = STATE_DIR / "learning.log"

MIN_TRADES_FOR_CONFIDENCE = 5
BLACKLIST_WR_THRESHOLD = 0.30      # below 30% WR = blacklist
WHITELIST_WR_THRESHOLD = 0.65      # above 65% WR = whitelist
RECENT_WINDOW = 15                 # last N trades for recency bias
WEIGHT_BOOST_WINNER = 1.5
WEIGHT_PENALTY_LOSER = 0.5
MAX_BLACKLIST_DAYS = 3             # auto-review after 3 days


def load_ledger() -> List[dict]:
    if not LEDGER_PATH.exists():
        return []
    try:
        return json.loads(LEDGER_PATH.read_text())
    except Exception:
        return []


def dedupe_trades(trades: List[dict]) -> List[dict]:
    """Remove duplicate ledger entries from sync retries."""
    seen = set()
    out = []
    for t in trades:
        key = (
            t.get("coin"),
            t.get("direction"),
            t.get("entry_px"),
            t.get("exit_px"),
            round(t.get("pnl", 0), 2),
            t.get("exit_reason"),
        )
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def compute_coin_stats(trades: List[dict], window: int = None) -> Dict[str, dict]:
    """Compute per-coin win rate, avg PnL, recency score."""
    stats = defaultdict(lambda: {
        "wins": 0, "losses": 0, "total_pnl": 0.0, "trades": [],
        "last_trade": None, "avg_pnl": 0.0, "wr": 0.0,
    })
    # Sort by close time
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    closed.sort(key=lambda t: t.get("closed_at") or t.get("close_at") or "")
    
    for t in closed:
        coin = t.get("coin", "UNKNOWN")
        pnl = t.get("pnl", 0)
        stats[coin]["trades"].append(pnl)
        stats[coin]["total_pnl"] += pnl
        if pnl > 0:
            stats[coin]["wins"] += 1
        else:
            stats[coin]["losses"] += 1
        stats[coin]["last_trade"] = t.get("closed_at") or t.get("close_at")
    
    # Compute rolling window stats
    for coin, s in stats.items():
        trades_list = s["trades"]
        if window and len(trades_list) > window:
            recent = trades_list[-window:]
        else:
            recent = trades_list
        wins = sum(1 for p in recent if p > 0)
        count = len(recent)
        s["wr"] = wins / count if count else 0.0
        s["avg_pnl"] = sum(recent) / count if count else 0.0
        s["count"] = count
        s["recent_count"] = len(recent)
    
    return dict(stats)


def classify_coins(stats: Dict[str, dict]) -> Dict[str, List[str]]:
    """Classify coins into tiers based on rolling performance."""
    blacklist = []
    whitelist = []
    neutral = []
    for coin, s in stats.items():
        if s["count"] < MIN_TRADES_FOR_CONFIDENCE:
            neutral.append(coin)
            continue
        if s["wr"] < BLACKLIST_WR_THRESHOLD:
            blacklist.append(coin)
        elif s["wr"] >= WHITELIST_WR_THRESHOLD:
            whitelist.append(coin)
        else:
            neutral.append(coin)
    return {"whitelist": whitelist, "blacklist": blacklist, "neutral": neutral}


def compute_signal_weights(stats: Dict[str, dict]) -> Dict[str, float]:
    """Compute dynamic weight multipliers per coin for signal scoring."""
    weights = {}
    for coin, s in stats.items():
        base = 1.0
        if s["count"] >= MIN_TRADES_FOR_CONFIDENCE:
            if s["wr"] >= WHITELIST_WR_THRESHOLD:
                base = WEIGHT_BOOST_WINNER
            elif s["wr"] < BLACKLIST_WR_THRESHOLD:
                base = WEIGHT_PENALTY_LOSER
            elif s["avg_pnl"] > 0:
                base = 1.0 + (s["avg_pnl"] / 100)  # small boost for consistent winners
            else:
                base = max(0.3, 1.0 + (s["avg_pnl"] / 50))
        weights[coin] = round(base, 2)
    return weights


def run_learning_cycle() -> dict:
    """Main entry point. Run every 30 minutes via cron."""
    ledger = load_ledger()
    trades = dedupe_trades(ledger)
    stats = compute_coin_stats(trades, window=RECENT_WINDOW)
    tiers = classify_coins(stats)
    weights = compute_signal_weights(stats)
    
    # Compute overall desk health
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    total_pnl = sum(t.get("pnl", 0) for t in closed)
    wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
    wr = wins / len(closed) if closed else 0
    
    result = {
        "dt": datetime.now(timezone.utc).isoformat(),
        "total_unique_trades": len(closed),
        "total_pnl": round(total_pnl, 2),
        "overall_wr": round(wr, 3),
        "tiers": tiers,
        "weights": weights,
        "coin_stats": {k: {
            "count": v["count"],
            "wr": round(v["wr"], 3),
            "avg_pnl": round(v["avg_pnl"], 2),
            "total_pnl": round(v["total_pnl"], 2),
        } for k, v in stats.items()},
        "recommendations": [],
    }
    
    # Generate actionable recommendations
    recs = []
    if tiers["blacklist"]:
        recs.append(f"🚫 BLACKLIST: {', '.join(tiers['blacklist'])}")
    if tiers["whitelist"]:
        recs.append(f"🌟 WHITELIST: {', '.join(tiers['whitelist'])}")
    if wr < 0.45:
        recs.append("⚠️ Overall WR below 45% — consider reducing position count or tightening SL")
    if total_pnl < -500:
        recs.append("🛑 Desk PnL deeply negative — manual review recommended")
    
    result["recommendations"] = recs
    
    # Persist
    WEIGHTS_PATH.write_text(json.dumps(result, indent=2))
    
    # Append to learning log
    log_line = f"{result['dt']} | trades={result['total_unique_trades']} | PnL=${result['total_pnl']:+.2f} | WR={result['overall_wr']:.1%} | BL={len(tiers['blacklist'])} | WL={len(tiers['whitelist'])}\n"
    with LEARN_LOG.open("a") as f:
        f.write(log_line)
    
    # Log summary
    logger.info("Learning cycle complete: %d trades, PnL $%.2f, WR %.1f%%", 
                result["total_unique_trades"], result["total_pnl"], result["overall_wr"] * 100)
    for r in recs:
        logger.info(r)
    
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = run_learning_cycle()
    print(json.dumps({
        "trades": out["total_unique_trades"],
        "pnl": out["total_pnl"],
        "wr": out["overall_wr"],
        "blacklist": out["tiers"]["blacklist"],
        "whitelist": out["tiers"]["whitelist"],
        "recs": out["recommendations"],
    }, indent=2))
    sys.exit(0)
