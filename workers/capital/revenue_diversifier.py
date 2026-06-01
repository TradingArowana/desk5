"""
Revenue Diversifier for Desk5.
Identifies non-trading revenue streams and alerts desk for manual/automated action.
1. Hyperliquid funding rate harvest (negative funding = longs get paid)
2. Social sentiment alpha (momentum from volume spike + sentiment)
3. Asymmetric 10% pocket (altcoin perpetuals outside top-30)
"""
import json
import logging
import os
import requests
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List

logger = logging.getLogger("revenue_div")

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# 10% asymmetric pocket allocation
ASYMMETRIC_PCT = 0.10


def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)


def get_funding_opportunities(threshold: float = -0.0005) -> List[dict]:
    """Find coins with negative funding (get paid to hold long)."""
    try:
        r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"}, timeout=15)
        r.raise_for_status()
        meta, ctxs = r.json()
        perps = meta["universe"]
        opps = []
        for idx, p in enumerate(perps):
            ctx = ctxs[idx]
            funding = float(ctx.get("funding", 0))
            mark = float(ctx.get("markPx", 0))
            oi = float(ctx.get("openInterest", 0))
            # Negative funding = longs receive payment
            if funding <= threshold and oi > 50000:  # min $50k OI for liquidity
                opps.append({
                    "type": "funding_harvest",
                    "coin": p["name"],
                    "funding_8h": round(funding, 6),
                    "mark_px": mark,
                    "oi": oi,
                    "score": round(abs(funding) * 10000, 1),  # higher = better
                    "direction": "LONG",
                    "note": f"Get paid {abs(funding):.5f}% every 8h to hold long",
                })
        opps.sort(key=lambda x: x["score"], reverse=True)
        return opps[:5]
    except Exception as exc:
        logger.warning("Funding fetch failed: %s", exc)
        return []


def get_asymmetric_pocket_signals() -> List[dict]:
    """Scan for high-volatility altcoins outside top 30 for 10% allocation."""
    try:
        # Fetch top coins from CoinGecko
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 100, "page": 1, "sparkline": False},
            timeout=15,
        )
        r.raise_for_status()
        coins = r.json()
        
        # Filter: rank 31-100, vol/market cap > 0.15 (high velocity), 24h change > 8%
        candidates = []
        for c in coins[30:]:  # skip top 30
            mc = c.get("market_cap") or 1
            vol = c.get("total_volume") or 0
            change = c.get("price_change_percentage_24h") or 0
            symbol = c.get("symbol", "").upper()
            if mc > 50000000 and vol / mc > 0.15 and abs(change) > 8:
                candidates.append({
                    "type": "asymmetric",
                    "coin": symbol,
                    "price": c.get("current_price"),
                    "change_24h": round(change, 2),
                    "vol_mc_ratio": round(vol / mc, 2),
                    "mc_rank": c.get("market_cap_rank"),
                    "score": round(abs(change) * (vol / mc) * 10, 1),
                    "direction": "LONG" if change > 0 else "SHORT",
                    "note": f"Rank #{c.get('market_cap_rank')} | {change:+.1f}% 24h | vol/mc {vol/mc:.2f}",
                })
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:5]
    except Exception as exc:
        logger.warning("Asymmetric scan failed: %s", exc)
        return []


def run_revenue_scan() -> dict:
    """Run all diversifier scans and emit alerts."""
    funding = get_funding_opportunities()
    asym = get_asymmetric_pocket_signals()

    alerts = []

    # Funding harvest alerts
    if funding:
        for opp in funding[:3]:
            alerts.append(
                f"💰 *Funding Harvest*\n"
                f"{opp['coin']} {opp['direction']}\n"
                f"8h funding: `{opp['funding_8h']:.5f}`\n"
                f"{opp['note']}"
            )

    # Asymmetric pocket
    if asym:
        for opp in asym[:3]:
            alerts.append(
                f"🎯 *Asymmetric Pocket* ({ASYMMETRIC_PCT*100:.0f}% allocation)\n"
                f"{opp['coin']} {opp['direction']} @ ${opp['price']}\n"
                f"{opp['note']}"
            )

    # Send all alerts
    for msg in alerts:
        send_telegram(msg)

    result = {
        "dt": datetime.now(timezone.utc).isoformat(),
        "funding_opps": funding,
        "asymmetric_opps": asym,
        "alerts_sent": len(alerts),
    }

    # Persist
    out_path = STATE_DIR / "revenue_scan.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))

    logger.info("Revenue scan: %d funding, %d asymmetric, %d alerts sent",
                len(funding), len(asym), len(alerts))

    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = run_revenue_scan()
    print(json.dumps({
        "funding": len(out["funding_opps"]),
        "asymmetric": len(out["asymmetric_opps"]),
        "alerts": out["alerts_sent"],
    }, indent=2))
    sys.exit(0)
