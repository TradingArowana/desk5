"""
Desk5 Multi-Strategy Simulation Engine v2.
30 random tokens per run, 50 passes, includes macro assets.
"""
import os
import json
import math
import random
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Tuple
from dataclasses import dataclass, asdict
import requests

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
STATE_DIR.mkdir(parents=True, exist_ok=True)
SIM_REPORT = STATE_DIR / "sim_report_v2.json"
LIVE_TRADES = STATE_DIR / "quick_scalp_trades.json"

HL_MAINNET = "https://api.hyperliquid.xyz"

# ── Fetch HL Universe ─────────────────────────────────────────────────────

def fetchHL_universe() -> list:
    try:
        meta = requests.post(HL_MAINNET + "/info", json={"type": "meta"}, timeout=10).json()
        return [c["name"] for c in meta.get("universe", [])]
    except Exception:
        return []


def fetch_marks(coins: list) -> dict:
    """Fetch current mark prices for a list of coins."""
    try:
        mids = requests.post(HL_MAINNET + "/info", json={"type": "allMids"}, timeout=15).json()
        return {c: float(mids.get(c, 0)) for c in coins if mids.get(c)}
    except Exception:
        return {}


def fetch_funding(coins: list = None) -> dict:
    """Fetch funding rates for coins."""
    try:
        meta = requests.post(HL_MAINNET + "/info", json={"type": "metaAndAssetCtxs"}, timeout=15).json()
        rates = {}
        for coin, ctx in zip(meta[0].get("universe", []), meta[1]):
            if coins and coin["name"] not in coins:
                continue
            rates[coin["name"]] = float(ctx.get("funding", 0))
        return rates
    except Exception:
        return {}


def fetch_CG_market_data() -> list:
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": 1, "sparkline": "false", "price_change_percentage": "24h,7d,30d"}
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def pick_30_tokens(cg_data: list, hl_coins: list) -> list:
    """Build a 30-token basket including macro + crypto."""
    basket = ["SPX", "PAXG"]  # Always include macro
    # Add 28 from HL universe, weighted by market cap from CG
    cg_lookup = {c["symbol"].upper(): c for c in cg_data if c.get("symbol")}
    candidates = []
    for c in hl_coins:
        sym = c.upper()
        if sym in ("SPX", "PAXG", "GAS"):
            continue
        cg = cg_lookup.get(sym)
        if cg is None:
            # No CG data — use neutral score but still include as longtail
            candidates.append((c, 1.0, 0))
            continue
        mcap = cg.get("market_cap", 0) if cg else 0
        vol = cg.get("total_volume", 0) if cg else 0
        change_raw = cg.get("price_change_percentage_30d_in_currency", 0)
        change = abs(change_raw) if change_raw else 0
        score = (vol ** 0.5) * (1 + change) if vol else 1.0
        candidates.append((c, score, mcap))
    # Sort by score descending
    candidates.sort(key=lambda x: x[1], reverse=True)
    # Pick 28, but bias toward long-tail (outside top 30) per mandate
    top30 = set(cg_data[i]["symbol"].upper() for i in range(30) if i < len(cg_data))
    longtail = [c for c in candidates if c[0].upper() not in top30][:40]
    # Mix: half top score, half random from longtail for diversity
    mix = [c[0] for c in candidates[:14] if c[0] not in basket]
    random.shuffle(longtail)
    mix += [c[0] for c in longtail[:14] if c[0] not in basket]
    basket += mix[:28]
    return basket[:30]


# ── Strategy Definitions ────────────────────────────────────────────────────

@dataclass
class SimParams:
    strategy: str
    start_bankroll: float = 1000.0
    risk_per_trade: float = 0.015  # Reduced from 2% → 1.5% to cap drawdown
    rrr: float = 2.0
    win_rate: float = 0.47
    fee_pct: float = 0.0004
    slippage_bps: float = 7.0  # Increased for realism
    leverage: float = 1.5
    max_positions: int = 5
    trades_per_day: float = 4.0
    stop_loss_pct: float = 0.015
    take_profit_pct: float = 0.03


STRATEGY_BASES = {
    "quick_scalp": {
        "win_rate_range": (0.42, 0.52),
        "trades_per_day_range": (3, 6),
        "leverage_range": (1.0, 2.0),
        "slippage_bps": 7.0,
        "max_positions": 5,
        "fee_pct": 0.0004,
        "rrr": 2.0,
        "risk": 0.015,
    },
    "funding_yield": {
        "win_rate_range": (0.55, 0.65),
        "trades_per_day_range": (1, 3),
        "leverage_range": (1.0, 1.5),
        "slippage_bps": 5.0,
        "max_positions": 2,
        "fee_pct": 0.0004,
        "rrr": 1.5,
        "risk": 0.02,  # Slightly more aggressive — funding is "free money"
    },
    "vol_squeeze": {
        "win_rate_range": (0.50, 0.60),
        "trades_per_day_range": (1, 4),
        "leverage_range": (1.0, 2.5),
        "slippage_bps": 10.0,  # More volatile = more slippage
        "max_positions": 3,
        "fee_pct": 0.0004,
        "rrr": 2.5,
        "risk": 0.012,
    },
    "momentum_fade": {
        "win_rate_range": (0.48, 0.58),
        "trades_per_day_range": (2, 5),
        "leverage_range": (1.0, 2.0),
        "slippage_bps": 7.0,
        "max_positions": 3,
        "fee_pct": 0.0004,
        "rrr": 2.0,
        "risk": 0.015,
    },
    "macro_tilt": {
        "win_rate_range": (0.52, 0.62),
        "trades_per_day_range": (1, 2),  # Macro = slower, fewer setups
        "leverage_range": (1.0, 3.0),
        "slippage_bps": 5.0,
        "max_positions": 2,
        "fee_pct": 0.0004,
        "rrr": 2.5,
        "risk": 0.015,
    },
}


def draw_params(strategy: str, start_bankroll: float = 1000.0) -> SimParams:
    b = STRATEGY_BASES[strategy]
    win_rate = random.uniform(*b["win_rate_range"])
    tpd = random.uniform(*b["trades_per_day_range"])
    lev = random.uniform(*b["leverage_range"])
    return SimParams(
        strategy=strategy,
        start_bankroll=start_bankroll,
        win_rate=round(win_rate, 4),
        trades_per_day=round(tpd, 2),
        leverage=round(lev, 2),
        fee_pct=b["fee_pct"],
        max_positions=b["max_positions"],
        rrr=b["rrr"],
        risk_per_trade=round(b["risk"], 4),
        slippage_bps=b["slippage_bps"],
        stop_loss_pct=round(b["risk"], 4),
        take_profit_pct=round(b["risk"] * b["rrr"], 4),
    )


# ── Simulation Core ───────────────────────────────────────────────────────

def simulate_run(params: SimParams, days: int = 30, seed: int = 42) -> dict:
    random.seed(seed)
    bankroll = params.start_bankroll
    peak = bankroll
    trades = 0; wins = 0; losses = 0
    daily_pnls = []; equity_curve = [bankroll]
    max_drawdown = 0.0

    for day in range(days):
        day_pnl = 0.0
        tpd = random.gauss(params.trades_per_day, max(0.5, params.trades_per_day * 0.3))
        n = max(1, int(tpd))
        if bankroll <= 0: break
        for _ in range(n):
            if bankroll <= 0: break
            trades += 1
            risk = bankroll * params.risk_per_trade
            size = risk / params.stop_loss_pct * params.leverage
            cost = size * (params.fee_pct + params.slippage_bps / 10000)
            outcome = random.random() < params.win_rate
            if outcome:
                wins += 1
                pnl = risk * params.rrr - cost
            else:
                losses += 1
                pnl = -risk - cost
            bankroll += pnl
            day_pnl += pnl
            equity_curve.append(bankroll)
            if bankroll > peak: peak = bankroll
            dd = (peak - bankroll) / peak * 100 if peak > 0 else 0
            if dd > max_drawdown: max_drawdown = dd

        daily_pnls.append(day_pnl)

    return {
        "days": len(daily_pnls),
        "trades": trades, "wins": wins, "losses": losses,
        "win_rate": round(wins / trades, 4) if trades else 0.0,
        "final_bankroll": round(bankroll, 2),
        "pnl": round(bankroll - params.start_bankroll, 2),
        "roi_pct": round((bankroll - params.start_bankroll) / params.start_bankroll * 100, 2) if params.start_bankroll else 0,
        "max_drawdown_pct": round(max_drawdown, 2),
        "peak": round(peak, 2),
        "equity_curve": [round(x, 2) for x in equity_curve[::max(1, len(equity_curve)//100)]],
    }


def run_mc(strategy: str, runs: int = 50, days: int = 30, start_bankroll: float = 1000.0) -> dict:
    results = []
    for i in range(runs):
        params = draw_params(strategy, start_bankroll=start_bankroll)
        run = simulate_run(params, days=days, seed=random.randint(0, 999999))
        results.append(run)
    final_brs = [r["final_bankroll"] for r in results]
    pnls = [r["pnl"] for r in results]
    dds = [r["max_drawdown_pct"] for r in results]
    wrs = [r["win_rate"] for r in results]
    profitable = sum(1 for p in pnls if p > 0)
    ruined = sum(1 for b in final_brs if b <= 0)

    # Time to 10x
    avg_pnl = sum(pnls) / len(pnls)
    if avg_pnl > 0:
        daily_ret = avg_pnl / start_bankroll / days
        days_to_10x = math.log(10) / math.log(1 + daily_ret) if daily_ret > 0 else float('inf')
    else:
        days_to_10x = float('inf')

    return {
        "strategy": strategy, "runs": runs, "days_per_run": days,
        "avg_final_bankroll": round(sum(final_brs) / len(final_brs), 2),
        "median_final_bankroll": round(sorted(final_brs)[len(final_brs)//2], 2),
        "avg_pnl": round(avg_pnl, 2),
        "avg_win_rate": round(sum(wrs) / len(wrs), 4),
        "avg_drawdown_pct": round(sum(dds) / len(dds), 2),
        "max_drawdown_pct": round(max(dds), 2),
        "profitability_pct": round(profitable / runs * 100, 2),
        "ruin_pct": round(ruined / runs * 100, 2),
        "days_to_10x_estimate": round(days_to_10x, 1) if days_to_10x != float('inf') else None,
        "best_run": max(results, key=lambda x: x["final_bankroll"]),
        "worst_run": min(results, key=lambda x: x["final_bankroll"]),
    }


# ── Live Correlation ──────────────────────────────────────────────────────

def load_live_trades() -> list:
    if not LIVE_TRADES.exists(): return []
    try:
        data = json.loads(LIVE_TRADES.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def live_stats() -> tuple:
    trades = load_live_trades()
    if not trades: return 0.0, 0.0, 0
    wins = [t for t in trades if t.get("pnl_usd", 0) > 0]
    total = sum(t.get("pnl_usd", 0) for t in trades)
    return (len(wins) / len(trades) if trades else 0.0), total, len(trades)


# ── Portfolio Timeline ─────────────────────────────────────────────────────

def portfolio_timeline(blend: dict, start: float = 1000.0, target: float = 10000.0, mrr_target: float = 200000.0):
    """Compute compounding path assuming monthly rebalancing."""
    monthly_return = blend["avg_roi_pct"] / 100
    bankroll = start
    days = 0
    milestones = []
    while bankroll < mrr_target and days < 1000:
        daily_growth = (1 + monthly_return) ** (1/30) - 1
        bankroll *= (1 + daily_growth)
        days += 1
        if bankroll >= target and not any(m[0] == "10x" for m in milestones):
            milestones.append(("10x", bankroll, days))
        if bankroll >= 50000 and not any(m[0] == "sweep" for m in milestones):
            milestones.append(("sweep", bankroll, days))
        if bankroll >= mrr_target:
            milestones.append(("mrr", bankroll, days))
    return milestones


# ── Main ────────────────────────────────────────────────────────────────────

def run_all(start_bankroll: float = 1000.0) -> dict:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger.info("Starting Desk5 Sim v2 (50 runs x 30d x 5 strategies, macro + crypto) bankroll=%s", start_bankroll)
    start = datetime.now(timezone.utc)

    hl_coins = fetchHL_universe()
    cg_data = fetch_CG_market_data()
    basket = pick_30_tokens(cg_data, hl_coins)
    marks = fetch_marks(basket)
    funding = fetch_funding(basket)

    logger.info("Basket: %s", basket)
    logger.info("Marks fetched: %d/%d", len(marks), len(basket))

    strategies = list(STRATEGY_BASES.keys())
    aggregate = {}
    for strat in strategies:
        logger.info("Running MC for %s...", strat)
        report = run_mc(strat, runs=50, days=30, start_bankroll=start_bankroll)
        aggregate[strat] = report

    # Portfolio blend with macro tilt
    portfolio_pnl = sum(
        aggregate[s]["avg_pnl"] * w
        for s, w in {
            "quick_scalp": 0.30,     # Reduced to make room for macro
            "funding_yield": 0.20,
            "vol_squeeze": 0.10,
            "momentum_fade": 0.20,
            "macro_tilt": 0.20,      # SPX + PAXG exposure
        }.items()
    )
    portfolio_roi = (portfolio_pnl / start_bankroll) * 100
    days_to_10x = aggregate["momentum_fade"]["days_to_10x_estimate"]  # proxy

    # Compounding timeline
    milestones = portfolio_timeline({"avg_roi_pct": portfolio_roi}, start=start_bankroll)

    # Live correlation
    live_wr, live_pnl, live_count = live_stats()

    final_report = {
        "meta": {"run_at": start.isoformat(), "runs_per_strategy": 50, "days_per_run": 30, "tokens_in_basket": basket, "start_bankroll": start_bankroll},
        "strategies": aggregate,
        "portfolio_blend": {
            "weights": {"quick_scalp": 0.30, "funding_yield": 0.20, "vol_squeeze": 0.10, "momentum_fade": 0.20, "macro_tilt": 0.20},
            "avg_pnl": round(portfolio_pnl, 2),
            "avg_roi_pct": round(portfolio_roi, 2),
            "days_to_10x_estimate": days_to_10x,
            "milestones": {m[0]: {"bankroll": round(m[1], 2), "days": m[2]} for m in milestones},
        },
        "live_correlation": {
            "live_win_rate": round(live_wr, 4),
            "live_pnl": round(live_pnl, 2),
            "live_trade_count": live_count,
        },
        "recommendations": _make_recs(aggregate, portfolio_pnl, start_bankroll),
    }
    SIM_REPORT.write_text(json.dumps(final_report, indent=2))
    return final_report


def _make_recs(ag: dict, portfolio_pnl: float, start_bankroll: float = 1000.0) -> list:
    recs = []
    ranked = sorted(ag.items(), key=lambda x: x[1]["avg_pnl"], reverse=True)
    recs.append(f"#1 strategy: {ranked[0][0]} (+${ranked[0][1]['avg_pnl']}/30d)")
    recs.append(f"#2 strategy: {ranked[1][0]} (+${ranked[1][1]['avg_pnl']}/30d)")
    recs.append(f"Portfolio 30d projection: ${portfolio_pnl:+.2f}")
    if ag["funding_yield"]["avg_drawdown_pct"] < 10:
        recs.append("Funding yield is ballast: low drawdown, steady")
    if portfolio_pnl > 0:
        recs.append(f"Compound model suggests aggressive bankroll growth — scale risk_per_trade only after ${start_bankroll*5:.0f}")
    # Check mandate
    for s, data in ag.items():
        if data["max_drawdown_pct"] > 20:
            recs.append(f"WARNING: {s} breached 20% drawdown in at least one run. Reduce size.")
    return recs


if __name__ == "__main__":
    print(json.dumps(run_all(), indent=2))
