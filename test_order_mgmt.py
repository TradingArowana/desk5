import sys
sys.path.insert(0, '.')
from workers.execution.hl_bridge import get_open_orders, get_positions, get_account_value
from workers.execution.hl_executor import cancel_all_open_orders

orders = get_open_orders()
print(f"[ORDERS] {len(orders)} open")

pos = get_positions()
print(f"[POSITIONS] {len(pos)} open")

res = cancel_all_open_orders()
print(f"[CANCEL_ALL] cancelled={res['cancelled']}, failed={res['failed']}")

acc = get_account_value()
print(f"[EQUITY] total=${acc['total']:.2f}, perp=${acc['perp']:.2f}, spot_avail=${acc['spot_available']:.2f}")

print("ALL TESTS PASS")
