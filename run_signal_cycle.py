import os, sys
sys.path.insert(0, '.')
os.environ['LIVE_MODE'] = 'true'
from workers.execution.signal_alert import run_alert_cycle
alerts = run_alert_cycle(dry_run=False)
for a in alerts:
    print(a)
    print('---')
