#!/usr/bin/env python3
import json, urllib.request, time, os, sys

log_file = "/tmp/desk-supervisor-health.jsonl"

def read_last_state():
    if not os.path.exists(log_file):
        return False, 0
    with open(log_file, "r") as f:
        lines = [l.strip() for l in f if l.strip()]
        if not lines:
            return False, 0
        try:
            d = json.loads(lines[-1])
            last_failed = bool(d.get("failed", False))
            last_consecutive = int(d.get("consecutive_errors", d.get("restart_needed", 0)))
            return last_failed, last_consecutive
        except Exception:
            return False, 0

def fetch(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None, str(e)

def main():
    last_failed, last_consecutive = read_last_state()

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    health_status, _ = fetch("http://localhost:8080/api/health")
    signals_status, _ = fetch("http://localhost:8080/api/strategies/quick_scalp/signals")

    health_ok = health_status is not None and health_status < 400
    signals_ok = signals_status is not None and signals_status < 400
    failed = not (health_ok and signals_ok)
    consecutive = last_consecutive + 1 if failed else 0
    restart_needed = (last_failed and failed) or consecutive >= 2

    log_entry = {
        "timestamp": now,
        "status": "ok" if not failed else "fail",
        "health_code": health_status,
        "signals_code": signals_status,
        "health_ok": health_ok,
        "signals_ok": signals_ok,
        "failed": failed,
        "prev_failed": last_failed,
        "consecutive_errors": consecutive,
        "restart_needed": restart_needed,
        "restarted": False,
        "action": "restart" if restart_needed else "none",
        "restart_reason": ""
    }

    if restart_needed:
        print("\nRESTART_NEEDED\n")

    with open(log_file, "a") as f:
        f.write(json.dumps(log_entry) + "\n")

    print(json.dumps(log_entry))

    if restart_needed:
        sys.exit(2)
    sys.exit(0)

if __name__ == "__main__":
    main()
