"""Config loader for local signing bot."""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

DESK5_SERVER_URL = os.environ.get("DESK5_SERVER_URL", "http://localhost:8080")
HL_API_KEY       = os.environ.get("HL_API_KEY", "")
AUTO_EXECUTE     = os.environ.get("AUTO_EXECUTE", "true").lower() == "true"

MAX_POSITIONS      = int(os.environ.get("MAX_POSITIONS", "3"))
RISK_PER_TRADE_PCT = float(os.environ.get("RISK_PER_TRADE_PCT", "0.015"))
MAX_DAILY_LOSS_USD = float(os.environ.get("MAX_DAILY_LOSS_USD", "50"))
MAX_LEVERAGE       = int(os.environ.get("MAX_LEVERAGE", "2"))
MAX_DRAWDOWN_PCT   = float(os.environ.get("MAX_DRAWDOWN_PCT", "20"))

POLL_INTERVAL_SEC  = int(os.environ.get("POLL_INTERVAL_SEC", "300"))
STATE_FILE         = BASE_DIR / "state.json"

if not HL_API_KEY or not HL_API_KEY.startswith("0x"):
    raise RuntimeError("HL_API_KEY missing or invalid in .env (must be 0x... hex)")
