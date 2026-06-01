import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from workers.execution.hl_bridge import get_account_value, get_positions
from workers.execution.exec_poller import load_queue

# Live account snapshot
acc = get_account_value()
total = acc["total"]
print(f'Account: ${total:.2f}')

# Live positions
pos = get_positions()
print(f'Open positions: {len(pos)}')
for p in pos:
    u = p.get('unrealized_pnl', 0)
    print(f"  {p['coin']} {p['side']} size={p['size']} entry={p['entry_px']} uPNL=${u:.2f}")

# Check unified queue
q = load_queue()
print(f'Signal queue: {len(q)} signals')
for s in q[:5]:
    score = s.get('_score', 'N/A')
    print(f"  {s.get('coin')} {s.get('direction')} score={score}")
