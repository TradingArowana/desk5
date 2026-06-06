#!/bin/bash
# Desk5 dashboard launcher
# Requires environment variables set BEFORE running:
#   export DESK_SOLANA_ADDRESS="your_sol_address_here"
#   export DESK_FERNET_KEY="your-fernet-key-here"
set -e

if [ -z "$DESK_SOLANA_ADDRESS" ]; then
    echo "NOTICE: DESK_SOLANA_ADDRESS not set. Solana deposit panel will show setup instructions." >&2
fi

if [ -z "$DESK_FERNET_KEY" ]; then
    echo "WARNING: DESK_FERNET_KEY not set — using auto-generated key (restart will break encrypted HL creds)." >&2
fi

source ./.venv/bin/activate
export ETHERSCAN_API_KEY="${ETHERSCAN_API_KEY:-}"
# Load desk5 .env into gunicorn env
set -a
source ./.env
set +a
export PORT=${PORT:-8080}
export PYTHONPATH=.

exec ./.venv/bin/gunicorn -w 1 -b 0.0.0.0:$PORT --timeout 60 --log-level info app:app
