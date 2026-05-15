# TradingView IG Markets Bot

FastAPI service that receives TradingView webhook alerts, validates trading rules, applies daily risk limits, and sends market orders to the IG Markets REST API.

The default configuration is demo mode. Use `TRADING_MODE=paper` to simulate orders locally without calling IG. Live IG orders are blocked unless both `TRADING_MODE=live` and `IG_ACCOUNT_TYPE=live` are configured.

## Setup

```bash
cd /Users/santics/tradingview-ig-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with your IG credentials and risk settings.

Run the API:

```bash
uvicorn app.main:app --reload
```

Open the paper dashboard:

```text
http://127.0.0.1:8000/dashboard
```

## TradingView Webhook

Send alerts to:

```text
POST http://your-server:8000/webhook
```

For a VM/paper test before connecting IG, set this in `.env`:

```env
TRADING_MODE=paper
```

In paper mode, accepted signals are shown in `/dashboard` and `/paper/trades`; no order is sent to IG.

## Railway Deploy

1. Create a GitHub repository and upload this project.
2. In Railway, choose `New Project` -> `Deploy from GitHub repo`.
3. Select the repository.
4. Open the Railway service settings and add these variables:

```env
TRADING_MODE=paper
IG_ACCOUNT_TYPE=demo
WEBHOOK_SECRET=choose-a-long-random-secret
MAX_TRADES_PER_DAY=3
MAX_DAILY_LOSS=500.0
RISK_PER_TRADE=0.01
ACCOUNT_EQUITY=10000.0
DEFAULT_STOP_DISTANCE=20.0
DEFAULT_TAKE_PROFIT_DISTANCE=40.0
DEFAULT_DEAL_SIZE=1.0
```

5. Railway will run:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

6. Open your public Railway URL:

```text
https://your-app.up.railway.app/dashboard
```

7. Use this TradingView webhook URL:

```text
https://your-app.up.railway.app/webhook
```

If `WEBHOOK_SECRET` is set, every TradingView alert must include the same value as `secret`.

Example alert body:

```json
{
  "symbol": "IX.D.SPTRD.DAILY.IP",
  "side": "short",
  "price": 5230.5,
  "bias": "BAJISTA",
  "state": "TENDENCIA",
  "long_score": 2,
  "short_score": 8,
  "dist_vwap_atr": 2.5,
  "timeframe": "15m",
  "secret": "optional-shared-secret"
}
```

## Signal Rules

The bot only allows a trade when all filters pass:

- Short: `side` is `short`, `bias` contains `BAJISTA`, and `short_score >= 7`.
- Long: `side` is `long`, `bias` contains `ALCISTA`, and `long_score >= 7`.
- `state` must not contain `RANGO`.
- `state` must not contain `NO CHASE`.
- `dist_vwap_atr <= 4.0`.

## Risk Management

The in-memory risk manager enforces:

- Maximum one open trade per symbol.
- Maximum three trades per day by default.
- Maximum daily realized loss from configured `MAX_DAILY_LOSS`.
- Configurable `RISK_PER_TRADE`.

Position size is calculated from `ACCOUNT_EQUITY * RISK_PER_TRADE / DEFAULT_STOP_DISTANCE` and capped by `DEFAULT_DEAL_SIZE`.

Use this endpoint to mark a trade closed and update daily PnL:

```text
POST /risk/close
```

```json
{
  "symbol": "IX.D.SPTRD.DAILY.IP",
  "realized_pnl": -120.5
}
```

## Logs

Logs are written to `logs/`:

- `app.log` for runtime logs.
- `alerts.jsonl` for every received alert with webhook secrets redacted.
- `decisions.jsonl` for every allow/reject/order decision.

## Safety Notes

- Keep `.env` private and never commit it.
- Use IG demo credentials while testing.
- `symbol` is sent to IG as the order `epic`; use the IG epic, not a generic ticker.
- The daily risk state is in memory. Restarting the process resets open-trade and daily-trade counters unless you persist or reconstruct them from IG.
