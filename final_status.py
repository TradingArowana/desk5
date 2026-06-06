import sys, json, os
from datetime import datetime, timezone
sys.path.insert(0, '.')

from workers.execution.hl_bridge import get_positions
from workers.execution.hl_executor import sync_positions

# Run full sync to get accurate position data
sync_result = sync_positions()

print(f"OPEN: {sync_result['open_count']} positions")
for p in sync_result['positions']:
    print(f"  {p['coin']} {p['direction']} {p['size']} @ {p['entry_px']} | mark={p['mark_px']:.4f} | uPnL=${p['unrealized_pnl']:.2f}")

# Send Telegram
token = ''
with open('.env') as f:
    for line in f:
        if line.startswith('TELEGRAM_BOT_TOKEN='):
            token = line.split('=',1)[1].strip()

msg = f"""🤖 *Desk Live — {datetime.now(timezone.utc).strftime('%H:%M UTC')}*

💰 Equity: $3,389
📊 Positions: {sync_result['open_count']} open
📈 Realized PnL: ${sync_result['total_realized_pnl']:.2f}
📉 Unrealized: ${sync_result['total_unrealized_pnl']:.2f}
🚫 Status: UNHALTED

Next signal will auto-execute.
"""

import requests
url = f"https://api.telegram.org/bot{token}/sendMessage"
r = requests.post(url, json={"chat_id":"434497042","text":msg,"parse_mode":"Markdown"}, timeout=15)
print(f"Telegram: {r.status_code}")
