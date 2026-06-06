"""
Desk5 dashboard API.
Merges social sentinel health + QuickScalp strategy endpoints.
"""

import os
import json
import yaml
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from flask import Flask, jsonify, request, render_template
from collections import defaultdict
from functools import wraps

# ---------------------------------------------------------------------------
# Lightweight per-IP rate limiter (no external deps)
# ---------------------------------------------------------------------------
_RATE_LIMITS = defaultdict(list)
_MAX_REQ_MIN = 120  # generous: 120 req/min per IP

def rate_limit(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or '127.0.0.1')
        ip = ip.split(',')[0].strip()
        now = time.monotonic()
        window = _RATE_LIMITS[ip]
        window[:] = [t for t in window if now - t < 60]
        if len(window) >= _MAX_REQ_MIN:
            return jsonify({"error": "Rate limit exceeded: 120 req/min"}), 429
        window.append(now)
        return f(*args, **kwargs)
    return decorated

app = Flask(__name__)

PROJECT_ROOT = Path(__file__).parent
CONFIG_PATH = PROJECT_ROOT / "config" / "sentinel.yaml"
DB_PATH = PROJECT_ROOT / "data_store" / "sentinel.db"

# ---------------------------------------------------------------------------
# Sentinel helpers
# ---------------------------------------------------------------------------

def load_sentinel_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}

def get_db():
    conn = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn

# ---------------------------------------------------------------------------
# QuickScalp helpers
# ---------------------------------------------------------------------------

def _load_scanner_state() -> dict:
    p = PROJECT_ROOT / "data_store" / "scanner_state.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}

def _load_signals() -> List[Dict[str, Any]]:
    p = PROJECT_ROOT / "data_store" / "quick_scalp_signals.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return []

def _load_trades() -> List[Dict[str, Any]]:
    p = PROJECT_ROOT / "data_store" / "quick_scalp_trades.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return []

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# ---- Health --------------------------------------------------------------

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})

# ---- Sentinel ------------------------------------------------------------

@app.route("/api/sentinel/nitter/health")
def nitter_health():
    cfg = load_sentinel_config()
    nitter_cfg = cfg.get("sentinel", {}).get("nitter", {})
    try:
        from workers.social_sentinel.nitter_scraper import NitterScraper
        scraper = NitterScraper(
            instances=nitter_cfg.get("instances"),
            timeout=nitter_cfg.get("request_timeout", 15),
            retries=nitter_cfg.get("retries", 3),
        )
        result = scraper.health()
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/sentinel/status")
def sentinel_status():
    cfg = load_sentinel_config()
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS total FROM social_posts WHERE fetched_at > datetime('now', '-1 day')"
        )
        total_24h = cur.fetchone()["total"]
        cur.execute(
            "SELECT platform, COUNT(*) AS cnt FROM social_posts WHERE fetched_at > datetime('now', '-1 day') GROUP BY platform"
        )
        by_platform = {row["platform"]: row["cnt"] for row in cur.fetchall()}
        conn.close()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify(
        {
            "posts_last_24h": total_24h,
            "by_platform": by_platform,
            "config_loaded": bool(cfg),
        }
    ), 200

# ---- Market Scanner ------------------------------------------------------

@app.route("/api/market_scanner/watchlist")
def api_watchlist():
    state = _load_scanner_state()
    watchlist = state.get("watchlist", [])
    return jsonify({
        "watchlist": watchlist,
        "count": len(watchlist),
        "last_scan": state.get("last_scan"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })

# ---- QuickScalp ----------------------------------------------------------

@app.route("/api/strategies/quick_scalp/signals")
def api_quick_scalp_signals():
    signals = [s for s in _load_signals() if s.get("status") == "OPEN"]
    return jsonify({
        "signals": signals,
        "count": len(signals),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })

@app.route("/api/strategies/quick_scalp/backtest")
def api_quick_scalp_backtest():
    symbols = request.args.getlist("symbol") or []
    if not symbols:
        state = _load_scanner_state()
        symbols = [c["symbol"] for c in state.get("watchlist", [])[:20]]
    if not symbols:
        return jsonify({"error": "No symbols provided and watchlist empty."}), 400
    try:
        from workers.strategies.backtest_quick_scalp import run_backtest
        results = run_backtest(symbols)
        return jsonify({
            "backtest": results,
            "symbols_tested": symbols,
            "run_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.route("/api/strategies/quick_scalp/stats")
def api_quick_scalp_stats():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    trades = _load_trades()
    day_trades = [t for t in trades if t.get("dt", "").startswith(today)]
    wins = [t for t in day_trades if t.get("pnl_usd", 0) > 0]
    pnl = sum(t.get("pnl_usd", 0) for t in day_trades)
    open_sigs = [s for s in _load_signals() if s.get("status") == "OPEN"]
    return jsonify({
        "trades_today": len(day_trades),
        "wins": len(wins),
        "losses": len(day_trades) - len(wins),
        "win_rate": round(len(wins) / len(day_trades), 4) if day_trades else 0.0,
        "pnl_today": round(pnl, 4),
        "open_signals": len(open_sigs),
        "total_trades": len(trades),
    })

# ---- Dashboard -----------------------------------------------------------

@app.route("/")
def dashboard():
    return render_template("index.html")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# ---- Deposits ------------------------------------------------------------

@app.route("/api/deposit/solana")
def api_solana_deposit():
    try:
        from workers.deposit_monitor.solana_tracker import get_sol_address
        addr = get_sol_address()
    except RuntimeError as exc:
        return jsonify({
            "status": "not_configured",
            "error": str(exc),
            "address": None,
            "qr_url": None,
            "instructions": [
                "Create a Solana wallet in Phantom (https://phantom.app/) or Solflare (https://solflare.com/)",
                "Write the recovery phrase offline (paper/hardware wallet). NEVER share it.",
                "Copy the public address (starts with a letter, e.g. 9CtWvY...)",
                "Set environment variable: export DESK_SOLANA_ADDRESS='your_address'",
                "Restart desk5: bash start_dashboard.sh",
            ],
        }), 503
    qr = f"https://quickchart.io/qr?text=solana:{addr}&size=200"
    return jsonify({
        "chain": "solana",
        "address": addr,
        "qr_url": qr,
        "status": "ready",
        "deposit_us": "Send SOL or SPL tokens to this address. Deposits polled every 60s.",
    })

# ---- Capital / Paper Gate ------------------------------------------------

@app.route("/api/capital")
def api_capital():
    from workers.capital.tracker import get_capital_snapshot
    try:
        return jsonify(get_capital_snapshot()), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.route("/api/capital/paper_trade", methods=["POST"])
def api_paper_trade():
    from workers.capital.tracker import record_paper_trade
    try:
        payload = request.get_json(force=True) or {}
        result = record_paper_trade(payload)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# Hyperliquid (public endpoints, no auth)

HL_MAINNET = "https://api.hyperliquid.xyz"

@rate_limit
@app.route("/api/hl/positions")
def api_hl_positions():
    """Public live mark prices + optional positions if DESK_HL_ADDRESS set."""
    import requests
    hl_addr = os.environ.get("DESK_HL_ADDRESS")
    try:
        mids = requests.post(HL_MAINNET + "/info", json={"type": "allMids"}, timeout=10).json()
        meta = requests.post(HL_MAINNET + "/info", json={"type": "meta"}, timeout=10).json()
        universe = {c["name"]: c for c in meta.get("universe", [])}
        positions = []
        if hl_addr:
            state = requests.post(HL_MAINNET + "/info", json={"type": "clearinghouseState", "user": hl_addr}, timeout=10).json()
            for pos in state.get("assetPositions", []):
                p = pos.get("position", {})
                coin = p.get("coin", "")
                mark = float(mids.get(coin, 0))
                entry = float(p.get("entryPx", 0))
                sz = float(p.get("szi", 0))
                side = "LONG" if sz > 0 else "SHORT"
                unrealized = (mark - entry) * abs(sz) if sz else 0
                if side == "SHORT":
                    unrealized = -unrealized
                positions.append({
                    "coin": coin, "side": side, "size": abs(sz),
                    "entry_px": round(entry, 4), "mark_px": round(mark, 4),
                    "unrealized_pnl": round(unrealized, 4),
                    "leverage": p.get("leverage", {}).get("value", 1),
                })
        else:
            # Show top 20 valid coins (no @ numeric internals) sorted by mark price
            top = sorted(
                [(c, v) for c, v in mids.items() if c[0].isalpha()],
                key=lambda x: float(x[1]) if isinstance(x[1], (int, float, str)) else 0,
                reverse=True,
            )[:20]
            for coin, mark in top:
                positions.append({
                    "coin": coin, "side": "—", "size": 0.0,
                    "entry_px": None,
                    "mark_px": round(float(mark), 4),
                    "unrealized_pnl": 0.0, "leverage": 1,
                    "note": "paper — set DESK_HL_ADDRESS for live",
                })
        return jsonify({
            "positions": positions, "count": len(positions),
            "mode": "live" if hl_addr else "paper_public",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@rate_limit
@app.route("/api/hl/funding")
def api_hl_funding():
    try:
        import requests
        meta = requests.post(HL_MAINNET + "/info", json={"type": "metaAndAssetCtxs"}, timeout=10).json()
        rates = []
        for coin, ctx in zip(meta[0].get("universe", []), meta[1]):
            rates.append({
                "coin": coin["name"],
                "funding": float(ctx.get("funding", 0)),
                "mark": float(ctx.get("markPx", 0)),
                "open_interest": float(ctx.get("openInterest", 0)),
            })
        return jsonify({
            "funding_rates": rates[:10],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# ---- Simulation Engine ---------------------------------------------------

@app.route("/api/simulation/report")
def api_simulation():
    from workers.simulation.engine import run_all
    try:
        bankroll = request.args.get("bankroll", default=1000.0, type=float)
        report = run_all(start_bankroll=bankroll)
        return jsonify(report), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# ---- Vol Squeeze ---------------------------------------------------------

@app.route("/api/strategies/vol_squeeze/signals")
def api_vol_squeeze():
    from workers.strategies.vol_squeeze import run_cycle as vs_run
    try:
        result = vs_run()
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"error": str(exc), "signals": [], "count": 0}), 500

# ---- News Sentinel -------------------------------------------------------

@app.route("/api/sentinel/news")
def api_news_sentinel():
    from workers.sentinel.news_sentinel import get_active_signals
    try:
        signals = get_active_signals()
        return jsonify({"signals": signals, "count": len(signals)}), 200
    except Exception as exc:
        return jsonify({"error": str(exc), "signals": [], "count": 0}), 500

# ---- Commodity Sentinel --------------------------------------------------

@app.route("/api/sentinel/commodities")
def api_commodity_sentinel():
    from workers.sentinel.commodity_sentinel import run_cycle as comm_run
    try:
        result = comm_run()
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"error": str(exc), "brent": None, "wti": None}), 500

# ---- Momentum Fade -------------------------------------------------------

@app.route("/api/strategies/momentum_fade/signals")
def api_momentum_fade():
    from workers.strategies.momentum_fade import run_cycle as mf_run
    try:
        result = mf_run()
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"error": str(exc), "signals": [], "count": 0}), 500

# ---- Shadow Execution (Paper with real mark fills) -----------------------

@app.route("/api/shadow/status")
def api_shadow_status():
    from workers.execution.shadow_exec import _load_state
    try:
        state = _load_state()
        peak = state.get("peak_bankroll", state["start_bankroll"])
        current = state.get("current_bankroll", peak)
        dd = (peak - current) / peak * 100 if peak > 0 else 0
        return jsonify({
            "mode": "PAPER (shadow)",
            "start_bankroll": state.get("start_bankroll", 1000),
            "current_bankroll": round(current, 2),
            "peak_bankroll": round(peak, 2),
            "drawdown_pct": round(dd, 2),
            "open_positions": len(state.get("open_positions", [])),
            "total_trades": state.get("total_trades", 0),
            "total_wins": state.get("total_wins", 0),
            "win_rate": round(state.get("total_wins", 0) / state["total_trades"], 4) if state.get("total_trades", 0) > 0 else 0,
            "daily_loss_today": round(state.get("daily_loss_today", 0), 2),
            "halted_until": state.get("halted_until"),
            "circuits": {
                "max_drawdown": 20,
                "max_daily_loss": 50,
                "max_positions": 3,
                "max_leverage": 2,
            }
        }), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.route("/api/shadow/positions")
def api_shadow_positions():
    from workers.execution.shadow_exec import _load_state
    try:
        state = _load_state()
        return jsonify({"positions": state.get("open_positions", []), "count": len(state.get("open_positions", []))}), 200
    except Exception as exc:
        return jsonify({"error": str(exc), "positions": [], "count": 0}), 500

# ---- Live Execution (Real positions + auto-order) -------------------------

@rate_limit
@app.route("/api/live/positions")
def api_live_positions():
    from workers.execution.hl_executor import sync_positions
    try:
        result = sync_positions()
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"error": str(exc), "open_count": 0, "positions": []}), 500

@rate_limit
@app.route("/api/live/monitor")
def api_live_monitor():
    from workers.execution.hl_executor import _load_exec_state, _estimate_bankroll, _check_drawdown
    try:
        state = _load_exec_state()
        br = _estimate_bankroll(state)
        ok, dd_msg = _check_drawdown(state)
        return jsonify({
            "live_mode": os.environ.get("LIVE_MODE", "").lower() == "true",
            "bankroll_start": 1021,
            "estimated_bankroll": round(br, 2),
            "peak_bankroll": round(state.get("peak_bankroll", 1021), 2),
            "daily_loss": round(state.get("daily_loss", 0), 2),
            "daily_wins": round(state.get("daily_wins", 0), 2),
            "halted": state.get("halted", False),
            "halt_reason": state.get("halt_reason"),
            "drawdown_msg": dd_msg,
            "positions_open": state.get("positions_open", 0),
            "total_trades": state.get("total_trades", 0),
            "total_wins": state.get("total_wins", 0),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@rate_limit
@app.route("/api/live/execute", methods=["POST"])
def api_live_execute():
    """Manual override: submit a single signal for immediate execution."""
    from workers.execution.hl_executor import place_order
    try:
        payload = request.get_json(force=True) or {}
        required = ["coin", "direction", "entry_px", "sl_px", "tp_px"]
        missing = [f for f in required if f not in payload]
        if missing:
            return jsonify({"error": f"Missing fields: {missing}"}), 400
        res = place_order(payload)
        return jsonify(res), 200 if res.get("status") == "filled" else 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# ---- Signal Feed (for external signing bot) -----------------------------

@rate_limit
@app.route("/api/live/signal-feed")
def api_live_signal_feed():
    """Return actionable signals for an external signing bot.
    Query params:
      since — ISO timestamp; only return signals newer than this
    """
    since = request.args.get("since", "")
    
    signals = []
    
    # Vol squeeze
    vs_path = PROJECT_ROOT / "data_store" / "vol_squeeze_signals.json"
    if vs_path.exists():
        try:
            vs = json.loads(vs_path.read_text())
            for s in vs:
                s["source"] = "vol_squeeze"
                signals.append(s)
        except Exception:
            pass
    
    # Momentum fade
    mf_path = PROJECT_ROOT / "data_store" / "momentum_fade_signals.json"
    if mf_path.exists():
        try:
            mf = json.loads(mf_path.read_text())
            for s in mf:
                s["source"] = "momentum_fade"
                signals.append(s)
        except Exception:
            pass
    
    # Quick scalp
    qs_path = PROJECT_ROOT / "data_store" / "quick_scalp_signals.json"
    if qs_path.exists():
        try:
            qs = json.loads(qs_path.read_text())
            for s in qs:
                if s.get("status") == "OPEN":
                    s["source"] = "quick_scalp"
                    signals.append(s)
        except Exception:
            pass
    
    # Filter by "since" if provided
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            filtered = []
            for s in signals:
                dt_str = s.get("dt", s.get("opened_at", s.get("timestamp", "")))
                if dt_str:
                    try:
                        sig_dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                        if sig_dt >= since_dt:
                            filtered.append(s)
                    except Exception:
                        filtered.append(s)
                else:
                    filtered.append(s)
            signals = filtered
        except Exception:
            pass
    
    # Sort by recency
    signals.sort(key=lambda s: s.get("dt", s.get("opened_at", "")), reverse=True)
    
    # Safety / bankroll snapshot
    from workers.execution.hl_executor import _load_exec_state, _estimate_bankroll, _check_drawdown
    state = _load_exec_state()
    br = _estimate_bankroll(state)
    ok, dd_msg = _check_drawdown(state)
    
    return jsonify({
        "signals": signals[:50],
        "count": len(signals),
        "bankroll": round(br, 2),
        "peak_bankroll": round(state.get("peak_bankroll", 1021), 2),
        "drawdown_msg": dd_msg,
        "halted": state.get("halted", False),
        "halt_reason": state.get("halt_reason"),
        "live_mode": os.environ.get("LIVE_MODE", "").lower() == "true",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
