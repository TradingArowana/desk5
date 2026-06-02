# Desk5 — Autonomous Perpetual Trading Desk for Hyperliquid

> ⚡ **Referral Code:** [`TRADEDESK5`](https://app.hyperliquid.xyz/join/TRADEDESK5) — Join Hyperliquid through this link to support development.

An autonomous multi-strategy trading desk built for Hyperliquid perpetual markets. Runs signal generators, execution engine, capital tracking, and a live dashboard — 24/7 via cron.

**Desk5 is designed to be paired with [Hermes Agent](https://github.com/NousResearch/hermes-agent)**, an autonomous AI agent framework that manages the desk end-to-end. Hermes handles setup, monitoring, strategy tuning, and risk management — so you don't have to babysit it.

---

## Prerequisites

Before you install Desk5, you need **Hermes Agent** — the AI orchestrator that runs the desk.

### 1. Install Hermes Agent

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
```

Or if you already have it installed, make sure it's up to date:
```bash
hermes update
```

### 2. Configure Hermes

Desk5 works well with any model that has tool calling. We personally run **Ollama with `kimi-k2.6`** (via ollama-cloud / OpenRouter) with excellent results — fast, cheap, great at code. Claude, GPT-4o, DeepSeek, and other local models also work. Pick whatever balances speed, cost, and reasoning for you.

```bash
hermes model
# Pick your provider and model interactively
# Tip: verify tool-calling works with your chosen model before going live.
```

### 3. Clone Desk5 via Hermes

Once Hermes is running, give it this command to install Desk5:

```
Clone the repository https://github.com/TradingArowana/desk5.git into ~/projects/desk5, set up the Python virtual environment, install requirements, copy .env.example to .env, and start the dashboard.
```

Hermes will handle the clone, venv, pip install, and initial config for you.

---

## Quick Start

### 1. Configure Environment

```bash
cp .env.example .env
nano .env
```

Fill in:
- `HL_API_KEY` — your wallet private key (hex, no `0x` prefix)
- `HL_ADDRESS` — your deposit address on Hyperliquid
- `DESK_SOLANA_ADDRESS` — your Solana public address (optional, for deposit QR)

### 2. Join Hyperliquid (Required)

**You MUST join through the referral link for setup to work:**
👉 **[https://app.hyperliquid.xyz/join/TRADEDESK5](https://app.hyperliquid.xyz/join/TRADEDESK5)**

Deposit USDC to your Hyperliquid address. The bot uses your full balance as "bankroll."

### 3. Verify Wallet Match

```bash
.venv/bin/python -c "from eth_account import Account; k='YOUR_KEY_HERE'; print(Account.from_key(k).address)"
```

**The printed address MUST match `HL_ADDRESS`.** If not, you're trading with the wrong wallet.

### 4. Start the Desk

```bash
./start_dashboard.sh
# Or manually:
.venv/bin/python workers/execution/exec_poller.py
```

---

## ⚠️ Unified Account Notice

**Hyperliquid uses a UNIFIED account for balances under $50,000.**

- Your SPOT USDC and PERP margin are the **same pool**.
- There is **no "transfer" step** between spot and perps.
- **Do NOT create subaccounts** for amounts under $50K — it causes "insufficient margin" errors.
- If you are absolutely sure you know better, open an issue and we will argue with you.

---

## Architecture

All price feeds come from **Hyperliquid's public `/info` API** — no CoinGecko API key required, no rate limits. Strategies read from a local cache that must be populated first.

| Component | Description | Schedule |
|---|---|---|
| `hl_candle_fetcher.py` | Native HL candle prefetch (→ cache) | Every 15 min |
| `hl_breakout.py` | Breakout signal generator | Every 5 min |
| `hl_vol_squeeze.py` | Volatility squeeze detector | Every 5 min (staggered) |
| `quick_scalp_v2.py` | Micro-range scalp signals | Every 5 min (staggered) |
| `exec_poller.py` | Unified execution engine | Every 1 min |
| `run_sync_positions.py` | SL/TP monitoring | Every 2 min |
| `tracker.py` | Capital tracking + milestones | Every 12 hours |
| `auto_learn.py` | Performance auto-tuning | Every 30 min |
| `app.py` | Live dashboard (gunicorn) | Manual / supervisor |

### Safety Circuits (Hardcoded)

| Limit | Value |
|---|---|
| Max drawdown halt | 20% from peak |
| Daily loss cap | 10% of **day-start** equity (fixed) |
| Profit lock | Throttled to 2 positions when net daily PnL > +5% |
| Max leverage | 5x |
| Risk per trade | 2% of day-start bankroll |
| Max concurrent positions | 5–12 (scales with bankroll) |

---

## Strategy Config

Edit inside each strategy file under `workers/strategies/`:

| Parameter | Typical Value | Description |
|---|---|---|
| `SL_PCT` | 0.02 | Stop-loss distance |
| `TP_PCT` | 0.04–0.05 | Take-profit distance |
| `VOL_MULT` | 1.0–1.5 | Volume filter multiplier |
| `LOOKBACK` | 8–12 candles | Lookback window |

---

## Running With Hermes

Desk5 is **built to be managed by Hermes Agent**. Once installed, Hermes reads the repo, understands the strategy files, monitors execution logs, and can tune parameters — all without you touching code.

### Recommended First Prompt (Copy-Paste This)

After you've completed Hermes setup including **Telegram gateway** (`hermes gateway setup` → connect your bot), **send this exact message to your Hermes bot** in Telegram:

> **Note:** To receive Telegram alerts and 6-hour reports, Telegram gateway must be connected first. Without it, Hermes has no way to reach you.

```
I just deposited $[AMOUNT] USDC to my Hyperliquid wallet. 

Your job: start desk5 trading with MINIMAL risk and MAXIMAL safety.

Specifically:
1. Read .env and verify the wallet address matches the private key.
2. Check my account value on Hyperliquid and confirm it matches my deposit.
3. Start the execution poller (exec_poller.py) with LIVE_MODE=true.
4. Set the position limit to 2 concurrent positions maximum for now.
5. Reduce risk per trade to 1% of bankroll instead of 2%.
6. Keep leverage at 3x maximum until we have 5 profitable trades in a row.
7. Monitor every trade. Send me a Telegram message on every position open, close, stop-loss hit, or take-profit hit.
8. If daily loss exceeds 5%, HALT all trading immediately and await my command.
9. Report back to me every 6 hours via Telegram with: bankroll, open positions, unrealized PnL, and today's realized PnL.
10. Do NOT trade highly volatile assets (meme coins, assets with < $100M market cap) until bankroll is $5,000+.

Begin now.
```

> **Replace `$[AMOUNT]` with your actual deposit.** The desk will treat this as your bankroll and size positions accordingly.

### What Happens Next

Hermes will:
- Verify your wallet → run the poller → monitor logs
- Auto-tune position sizing as your bankroll grows
- Halt on any safety trigger (drawdown, daily loss cap, API errors)
- Send you actionable alerts only — no spam
- Learn from each session and save tuning as skills

### Daily Hermes Check-In (Optional)

Every morning, send this to your Hermes bot in Telegram:

```
Good morning. Check desk5 status: bankroll vs peak, any open positions in danger, funding-rate opportunities, and what the capital tracker learned overnight. Recommend any parameter tweaks.
```

Hermes will give you a concise report and suggest changes if data supports it.

---

## Paper Trading & Backtesting

Before risking real money, you can backtest strategies on historical data and run paper trades.

### Backtest on Last 30 Days

Send this to your Hermes bot:

```
Backtest all strategies in workers/strategies/ against the last 30 days of Hyperliquid OHLC data. Use the native HL candle cache as price source. Report win rate, average R:R, max drawdown, and profit factor for each strategy. Recommend which ones are safe to run live.
```

Desk5 includes `workers/strategies/backtest_quick_scalp.py` as a reference — Hermes can adapt it for any strategy file you add.

### Paper Trade Mode

To run the desk with zero real risk:

1. Set `LIVE_MODE=false` in `.env`
2. Start the execution poller as normal

In paper mode, the desk logs every signal, entry, exit, and PnL to `data_store/paper_ledger.json`. No real trades are sent to Hyperliquid.

Send this to Hermes to switch modes:

```
Run desk5 in PAPER mode for the next 48 hours. Track every signal that would have been a live trade. After 48 hours, compare paper results to what real execution would have cost (including slippage and funding). Report whether the strategies are profitable enough to go live.
```

### Train Hermes to Optimize

You can ask Hermes to learn from paper/live results and suggest parameter tweaks:

```
Read the last 7 days of trade history from data_store/live_ledger.json or data_store/paper_ledger.json. Identify which strategies are underperforming. Suggest better SL_PCT, TP_PCT, or LOOKBACK values. Save your recommendations as a skill so future sessions remember the tuning.
```

Hermes will analyze the data and persist tuning rules as skills.

> **Reality check:** Backtests and paper trades are useful, but they don't include slippage, funding-rate drift, or emotionally-driven mistakes. **Only live funds can definitively prove if a strategy works.** Always start with small size.

---

## Support This Project

This repo is free and open-source. If it makes you money and you want to say thanks, here are some wallets:

| Chain | Address |
|---|---|
| **Solana (SPL)** | `CEXiRvfct7xZMQj9U9ztxSgK9SYNPiVYjf8V77S2S9dh` |
| **ETH / ERC-20** | `0x6726E34Af42A56F424F5084df5196a934Ee18616` |
| **BTC** | `bc1pmd379fwwx0z2gxz800hahyh53jjpndn7sck0kw537s8pgawtmz5q25f9zw` |

---

## License

MIT — use at your own risk. Trading is dangerous. This is not financial advice.
