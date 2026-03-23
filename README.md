# Trading 212 swing-signal bot

Signals-only Python bot for a small Trading 212 account. It scans a watchlist once per run, looks for a simple swing-breakout setup, and sends watch / buy / exit alerts to Telegram or Discord.

## What it does

- scans a liquid watchlist on daily candles
- filters for uptrend, ATR range, and dollar-volume liquidity
- sends **WATCH** alerts when a stock is close to a breakout
- sends **BUY SETUP** alerts on confirmed breakouts with volume
- sends **EXIT** alerts for stop, target, fast-MA failure, or time exit
- suppresses duplicate alerts with a local `state.json`
- can optionally read your **open positions** from Trading 212 using the official Public API

## Strategy logic

A stock can trigger only if:

- `close > SMA20 > SMA50`
- ATR% is inside your configured range
- average dollar volume is above your configured minimum

Then:

- **WATCH** if price is within `near_breakout_pct` of the prior 10-day high
- **BUY SETUP** if price closes above the prior 10-day high on stronger-than-average volume
- **EXIT** if price hits stop, target, closes below the fast MA, or times out

This is intentionally simple and meant for **learning and paper-to-live testing**, not as a promise of profit.

## Setup

```bash
cd trading212_swing_bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp config.example.json config.json
cp positions.example.json positions.json
```

Edit:

- `.env`
- `config.json`
- `positions.json` if you are **not** pulling positions from Trading 212

## Run once

```bash
python bot.py --config config.json --positions positions.json --state state.json
```

## Telegram bot setup

1. Create a bot with BotFather.
2. Put the token into `TELEGRAM_BOT_TOKEN`.
3. Put your chat ID into `TELEGRAM_CHAT_ID`.

## Discord setup

Create a channel webhook and put it into `DISCORD_WEBHOOK_URL`.

## Optional Trading 212 read-only integration

If you want the bot to read open positions from Trading 212 instead of `positions.json`, set these in `.env`:

```env
T212_ENV=demo
T212_API_KEY=...
T212_API_SECRET=...
```

Start with a key that only has **read** permissions.

## Example cron schedule

Run once after the U.S. cash session closes:

```cron
15 22 * * 1-5 cd /path/to/trading212_swing_bot && /path/to/.venv/bin/python bot.py --config config.json --positions positions.json --state state.json >> bot.log 2>&1
```

Adjust the time for your timezone and daylight saving changes.

## Recommended first tweaks

- keep only 8-15 liquid stocks in the watchlist
- use one position at a time with a tiny account
- start on demo / paper mode
- review every signal manually before trading live
- keep API permissions read-only until you trust the alerts

## File notes

- `config.json` controls the scanner thresholds
- `positions.json` is only needed if you are not reading positions from Trading 212
- `state.json` is created automatically to avoid duplicate messages

## Safe next steps

After you test this, the next upgrade should be **better journaling and backtesting**, not auto-execution.
