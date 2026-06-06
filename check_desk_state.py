import json
from pathlib import Path
from datetime import datetime, timezone

base = Path('.')

exec_state = json.loads((base / 'data_store/exec_state.json').read_text())
print('=== exec_state ===')
for k in ['daily_loss', 'daily_wins', 'peak_bankroll', 'total_trades', 'total_wins', 'positions_open']:
    print(f'{k}: {exec_state.get(k)}')

ledger = json.loads((base / 'data_store/live_ledger.json').read_text())
open_pos = [t for t in ledger if t.get('status') == 'OPEN']
print(f'\n=== live_ledger ===')
print(f'Total entries: {len(ledger)}')
print(f'Open positions: {len(open_pos)}')

unrealized = sum(float(p.get('unrealized_pnl', 0)) for p in open_pos)
print(f'Total unrealized PnL: {unrealized:.4f}')

if open_pos:
    for p in open_pos:
        print(f"- {p['coin']} {p.get('direction','?')} entry={p.get('entry_px','?')} unreal={float(p.get('unrealized_pnl', 0)):.4f}")
else:
    print('No open positions')

# realized today (UTC)
today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
today_entries = [
    t for t in ledger
    if t.get('status') in ('FILLED','CLOSED') and t.get('closed_at') and today_str in t.get('closed_at','')
]
realized_today = sum(float(t.get('pnl', 0)) for t in today_entries)
print(f'\nRealized PnL today ({today_str}): {realized_today:.4f}')
