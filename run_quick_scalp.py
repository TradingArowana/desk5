import json, os, sys
from pathlib import Path
from workers.strategies.quick_scalp import refresh
wl = json.loads(Path('data_store/scanner_state.json').read_text()).get('watchlist',[])
refresh(wl[:5])
