#!/bin/bash
# Desk5 Public Edition — Dashboard Launcher
#
# SETUP: Copy .env.example → .env and fill in your values FIRST
# REFERRAL: Join Hyperliquid at https://app.hyperliquid.xyz/join/TRADEDESK5
#
# IMPORTANT: Under $50,000, Hyperliquid uses a UNIFIED account.
# Do NOT create subaccounts unless you know what you're doing.
# If you want to debate this, open an issue — we will argue with you.
#
set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="${PROJECT_ROOT}/.venv"

if [ ! -f "${PROJECT_ROOT}/.env" ]; then
    echo "ERROR: .env not found. Copy .env.example → .env and fill in your keys." >&2
    exit 1
fi

if [ -z "${DESK_FERNET_KEY:-}" ]; then
    echo "WARNING: DESK_FERNET_KEY not set — using auto-generated key (restart will break encrypted HL creds)." >&2
fi

source "${VENV}/bin/activate"
set -a
source "${PROJECT_ROOT}/.env"
set +a

export PORT="${PORT:-8080}"
export PYTHONPATH="${PROJECT_ROOT}"

exec "${VENV}/bin/gunicorn" -w 1 -b "0.0.0.0:${PORT}" --timeout 60 --log-level info app:app
