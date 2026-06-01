#!/usr/bin/env python3
"""
QuickScalp autonomous signal runner.
Runs every 5 minutes: refreshes watchlist, generates signals, updates paper positions.
"""
import os
import sys
import json
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workers.strategies.quick_scalp import refresh, today_stats

WATCHLIST_PATH = Path(__file__).parent.parent.parent / "data_store" / "scanner_state.json"
INTERVAL = int(os.environ.get("DESK_QUICKSCALP_INTERVAL", "300"))  # 5 min default
RUN_ONCE = os.environ.get("DESK_RUN_ONCE", "0") == "1"


def _fmt_signal(s: dict) -> str:
    return f"  {s.get('coin','?')} {s.get('direction','?').upper()} @ {s.get('entry_px','?')} SL={s.get('sl_px','?')} TP={s.get('tp_px','?')}"


def main():
    if not RUN_ONCE:
        print(f"[{datetime.now(timezone.utc).isoformat()}] QuickScalp runner started")
    while True:
        try:
            wl = []
            if WATCHLIST_PATH.exists():
                wl = json.loads(WATCHLIST_PATH.read_text()).get("watchlist", [])
            if wl:
                signals = refresh(wl[:20])
                stats = today_stats()
                has_action = bool(signals) or stats.get("trades_today") or stats.get("pnl_today")
                if has_action:
                    print(f"[{datetime.now(timezone.utc).isoformat()}] Signals: {len(signals)}, Today trades: {stats['trades_today']}, PnL: {stats['pnl_today']}")
                    for s in signals:
                        print(_fmt_signal(s))
            else:
                print(f"[{datetime.now(timezone.utc).isoformat()}] No watchlist yet — waiting for alpha scanner")
            if RUN_ONCE:
                break
            time.sleep(INTERVAL)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[{datetime.now(timezone.utc).isoformat()}] ERROR: {exc}")
            if RUN_ONCE:
                sys.exit(1)
            time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
