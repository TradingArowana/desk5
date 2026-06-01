# Desk5 Local Signing Bot

**Never stores or sends your private key to the server.**

This bot polls the Desk5 signal feed from your server, validates signals against local safety rules, and **signs + submits orders directly to Hyperliquid** from your local machine.

## Architecture

```
┌─────────────────┐         HTTP (no auth)         ┌──────────────────┐
│  Desk5 Server   │  ◄─── /api/live/signal-feed ───│  Your Laptop     │
│  (signals only) │                                │  (private key    │
└─────────────────┘                                │   NEVER leaves)  │
                                                   └────────┬─────────┘
                                                            │
                                                            │ signed tx
                                                            ▼
                                                   ┌──────────────────┐
                                                   │  Hyperliquid API │
                                                   └──────────────────┘
```

## Setup

### 1. Create a fresh wallet (if you haven't already)

**Option A: Hyperliquid native**
1. Go to https://app.hyperliquid.xyz/
2. Click Connect → create a new wallet
3. Save the **private key** (hex string, 64 chars) offline on paper
4. Copy the **public address** (0x...)

**Option B: Use existing EVM wallet**
- Any Ethereum private key works. Hyperliquid derives the same address.

### 2. Install

```bash
cd signing_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

Create `.env` in `signing_bot/`:

```bash
# Required
HL_API_KEY=0xYOUR_PRIVATE_KEY_HERE
DESK5_SERVER_URL=http://your-server-ip:8080

# Optional safety overrides
MAX_POSITIONS=3
RISK_PER_TRADE_PCT=0.015
MAX_DAILY_LOSS_USD=50
MAX_LEVERAGE=2
AUTO_EXECUTE=true
```

**IMPORTANT:** Never commit `.env`. It is already in `.gitignore`.

### 4. Fund the wallet

1. Deposit USDC to your Hyperliquid address
2. The bot only trades perps — no spot required

### 5. Run

```bash
python bot.py
```

Or with auto-restart:

```bash
while true; do python bot.py; sleep 5; done
```

## How It Works

1. **Polls** `/api/live/signal-feed?since=...` every 5 minutes
2. **Validates** signals against local safety circuits (drawdown, daily loss, max positions)
3. **Checks** if signal was already acted on (deduplication via local state file)
4. **Computes** position size from risk parameters
5. **Signs** order locally using your private key
6. **Submits** directly to `api.hyperliquid.xyz` (server never sees the tx)
7. **Logs** all activity to local `bot.log` and `state.json`

## Safety Circuits (enforced locally)

| Circuit | Default | Description |
|---------|---------|-------------|
| Max Drawdown | 20% | Halt if bankroll drops 20% from peak |
| Daily Loss Cap | $50 | Halt if daily losses exceed $50 |
| Max Positions | 3 | No more than 3 open positions |
| Max Leverage | 2× | Hardcoded per order |
| Risk Per Trade | 1.5% | Position size calculated from SL distance |
| Duplicate Block | 24h | Won't act on same coin+direction within 24h |

## Files

- `bot.py` — Main loop
- `config.py` — Settings loader
- `hyperliquid_signer.py` — Order signing + submission
- `state.py` — Local state tracking (positions, daily PnL, dedup)
- `safety.py` — Circuit breaker logic
- `requirements.txt` — Dependencies
- `.env.example` — Configuration template

## Security Model

| Secret | Location | Server Access? |
|--------|----------|----------------|
| Private key | Your laptop `.env` | **NO** |
| Signal data | Desk5 server | Yes (public endpoint) |
| Signed transactions | Your laptop → HL API | **NO** |

The server **cannot** trade on your behalf. It only suggests signals. Your bot decides whether to act.

## Troubleshooting

**Bot says "Halted: Daily loss $50.00 >= cap $50"**
→ Normal safety behavior. Resets at UTC midnight. Check `state.json` for details.

**"No signals available"**
→ Server scanner may not have found setups. Wait for next cycle (5 min) or check `/api/live/signal-feed` in browser.

**"Order rejected: insufficient margin"**
→ Deposit more USDC to Hyperliquid or reduce `RISK_PER_TRADE_PCT`.

**"Connection refused"**
→ Check `DESK5_SERVER_URL` points to the correct IP/port. Ensure server firewall allows port 8080.
