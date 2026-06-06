#!/usr/bin/env python3
"""
Weekly Self-Audit for Desk5 Trading System
Scans for: conflicting SL/TP, stale constants, anti-profit logic, broken fallbacks
"""
import json, re, sys
from pathlib import Path
from datetime import datetime, timezone

PROJECT = Path(".")
STATE_DIR = PROJECT / "data_store"
AUDIT_LOG = STATE_DIR / "audit_results.json"

def log_issue(severity, file, line, issue):
    return {"severity": severity, "file": str(file), "line": line, "issue": issue, "dt": datetime.now(timezone.utc).isoformat()}

def scan_hl_executor():
    issues = []
    path = PROJECT / "workers/execution/hl_executor.py"
    text = path.read_text()
    lines = text.splitlines()

    # 1. Check for profit-lock / anti-profit logic (skip lines that are only comments)
    for i, line in enumerate(lines):
        if line.strip().startswith("#") or line.strip().startswith('"""'):
            continue  # skip comment-only lines
        stripped = line.strip()
        if "throttle" in stripped.lower() and "profit" in stripped.lower():
            issues.append(log_issue("CRITICAL", path, i+1, "Anti-profit throttle detected"))

    # 2. Check recon block has correct SL/TP values
    # Find recon block where "reconciled": True appears
    recon_block = None
    for i, line in enumerate(lines):
        if '"reconciled": True' in line or '"reconciled": true' in line:
            # Check within +/- 15 lines for entry_px * factor patterns
            start = max(0, i-15)
            end = min(len(lines), i+15)
            recon_block = '\n'.join(lines[start:end])
            break
    if recon_block:
        sl_vals = [float(x) for x in re.findall(r'entry_px\]\s*\*\s*(\d+\.\d+)', recon_block)]
        # Filter to SL-related lines (should be ~0.98/1.02 for 2% SL)
        if sl_vals and not all(v in {0.98, 1.02} for v in sl_vals):
            issues.append(log_issue("CRITICAL", path, 0, f"Recon block SL values wrong: {sl_vals} (expected 0.98, 1.02)"))
    
    # Also check for known bad old values anywhere in file
    if "* 0.985" in text or "* 1.015" in text:
        issues.append(log_issue("CRITICAL", path, 0, "Found old wrong SL values (1.5%) — must be 2% (0.98/1.02)"))
    if "* 1.02" in text and '"reconciled"' in text:
        issues.append(log_issue("WARNING", path, 0, "Found 2%% factor in recon context — verify TP is 1.05/0.95 (5%%)"))
    if "* 0.98" in text and '"reconciled"' in text:
        issues.append(log_issue("WARNING", path, 0, "Found 2%% factor in recon context — verify TP is 1.05/0.95 (5%%)"))

    # 3. Check _START_BANKROLL is within 20% of capital tracker
    br_match = re.search(r'_START_BANKROLL\s*=\s*(\d+\.?\d*)', text)
    if br_match:
        code_br = float(br_match.group(1))
        ct = STATE_DIR / "capital_tracker.json"
        if ct.exists():
            d = json.loads(ct.read_text())
            live = max(d.get("last_balance", 0), d.get("peak", 0))
            if live > 0 and abs(code_br - live) / live > 0.20:
                issues.append(log_issue("CRITICAL", path, 0, f"_START_BANKROLL {code_br} >20% off live {live}"))

    # 4. Check MAX_DRAWDOWN is reasonable
    dd_match = re.search(r'MAX_DRAWDOWN_PCT\s*=\s*(\d+\.?\d*)', text)
    if dd_match:
        dd = float(dd_match.group(1))
        if dd < 15:
            issues.append(log_issue("CRITICAL", path, 0, f"MAX_DRAWDOWN_PCT {dd}% too tight — will halt on normal pullbacks"))
        if dd > 50:
            issues.append(log_issue("WARNING", path, 0, f"MAX_DRAWDOWN_PCT {dd}% very loose"))

    # 5. Check recon mismatch doesn't immediately halt
    if "api_coins != ledger_coins" in text and text.count("recon_retries") == 0:
        issues.append(log_issue("CRITICAL", path, 0, "Recon mismatch triggers immediate halt — no retry count"))

    return issues

def scan_quick_scalp():
    issues = []
    path = PROJECT / "workers/strategies/quick_scalp_v2.py"
    text = path.read_text()

    # 1. Ensure SL_PCT and TP_PCT are 0.02 and 0.10 (10% TP = 2.5× old)
    sl_pct = re.search(r'SL_PCT\s*=\s*(\d\.\d+)', text)
    tp_pct = re.search(r'TP_PCT\s*=\s*(\d\.\d+)', text)
    if sl_pct and float(sl_pct.group(1)) != 0.02:
        issues.append(log_issue("CRITICAL", path, 0, f"SL_PCT = {sl_pct.group(1)} (expected 0.02)"))
    if tp_pct and float(tp_pct.group(1)) != 0.10:
        issues.append(log_issue("CRITICAL", path, 0, f"TP_PCT = {tp_pct.group(1)} (expected 0.10)"))

    # 2. Ensure signal quality filters exist
    checks = ["vol_elevated", "up_count", "down_count", "strong_body"]
    for check in checks:
        if check not in text:
            issues.append(log_issue("CRITICAL", path, 0, f"Missing signal filter: {check}"))

    return issues

def main():
    all_issues = []
    all_issues.extend(scan_hl_executor())
    all_issues.extend(scan_quick_scalp())

    result = {
        "dt": datetime.now(timezone.utc).isoformat(),
        "issues_found": len(all_issues),
        "critical_count": sum(1 for i in all_issues if i["severity"] == "CRITICAL"),
        "issues": all_issues
    }

    AUDIT_LOG.write_text(json.dumps(result, indent=2))

    if result["critical_count"] > 0:
        print(f"🔴 CRITICAL ISSUES: {result['critical_count']}")
        for i in all_issues:
            if i["severity"] == "CRITICAL":
                print(f"  {i['file']}:{i['line']} → {i['issue']}")
        sys.exit(1)
    else:
        print(f"✅ Audit clean — {len(all_issues)} total issues, 0 critical")
        sys.exit(0)

if __name__ == "__main__":
    main()
