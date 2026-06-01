"""
Simulation Engine v3 — 100 runs, compounding, $1500 vs $1000 comparison.
Uses real CoinGecko OHLC data cached in data_store/cg_ohlc_cache.json.
Scanning strategies on every bar of historical data for realistic signal counts.
"""
import json, math, random, logging, sys
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent
CACHE_FILE = SCRIPT_DIR.parent.parent / "data_store" / "cg_ohlc_cache.json"


def load_cached_market_data(min_candles: int = 30) -> Dict[str, List[dict]]:
    if not CACHE_FILE.exists():
        logger.error("Cache not found: %s", CACHE_FILE)
        return {}
    try:
        cache = json.loads(CACHE_FILE.read_text())
        data = cache.get("data", {})
        out = {}
        for coin, payload in data.items():
            candles = payload.get("ohlc", [])
            if len(candles) >= min_candles:
                out[coin] = candles
        return out
    except Exception as exc:
        logger.error("Cache read failed: %s", exc)
        return {}


def bb_squeeze(candles: List[dict], mult: float = 2.0) -> dict:
    if len(candles) < 20:
        return {"valid": False}
    candles = candles[-20:]
    closes = [c["c"] for c in candles]
    sma = sum(closes) / len(closes)
    variance = sum((c - sma) ** 2 for c in closes) / len(closes)
    std = math.sqrt(variance)
    upper = sma + mult * std
    lower = sma - mult * std
    bandwidth = ((upper - lower) / sma * 100) if sma > 0 else 0
    squeeze = bandwidth < 5.0
    bias = "LONG" if closes[-1] > sma else "SHORT"
    atr = sum(c["h"] - c["l"] for c in candles) / len(candles)
    return {
        "valid": True,
        "sma": sma,
        "upper": upper,
        "lower": lower,
        "bandwidth_pct": bandwidth,
        "squeeze": squeeze,
        "bias": bias,
        "atr": atr,
    }


def wilder_rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def momentum_fade(candles: List[dict]) -> dict:
    if len(candles) < 20:
        return {"valid": False}
    closes = [c["c"] for c in candles]
    rsi = wilder_rsi(closes)
    sma20 = sum(closes[-20:]) / 20
    std20 = math.sqrt(sum((c - sma20) ** 2 for c in closes[-20:]) / 20)
    overbought = rsi > 70 or closes[-1] > sma20 + 2 * std20
    oversold = rsi < 30 or closes[-1] < sma20 - 2 * std20
    if not overbought and not oversold:
        return {"valid": False}
    direction = "SHORT" if overbought else "LONG"
    atr = sum(c["h"] - c["l"] for c in candles[-20:]) / 20
    return {"valid": True, "direction": direction, "rsi": rsi, "atr": atr, "score": abs(rsi - 50) / 50}


# ---- Simulate one coin's 30-day returns for each strategy ----

def sim_coin_day(candles_30d: List[dict], bankroll: float) -> Dict[str, float]:
    """
    Walk through each candle (day) and count signals.
    Returns dict of strategy -> total pnl for this coin.
    """
    vs_pnl, mf_pnl, fy_pnl, qs_pnl, mt_pnl = 0, 0, 0, 0, 0
    # We have ~30 daily candles; simulate one signal per strategy when it fires
    for i in range(20, len(candles_30d)):
        window = candles_30d[:i+1]

        # Vol Squeeze
        sig = bb_squeeze(window)
        if sig.get("squeeze") and sig.get("bias"):
            risk = bankroll * 0.015
            # 2:1 R/R optimistic; factor in hit rate ~55%
            hit = random.random() < 0.55
            if hit:
                pnl = risk * random.uniform(1.2, 2.0)
            else:
                pnl = -risk * random.uniform(0.6, 1.1)
            vs_pnl += pnl

        # Momentum Fade
        mf = momentum_fade(window)
        if mf.get("valid"):
            risk = bankroll * 0.015
            hit = random.random() < 0.52
            if hit:
                pnl = risk * mf["score"] * random.uniform(1.0, 2.0)
            else:
                pnl = -risk * random.uniform(0.7, 1.2)
            mf_pnl += pnl

    # Quick Scalp: many trades (assume 3/day * 30 = 90)
    trades = random.randint(60, 100)
    for _ in range(trades):
        risk = bankroll * 0.015
        hit = random.random() < 0.42  # slight negative edge without fill slippage
        pnl = risk * random.uniform(0.2, 1.2) if hit else -risk * random.uniform(0.4, 1.0)
        qs_pnl += pnl

    # Funding yield: passive
    fy_pnl = bankroll * 0.0003 * 30 * random.uniform(0.8, 1.3)

    # Macro tilt: a few trades
    for _ in range(random.randint(3, 8)):
        risk = bankroll * 0.015
        pnl = risk * random.gauss(0.05, 0.8)
        mt_pnl += pnl

    return {"vol_squeeze": vs_pnl, "momentum_fade": mf_pnl, "funding_yield": fy_pnl,
            "quick_scalp": qs_pnl, "macro_tilt": mt_pnl}


def simulate_portfolio(market_data: Dict[str, List[dict]], days: int = 30, bankroll_start: float = 1000) -> dict:
    coins = list(market_data.keys())
    if not coins:
        return {"start": bankroll_start, "end": bankroll_start, "pnl": 0, "max_dd": 0, "trades": 0, "win_rate": 0}
    bankroll = bankroll_start
    peak = bankroll
    max_dd = 0.0
    total_trades = 0
    wins = 0
    weights = {"vol_squeeze": 0.30, "momentum_fade": 0.20, "funding_yield": 0.10, "quick_scalp": 0.20, "macro_tilt": 0.10}

    for coin in random.sample(coins, min(len(coins), 5)):
        candles = market_data[coin]
        if len(candles) < 20:
            continue
        res = sim_coin_day(candles, bankroll)
        for strat, w in weights.items():
            bal_change = res.get(strat, 0) * w
            bankroll += bal_change
            # rough trade count heuristic
            if strat == "quick_scalp":
                total_trades += random.randint(60, 100)
            else:
                total_trades += random.randint(2, 8)
            if bal_change > 0:
                wins += 1
        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
        if bankroll <= 0:
            bankroll = 0
            break

    # Win rate rough estimate
    wr = wins / total_trades if total_trades > 0 else 0
    return {
        "start": round(bankroll_start, 2),
        "end": round(bankroll, 2),
        "pnl": round(bankroll - bankroll_start, 2),
        "max_dd": round(max_dd, 2),
        "trades": total_trades,
        "win_rate": round(wr, 4),
    }


def run_monte_carlo(runs: int = 100, days: int = 30) -> Dict[int, List[dict]]:
    market_data = load_cached_market_data(min_candles=30)
    logger.info("Loaded market data for %d coins", len(market_data))
    results = {1000: [], 1500: []}
    for _ in range(runs):
        results[1000].append(simulate_portfolio(market_data, days=days, bankroll_start=1000))
        results[1500].append(simulate_portfolio(market_data, days=days, bankroll_start=1500))
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data = run_monte_carlo(runs=100, days=30)
    print(json.dumps(data, indent=2))
    sys.exit(0)
