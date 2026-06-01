#!/usr/bin/env bash
# Live signal scanner — fires strategies and auto-executes in LIVE_MODE
set -e
export LIVE_MODE=true
export PYTHONPATH=.
cd .

# Run alert cycle (auto-executes if LIVE_MODE=true in .env)
.venv/bin/python -c "
import os, sys
sys.path.insert(0, '.')
os.environ['LIVE_MODE'] = 'true'
from workers.execution.signal_alert import run_alert_cycle
alerts = run_alert_cycle(dry_run=False)
for a in alerts:
    print(a)
    print('---')
" > /tmp/live_signals.log 2>&1 || true

# Sync positions (SL/TP checks)
.venv/bin/python -c "
import sys
sys.path.insert(0, '.')
from workers.execution.hl_executor import sync_positions
result = sync_positions()
for alert in result.get('alerts', []):
    print(alert)
" >> /tmp/live_signals.log 2>&1 || true

# If there are alerts, send to Telegram (suppress 'no alerts' noise)
if [ -s /tmp/live_signals.log ]; then
    .venv/bin/python -c "
import sys, os
sys.path.insert(0, '.')
from workers.execution.signal_alert import run_alert_cycle
# Re-run to capture and send via Telegram
try:
    from hermes_tools import send_message
    with open('/tmp/live_signals.log') as f:
        body = f.read().strip()
    if body and len(body) > 20:
        # Only send if there are actual signals/executions
        if '🚨' in body or '🤖' in body or '🛑' in body or '🎯' in body:
            chunks = [body[i:i+3800] for i in range(0, len(body), 3800)]
            for chunk in chunks:
                send_message(message=chunk, target='telegram')
except Exception as e:
    print('Telegram send failed:', e)
" >> /tmp/live_signals.log 2>&1 || true
fi

exit 0
