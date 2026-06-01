"""
HL Breakout Signal Engine — uses native Hyperliquid candles.
Generates momentum breakout signals with real prices.
"""
import json, logging, sys, time
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any

sys_path_inserted = False
for p in [Path(__file__).parent.parent.parent]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
        sys_path_inserted = True

from workers.caches.hl_candle_fetcher import fetch_hl_candles
from workers.strategies.signal_aggregator import is_tradeable, save_to_queue

logger = logging.getLogger("hl_breakout")

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
STATE_DIR.mkdir(parents=True, exist_ok=True)
SIGNALS_PATH = STATE_DIR / "hl_breakout_signals.json"

# Coin mapping: scanner symbols → HL coin names
COIN_MAP = {
    "BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "DOGE": "DOGE",
    "AVAX": "AVAX", "LINK": "LINK", "BNB": "BNB", "XRP": "XRP",
    "ADA": "ADA", "DOT": "DOT", "ATOM": "ATOM", "CRV": "CRV",
    "LDO": "LDO", "GMX": "GMX", "SUI": "SUI", "PAXG": "PAXG",
    "AAVE": "AAVE", "COMP": "COMP", "TRX": "TRX",
    "APE": "APE", "APT": "APT", "BCH": "BCH", "SNX": "SNX",
    "INJ": "INJ", "STX": "STX", "PEPE": "kPEPE",
    "OP": "OP", "ARB": "ARB", "WBTC": "WBTC",
    "WETH": "ETH", "USDC": "USDC", "USDT": "USDT",
    "FTM": "FTM", "RNDR": "RNDR", "MKR": "MKR", "WLD": "WLD",
    "MATIC": "MATIC", "LTC": "LTC", "DYDX": "DYDX", "CFX": "CFX",
}

def generate_signals(watchlist: List[Dict[str, Any]], max_coins: int = 20) -> List[Dict]:
    """Generate breakout signals using real Hyperliquid candles."""
    signals = []
    
    for coin_data in watchlist[:max_coins]:
        sym = coin_data.get("symbol", "").upper()
        hl_coin = COIN_MAP.get(sym, sym)
        
        if not is_tradeable(hl_coin):
            continue
            
        candles = fetch_hl_candles(hl_coin, "15m", 4)
        if not candles or len(candles) < 4:
            continue
            
        recent = candles[-12:] if len(candles) >= 12 else candles
        closes = [c["c"] for c in recent]
        highs = [c["h"] for c in recent]
        lows = [c["l"] for c in recent]
        
        last = closes[-1]
        prior_high = max(highs[:-1]) if len(highs) > 1 else highs[0]
        prior_low = min(lows[:-1]) if len(lows) > 1 else lows[0]
        
        # Volume proxy: sum of (high-low) * close
        vol_proxy = sum((c["h"] - c["l"]) * c["c"] for c in recent)
        avg_vol = vol_proxy / len(recent)
        
        # Breakout above prior h1_high → LONG
        if last > prior_high * 1.001 and vol_proxy >= avg_vol * 1.1:
            sl = last * 0.97
            tp = last * 1.06
            signals.append({
                "coin": hl_coin,
                "direction": "LONG",
                "entry_px": round(last, 6),
                "sl_px": round(sl, 6),
                "tp_px": round(tp, 6),
                "dt": datetime.now(timezone.utc).isoformat(),
                "vol_24h": coin_data.get("change_24h", 0),
                "volume_24h": coin_data.get("volume_24h", 0),
                "_source": "hl_breakout",
            })
            
        # Breakout below prior h1_low → SHORT
        elif last < prior_low * 0.999 and vol_proxy >= avg_vol * 1.1:
            sl = last * 1.03
            tp = last * 0.94
            signals.append({
                "coin": hl_coin,
                "direction": "SHORT",
                "entry_px": round(last, 6),
                "sl_px": round(sl, 6),
                "tp_px": round(tp, 6),
                "dt": datetime.now(timezone.utc).isoformat(),
                "vol_24h": coin_data.get("change_24h", 0),
                "volume_24h": coin_data.get("volume_24h", 0),
                "_source": "hl_breakout",
            })
            
    logger.info("Generated %d HL breakout signals", len(signals))
    return signals


def main():
    """One-shot signal generation + queue push."""
    scanner = STATE_DIR / "scanner_state.json"
    if not scanner.exists():
        logger.warning("No scanner_state.json found")
        return []
        
    wl = json.loads(scanner.read_text()).get("watchlist", [])
    if not wl:
        logger.warning("Watchlist empty")
        return []
        
    signals = generate_signals(wl)
    
    # Save to strategy file
    if signals:
        SIGNALS_PATH.write_text(json.dumps(signals, indent=2, default=str))
        # Push to unified queue
        save_to_queue(signals)
        
    for s in signals[:5]:
        logger.info("%s %s @ %.4f SL=%.4f TP=%.4f",
                    s["coin"], s["direction"], s["entry_px"], s["sl_px"], s["tp_px"])
                    
    return signals


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
