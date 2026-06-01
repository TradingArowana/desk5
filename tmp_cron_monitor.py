#!/usr/bin/env python3
"""Wrapper to run live_monitor for cron."""
import sys, json, logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from workers.execution.live_monitor import check_positions
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
result = check_positions()
print(json.dumps(result, indent=2))
