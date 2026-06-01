import sys, json, os
from datetime import datetime, timezone
sys.path.insert(0, '.')

from workers.execution.hl_bridge import get_account_value, get_positions, get_open_orders

acc = get_account_value()
print(f'EQUITY: total=${acc["total"]:.2f} perp=${acc["perp"]:.2f} spot_avail=${acc["spot_available"]:.2f}')

pos = get_positions()
print(f'POSITIONS: {len(pos)} open')

orders = get_open_orders()
print(f'ORDERS: {len(orders)} open')

# Unhalt
with open('data_store/exec_state.json') as f:
    state = json.load(f)
state['halted'] = False
state['halt_reason'] = None
state['today'] = datetime.now(timezone.utc).strftime('%Y-%m-%d')
state['daily_loss'] = 0.0
state['daily_wins'] = 0.0
state['positions_open'] = len(pos)
with open('data_store/exec_state.json','w') as f:
    json.dump(state, f, indent=2)
print(f'STATE: halted={state["halted"]} peak=${state["peak_bankroll"]:.2f}')
print('DESK IS LIVE')
