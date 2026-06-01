import sys
sys.path.insert(0, '.')
from workers.execution.hl_executor import sync_positions
result = sync_positions()
import json
print(json.dumps(result, indent=2))
