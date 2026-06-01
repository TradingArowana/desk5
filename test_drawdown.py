import sys
sys.path.insert(0, '.')
from workers.execution.hl_executor import _check_drawdown, _load_exec_state, _estimate_bankroll

state = _load_exec_state()
br = _estimate_bankroll(state)
print(f'Live equity: ${br:.2f}')
print(f"Peak: ${state['peak_bankroll']:.2f}")
ok, reason = _check_drawdown(state)
print(f'Drawdown check: ok={ok}, reason={reason}')
print(f"State halted: {state['halted']}")
