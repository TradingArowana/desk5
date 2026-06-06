#!/usr/bin/env python3
"""
Desk5 Live Signal Scanner — one-shot runner for cron compatibility.
Restored from git commit 5ee2351; adapted for one-shot DESK_RUN_ONCE mode.
"""
import os
import sys
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(".")
DATA_STORE = PROJECT_ROOT / "data_store"
QUEUE = DATA_STORE / "all_signals.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
)
logger = logging.getLogger("desk5-runner")

sys.path.insert(0, str(PROJECT_ROOT))

def _keys(data):
    return {(s.get("coin"), s.get("direction"), s.get("entry_px")) for s in data}

def main():
    # 1. Snapshot queue before
    pre = []
    pre_k = set()
    if QUEUE.exists():
        try:
            pre = json.loads(QUEUE.read_text())
            pre_k = _keys(pre)
        except Exception:
            pass

    generator_errors = []

    # 2. Run signal generators
    try:
        from workers.strategies.hl_breakout import main as breakout_main
        breakout_main()
    except Exception as e:
        generator_errors.append(f"hl_breakout: {e}")

    try:
        from workers.strategies.hl_vol_squeeze import main as squeeze_main
        squeeze_main()
    except Exception as e:
        generator_errors.append(f"hl_vol_squeeze: {e}")

    try:
        from workers.strategies.quick_scalp import refresh, today_stats
        logging.getLogger("quick_scalp").setLevel(logging.WARNING)
        scanner_state = DATA_STORE / "scanner_state.json"
        wl = json.loads(scanner_state.read_text()).get("watchlist", []) if scanner_state.exists() else []
        refresh(wl)
    except Exception as e:
        generator_errors.append(f"quick_scalp: {e}")

    # 3. Aggregate queue changes
    post = []
    post_new = []
    if QUEUE.exists():
        try:
            post = json.loads(QUEUE.read_text())
            post_new = [s for s in post if (s.get("coin"), s.get("direction"), s.get("entry_px")) not in pre_k]
        except Exception:
            pass

    # 4. Execute queue
    exec_ran = False
    try:
        from workers.execution.exec_poller import main as exec_main
        exec_main()
        exec_ran = True
    except Exception as e:
        generator_errors.append(f"exec_poller: {e}")

    # 5. Sync positions
    sync_alerts = []
    try:
        from workers.execution.hl_executor import sync_positions
        sync_alerts = sync_positions().get("alerts", [])
    except Exception as e:
        generator_errors.append(f"sync_positions: {e}")

    # 6. Build report
    has_output = bool(post_new or sync_alerts or generator_errors)
    if not has_output and not exec_ran:
        # Nothing happened and execution didn't run — stay silent
        sys.exit(0)

    report = []
    report.append(f"🤖 DESK5 LIVE SCANNER — {datetime.now(timezone.utc).isoformat()}")

    if generator_errors:
        report.append("⚠️ ERRORS:")
        for e in generator_errors:
            report.append(f"  {e}")

    if post_new:
        report.append(f"📊 {len(post_new)} NEW SIGNAL(s):")
        for s in post_new[:10]:
            src = s.get("_source", "?")
            report.append(
                f"  {s.get('coin','?')} {s.get('direction','?')} @ {s.get('entry_px','?')} "
                f"SL={s.get('sl_px','?')} TP={s.get('tp_px','?')} [{src}]"
            )
        if len(post_new) > 10:
            report.append(f"  ... and {len(post_new)-10} more")

    if sync_alerts:
        report.append(f"🔄 SYNC ({len(sync_alerts)} alert(s)):")
        for a in sync_alerts[:10]:
            report.append(f"  {a}")
        if len(sync_alerts) > 10:
            report.append(f"  ... and {len(sync_alerts)-10} more")

    # Exec state summary
    try:
        from workers.execution.hl_executor import _load_exec_state
        st = _load_exec_state()
        report.append(
            f"📈 STATE today={st.get('today','?')} "
            f"wins=${st.get('daily_wins',0):.2f} "
            f"loss=${st.get('daily_loss',0):.2f} "
            f"open={st.get('positions_open',0)} "
            f"halted={st.get('halted',False)}"
        )
    except Exception:
        pass

    print("\n".join(report))
    sys.exit(0)

if __name__ == "__main__":
    main()
