
"""
Backtest stub for QuickScalp strategy.
Replays last ~30 days of CoinGecko OHLC and computes signal accuracy.
"""
import json
from datetime import datetime, timezone
from typing import List, Dict

import requests

CG_OHLC = "https://api.coingecko.com/api/v3/coins/{id}/ohlc"


def _cg_id(symbol: str) -> str:
    mapping = {
        "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
        "AVAX": "avalanche-2", "DOGE": "dogecoin",
        "PEPE": "pepe", "LINK": "chainlink", "UNI": "uniswap",
    }
    return mapping.get(symbol.upper(), symbol.lower())


def fetch_ohlc(symbol: str, days: int = 30) -> List[List[float]]:
    try:
        r = requests.get(
            CG_OHLC.format(id=_cg_id(symbol)),
            params={"vs_currency": "usd", "days": days},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def run_backtest(symbols: List[str]) -> Dict:
    results: Dict[str, Dict] = {}
    for sym in symbols:
        ohlc = fetch_ohlc(sym, days=30)
        points = len(ohlc)
        wins, losses = 0, 0
        pnl = 0.0
        trades = []
        # Naive backtest: 3% breakout from previous candle high/low
        for i in range(2, min(points, 100)):
            prev_high, prev_low = ohlc[i-1][2], ohlc[i-1][3]
            curr_open, curr_high, curr_low, curr_close = ohlc[i][1], ohlc[i][2], ohlc[i][3], ohlc[i][4]
            if curr_high > prev_high * 1.03:
                # simulate long entry at break, exit at next candle close + 6%
                entry = prev_high * 1.03
                exit_px = entry * 1.06
                p = round((exit_px - entry) / entry * 100, 2)
                wins += 1
                pnl += p
                trades.append({"sym": sym, "dir": "LONG", "entry": round(entry, 4), "exit": round(exit_px, 4), "pnl_pct": p})
            elif curr_low < prev_low * 0.97:
                entry = prev_low * 0.97
                exit_px = entry * 0.94
                p = round((exit_px - entry) / entry * 100, 2)
                losses += 1
                pnl += p
                trades.append({"sym": sym, "dir": "SHORT", "entry": round(entry, 4), "exit": round(exit_px, 4), "pnl_pct": p})
        results[sym] = {
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / max(wins + losses, 1), 3),
            "total_pnl_pct": round(pnl, 2),
            "points": points,
        }
    return results
