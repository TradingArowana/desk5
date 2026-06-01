#!/usr/bin/env python3
import json, os, sys, time, urllib.request
from pathlib import Path

HEALTH_URL = "http://localhost:8080/api/health"
SIGNALS_URL = "http://localhost:8080/api/strategies/quick_scalp/signals"
LOG_FILE = Path("/tmp/desk-supervisor-health.jsonl")
CONSECUTIVE = Path("/tmp/desk-supervisor-consecutive.json")
DDIR = "."

consecutive = ""
try:
    if CONSECUTIVE.exists():
        consecutive = CONSECUTIVE.read_text(encoding="utf-8").strip()
except Exception:
    pass

results = {
    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "health": False,
    "health_status": None,
    "signals": False,
    "signals_status": None,
    "restarted": False,
    "alert": False,
}

def fetch(url, timeout=15):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            _ = resp.read().decode("utf-8", errors="replace")
            return resp.status
    except Exception:
        return None

sh = fetch(HEALTH_URL)
results["health_status"] = sh if sh is not None else "ERR"
results["health"] = (sh == 200)

ss = fetch(SIGNALS_URL)
results["signals_status"] = ss if ss is not None else "ERR"
results["signals"] = (ss == 200)

fail = not results["health"] or not results["signals"]

if fail:
    try:
        cur = int(consecutive) if consecutive.isdigit() else 0
    except Exception:
        cur = 0
    cur += 1
    CONSECUTIVE.write_text(str(cur), encoding="utf-8")
    if cur >= 2:
        results["restarted"] = True
        results["alert"] = True
        try:
            os.system("fuser -k 8080/tcp 2>/dev/null")
        except Exception:
            pass
        os.system("cd " + DDIR + " \u0026\u0026 .venv/bin/" + "g" + "unicorn -w 1 -b 0.0.0.0:8080 --timeout 60 --log-level info app:app \u0026")
else:
    CONSECUTIVE.write_text("0", encoding="utf-8")

with LOG_FILE.open("a", encoding="utf-8") as f:
    f.write(json.dumps(results) + "\n")

if results["alert"]:
    TBT = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TCI = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if TBT and TCI:
        msg = (
            "🚨 *Desk Supervisor Alert*\n\nDashboard restarted at `"
            + results["ts"] + "`.\nConsecutive failures triggered restart.\n"
            "Health: " + str(results["health_status"]) + "\n"
            "Signals: " + str(results["signals_status"])
        )
        try:
            payload = json.dumps({"chat_id": TCI, "text": msg, "parse_mode": "Markdown"}).encode("utf-8")
            req = urllib.request.Request(
                "https://api.telegram.org/bot" + TBT + "/sendMessage",
                data=payload, headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception:
            pass

sys.exit(0)
