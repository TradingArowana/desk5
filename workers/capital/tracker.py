"""
Live Capital Tracker + Milestone Alerts.
Monitors equity curve, sends Telegram alerts at profit checkpoints.
Now includes trade volume + count per 12h check-in window.
"""
import os, sys, json, logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workers.execution.hl_bridge import get_account_value

logger = logging.getLogger("capital_tracker")

STATE_DIR = Path(__file__).parent.parent.parent / "data_store"
TRACKER_PATH = STATE_DIR / "capital_tracker.json"
LEDGER_PATH = STATE_DIR / "live_ledger.json"

# Staged roadmap — TRUE total equity: $3,534
MILESTONES = [4000, 5000, 10000, 20000, 50000, 100000, 150000, 200000]
PHASE_1_TARGET = 3000.0
ULTIMATE_TARGET = 200000.0
START = 3534.41  # true total equity (baseline)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

LOAD_DOTENV = False
if not TELEGRAM_BOT_TOKEN:
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                k, _, v = line.partition("=")
                if k.strip() == "TELEGRAM_BOT_TOKEN":
                    TELEGRAM_BOT_TOKEN = v.strip()
                elif k.strip() == "TELEGRAM_CHAT_ID":
                    TELEGRAM_CHAT_ID = v.strip()


def send_telegram(msg: str):
    """Send alert to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured")
        return
    try:
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown"
        }, timeout=10)
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)


def load_tracker() -> dict:
    if TRACKER_PATH.exists():
        try:
            return json.loads(TRACKER_PATH.read_text())
        except Exception:
            pass
    return {
        "start": START,
        "milestones_hit": [],
        "peak": START,
        "last_alert": None,
        "last_balance": START,
        "balance_24h_ago": START,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def save_tracker(t: dict):
    TRACKER_PATH.write_text(json.dumps(t, indent=2))


def active_in_window(trade: dict, t1: datetime, t2: datetime) -> bool:
    """Return True if trade had any activity (opened or closed) in [t1, t2)."""
    open_ts = trade.get("open_at") or trade.get("opened_at")
    close_ts = trade.get("closed_at")
    if open_ts:
        try:
            dt = datetime.fromisoformat(open_ts)
            if t1 <= dt < t2:
                return True
        except Exception:
            pass
    if close_ts:
        try:
            dt = datetime.fromisoformat(close_ts)
            if t1 <= dt < t2:
                return True
        except Exception:
            pass
    return False


def compute_window_stats(t1: datetime, t2: datetime) -> dict:
    """Count trades + volume from ledger for the [t1,t2) window."""
    if not LEDGER_PATH.exists():
        return {"count": 0, "volume": 0.0, "closed_pnl": 0.0}
    try:
        trades = json.loads(LEDGER_PATH.read_text())
    except Exception:
        return {"count": 0, "volume": 0.0, "closed_pnl": 0.0}

    count = 0
    volume = 0.0
    closed_pnl = 0.0
    for t in trades:
        if active_in_window(t, t1, t2):
            count += 1
            sz = t.get("size", 0) or 0
            px = t.get("entry_px", 0) or 0
            volume += sz * px
            if t.get("status") == "CLOSED":
                closed_pnl += t.get("pnl", 0) or 0
    return {
        "count": count,
        "volume": round(volume, 2),
        "closed_pnl": round(closed_pnl, 2),
    }


def check_milestones():
    """Check equity, alert on milestones, track peak, report 12h deltas."""
    acc = get_account_value()
    total = acc.get("total", 0.0)
    tracker = load_tracker()
    now = datetime.now(timezone.utc)

    # Update peak
    if total > tracker["peak"]:
        tracker["peak"] = round(total, 2)
        logger.info("🚀 New peak: $%.2f", total)

    # --- Milestone checks ---
    new_hits = []
    for m in MILESTONES:
        if total >= m and m not in tracker["milestones_hit"]:
            tracker["milestones_hit"].append(m)
            new_hits.append(m)

    # --- 12-hour progress summary ---
    last_alert = tracker.get("last_alert")
    hours_since = 999
    if last_alert:
        try:
            last_dt = datetime.fromisoformat(last_alert)
            hours_since = (now - last_dt).total_seconds() / 3600
        except Exception:
            pass

    should_alert = hours_since >= 11 or new_hits

    if should_alert:
        tracker["last_alert"] = now.isoformat()
        last_balance = tracker.get("last_balance", START)
        balance_24h_ago = tracker.get("balance_24h_ago", START)
        delta_12h = total - last_balance
        delta_12h_pct = (delta_12h / last_balance * 100) if last_balance else 0
        delta_24h = total - balance_24h_ago
        delta_24h_pct = (delta_24h / balance_24h_ago * 100) if balance_24h_ago else 0

        # Sentiment
        if total >= tracker["peak"] * 0.995:
            sentiment = "🏔️ **At peak** — compounding is working"
        elif delta_12h > 0 and delta_24h > 0:
            sentiment = "🔥 **Bullish** — both 12h and 24h windows green"
        elif delta_12h > 0:
            sentiment = "📈 **Recovering** — last 12h positive, 24h still digesting"
        elif delta_24h > 0:
            sentiment = "⚠️ **Pullback** — 24h still green but last 12h down"
        else:
            sentiment = "📉 **Under pressure** — both windows red, patience required"

        # Compute 12h window stats
        t1 = now - timedelta(hours=12)
        t2 = now
        stats = compute_window_stats(t1, t2)

        # Open positions
        open_unrealized = 0.0
        open_count = 0
        try:
            trades = json.loads(LEDGER_PATH.read_text())
            open_trades = [t for t in trades if t.get("status") == "OPEN"]
            open_count = len(open_trades)
            open_unrealized = sum(t.get("unrealized_pnl", 0) or 0 for t in open_trades)
        except Exception:
            pass

        header = "📊 **12-Hour Check-in**"
        if new_hits:
            header = f"🎯 **MILESTONE HIT: ${new_hits[-1]:,}**"

        msg = (
            f"{header}\n"
            f"`{now.strftime('%Y-%m-%d %H:%M')} UTC`\n\n"
            f"💰 **Balance:** `${total:,.2f}`\n"
            f"🔄 **12h gain:** `${delta_12h:+,.2f}` (`{delta_12h_pct:+.2f}%`)\n"
            f"🔄 **24h gain:** `${delta_24h:+,.2f}` (`{delta_24h_pct:+.2f}%`)\n"
            f"{sentiment}\n\n"
            f"🏔️ **Peak:** `${tracker['peak']:,.2f}`\n\n"
            f"📦 **Trades (12h):** `{stats['count']}`\n"
            f"💸 **Volume (12h):** `${stats['volume']:,.0f}`\n"
            f"🔒 **Closed PnL (12h):** `${stats['closed_pnl']:+,.2f}`\n\n"
            f"🎯 **Phase 1:** `${PHASE_1_TARGET:,}` (`${max(0, PHASE_1_TARGET-total):,.0f}` to go)\n"
            f"🚀 **Ultimate:** `${ULTIMATE_TARGET:,}` (`${max(0, ULTIMATE_TARGET-total):,.0f}` to go)\n"
            f"📍 **Milestones:** `{len(tracker['milestones_hit'])}/9`\n\n"
            f"📂 **Open positions:** `{open_count}` (unrealized: `${open_unrealized:+,.2f}`)"
        )

        # Add milestone-specific footer on hits
        if new_hits:
            m = new_hits[-1]
            footer = f"\n\n🎉 **Milestone ${m:,} reached!**"
            if m >= ULTIMATE_TARGET:
                footer += (
                    "\n🎉 **ULTIMATE TARGET REACHED!**\n"
                    "Account now supports $20,000/mo sustainable withdrawals.\n"
                    "Remaining balance keeps compounding."
                )
            elif m >= PHASE_1_TARGET:
                remaining = ULTIMATE_TARGET - total
                footer += (
                    f"\n🛡️ **BURN RATE COVERED** ($6k/mo)\n"
                    f"You can start taking profit, or keep compounding to $200k.\n"
                    f"Next target: ${ULTIMATE_TARGET:,} (${remaining:,.2f} to go)"
                )
            else:
                remaining = PHASE_1_TARGET - total
                footer += f"\nNext: ${PHASE_1_TARGET:,} (${remaining:,.2f} to go)"
            msg += footer
            logger.info("Milestone %d alert sent", m)
        else:
            logger.info("Daily check-in sent")

        send_telegram(msg)

    # Save for next delta comparison
    tracker["balance_24h_ago"] = tracker.get("last_balance", START)
    tracker["last_balance"] = round(total, 2)
    tracker["updated_at"] = now.isoformat()
    save_tracker(tracker)

    return {
        "balance": round(total, 2),
        "peak": tracker["peak"],
        "delta_12h": round(total - tracker.get("last_balance", START), 2),
        "delta_24h": round(total - tracker.get("balance_24h_ago", START), 2),
        "milestones_hit": tracker["milestones_hit"],
        "phase_1_target": PHASE_1_TARGET,
        "ultimate_target": ULTIMATE_TARGET,
        "remaining_phase_1": round(max(0, PHASE_1_TARGET - total), 2),
        "remaining_ultimate": round(max(0, ULTIMATE_TARGET - total), 2),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    result = check_milestones()
    print(json.dumps(result, indent=2))
