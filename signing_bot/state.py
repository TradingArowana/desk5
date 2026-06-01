"""Local state tracking for signing bot."""
import json, os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List

from config import STATE_FILE


def load_state() -> dict:
    if Path(STATE_FILE).exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "today": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "daily_loss": 0.0,
        "daily_wins": 0.0,
        "peak_bankroll": 0.0,
        "halted": False,
        "halt_reason": None,
        "acted": {},  # coin_direction -> iso_timestamp
        "open_positions": [],
    }


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def reset_daily(state: dict):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("today") != today:
        state["today"] = today
        state["daily_loss"] = 0.0
        state["daily_wins"] = 0.0
        state["halted"] = False
        state["halt_reason"] = None
        save_state(state)


def already_acted(state: dict, coin: str, direction: str, hours: int = 24) -> bool:
    key = f"{coin}_{direction}"
    ts = state.get("acted", {}).get(key)
    if not ts:
        return False
    try:
        last = datetime.fromisoformat(ts)
        ago = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        return ago < hours
    except Exception:
        return False


def record_act(state: dict, coin: str, direction: str):
    key = f"{coin}_{direction}"
    state["acted"][key] = datetime.now(timezone.utc).isoformat()
    save_state(state)


def add_position(state: dict, pos: dict):
    state["open_positions"].append(pos)
    save_state(state)


def close_position(state: dict, coin: str, pnl: float):
    for p in state["open_positions"]:
        if p.get("coin") == coin and p.get("status") == "OPEN":
            p["status"] = "CLOSED"
            p["closed_at"] = datetime.now(timezone.utc).isoformat()
            p["pnl"] = pnl
            break
    if pnl > 0:
        state["daily_wins"] = state.get("daily_wins", 0) + pnl
    else:
        state["daily_loss"] = state.get("daily_loss", 0) + abs(pnl)
    save_state(state)
