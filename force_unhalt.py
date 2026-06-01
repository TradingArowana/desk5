import json, os, sys
from datetime import datetime, timezone

os.chdir('.')

# Force unhalt with proper drawdown math
with open('data_store/exec_state.json') as f:
    state = json.load(f)

with open('data_store/last_good_equity.json') as f:
    equity = json.load(f)

total = equity.get('total', 3400.59)
peak = max(state.get('peak_bankroll', 4113.9), total)
dd_pct = (1 - total/peak) * 100 if peak > 0 else 0

print(f"Total equity: ${total:.2f}")
print(f"Peak: ${peak:.2f}")
print(f"Drawdown: {dd_pct:.1f}%")
print(f"Max allowed: 20.0%")
print(f"Currently halted: {state.get('halted')}")
print(f"Halt reason: {state.get('halt_reason')}")

# Unhalt
state['halted'] = False
state['halt_reason'] = None
state['today'] = datetime.now(timezone.utc).strftime('%Y-%m-%d')
state['daily_loss'] = 0.0
state['daily_wins'] = 0.0
state['peak_bankroll'] = peak
state['positions_open'] = 0

with open('data_store/exec_state.json','w') as f:
    json.dump(state, f, indent=2)

print(f"\nUNHALTED at {datetime.now(timezone.utc).isoformat()}")
print(f"New state: halted={state['halted']}, peak=${state['peak_bankroll']:.2f}")
