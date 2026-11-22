# forex-bot

A trading bot for **gold (XAU/USD) and currency pairs**. It runs three strategies (EMA/RSI
trend following, grid trading and DCA), backtests them on real history, paper trades them
with live prices, and trades live on **OANDA** once you switch that on.

The same strategy code runs in all three modes, so what you backtest is what trades.

> **Risk warning.** Leveraged forex and gold trading can lose more than you expect, fast.
> This software comes with no guarantee of profit. Backtest, then paper trade for weeks,
> then start live on an OANDA *practice* account before you risk real money.

## Contents

- [Quick start](#quick-start)
- [Commands](#commands)
- [Strategies](#strategies)
- [Configuration](#configuration)
- [Data sources](#data-sources)
- [Going live on OANDA](#going-live-on-oanda)
- [Telegram notifications](#telegram-notifications)
- [Running 24/7](#running-247)
- [How the simulation works](#how-the-simulation-works)
- [What the backtests showed](#what-the-backtests-showed)
- [Development](#development)

## Quick start

Needs Python 3.10 or newer.

```bash
git clone https://github.com/lycas1017/lycas-crypto.git
cd lycas-crypto
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install .

forex-bot init        # creates config.yaml and .env in this folder
forex-bot demo        # offline smoke test on synthetic prices, no account needed
forex-bot backtest    # one year of real history from Yahoo Finance (free, no account)
forex-bot paper       # paper trading with live prices and a simulated 10,000 USD account
```

Each backtest writes `reports/backtest-<time>/report.html` (open it in a browser) plus
`trades.csv`, `equity.csv` and `summary.json`.

## Commands

| Command | What it does |
| --- | --- |
| `forex-bot init [DIR]` | Create `config.yaml` and `.env` from the templates |
| `forex-bot demo` | Backtest every strategy on synthetic data (offline) |
| `forex-bot backtest` | Backtest the enabled bots. `--bot ID` (repeatable), `--start 2025-01-01`, `--end`, `--source yahoo\|oanda\|csv\|synthetic`, `--balance 5000` |
| `forex-bot fetch XAU_USD H1` | Save candles to `data/XAU_USD_H1.csv` for offline backtests (`--source`, `--start`, `--end`) |
| `forex-bot paper` | Paper trade with live prices. `--reset` starts a fresh account, `--once` runs one cycle |
| `forex-bot live` | Trade on OANDA. Refuses to start unless `LIVE_TRADING=true` |
| `forex-bot check` | Validate the config and test data, OANDA and Telegram (`--telegram` sends a test message) |
| `forex-bot status` | Show the saved paper (or `--mode live`) state: balance, open trades, risk switches |
| `forex-bot reset-risk` | Clear the drawdown kill switch and daily halt after you have reviewed what happened |

Add `-c path/to/config.yaml` to use another config and `-v` for debug logging.

## Strategies

Every bot runs one strategy on one instrument and timeframe. You can run any mix.

### `ema_rsi`: trend following

Buys when the fast EMA crosses above the slow EMA, RSI is not overbought and price is above
the long-term trend EMA. Sells short on the mirror image. Stop loss and take profit are ATR
multiples, and an opposite crossover closes the trade early.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `fast`, `slow` | 20, 50 | EMA periods for the crossover |
| `trend` | 200 | Trend filter EMA; 0 turns it off |
| `rsi_period` | 14 | RSI period |
| `rsi_long_max`, `rsi_short_min` | 70, 30 | No longs above / shorts below these RSI values |
| `atr_period` | 14 | ATR period |
| `sl_atr`, `tp_atr` | 2.0, 3.0 | Stop loss and take profit in ATRs (tp 0 = no target) |
| `allow_long`, `allow_short` | true, true | Directions allowed |
| `exit_on_cross` | true | Close on the opposite crossover |

### `grid`: range trading

Splits a price range into levels. In `long` mode it buys each time price falls through a
level and takes profit one level higher; `short` is the reverse; `neutral` buys below the
middle and sells above it. Every trade's stop sits `stop_pct` beyond the grid edge.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `mode` | long | `long`, `short` or `neutral` (neutral needs a hedging account) |
| `levels` | 10 | Number of steps in the range |
| `lower`, `upper` | none | Fixed range. Leave both out for an automatic range |
| `range_pct` | 2.0 | Automatic range: this many percent either side of the price |
| `stop_pct` | 1.0 | Stop loss this many percent beyond the grid edge |
| `recenter` | true | Automatic range only: rebuild the grid around price after a breakout |

Grids earn in sideways markets and lose in strong trends; the stop caps that loss.

### `dca`: dollar-cost averaging in cycles

Opens a base order, adds one every `interval_candles`, adds a larger "safety" order each time
price moves `safety_step_pct` against the last entry, and closes the whole cycle at
`take_profit_pct` above the average entry (or at `stop_loss_pct` below it).

| Parameter | Default | Meaning |
| --- | --- | --- |
| `direction` | long | `long` or `short` |
| `interval_candles` | 24 | Candles between scheduled orders |
| `take_profit_pct` | 1.5 | Close the cycle this far beyond the average entry |
| `stop_loss_pct` | 0 | Close the cycle this far against the average entry (0 = never) |
| `safety_orders` | 3 | Extra orders on the way down |
| `safety_step_pct` | 1.0 | Distance from the last entry that triggers a safety order |
| `safety_scale` | 1.5 | Each safety order is this many times the previous size |
| `max_buys` | 10 | Most orders open at once in one cycle |

DCA exits are checked at candle closes and have no per-trade stop, so DCA bots must use
`notional` or `units` sizing.

## Configuration

`config.yaml` holds settings, `.env` holds secrets. `forex-bot init` writes both with
comments; `forex-bot check` validates them and reports every problem at once.

**Instruments** use OANDA names: `XAU_USD` (gold), `XAG_USD` (silver), `EUR_USD`, `GBP_USD`,
`USD_JPY`, `AUD_USD`, `USD_CHF`, `USD_CAD`, `NZD_USD`, `EUR_GBP`, `EUR_JPY`, `GBP_JPY`
and others. **Timeframes:** `M1 M5 M15 M30 H1 H4 D`.

**Sizing** per bot:

| Mode | `value` means | Example |
| --- | --- | --- |
| `risk` | Percent of equity lost if the stop loss is hit (max 5) | `{mode: risk, value: 1}` |
| `notional` | Trade size in account currency | `{mode: notional, value: 5000}` |
| `units` | Units; for gold one unit is one troy ounce | `{mode: units, value: 1}` |

OANDA's smallest gold trade is 1 ounce, worth several thousand USD, so size gold bots for
your balance. A `risk` size that rounds below the minimum is skipped and logged.

**Risk rules** (whole account):

| Setting | Default | Effect |
| --- | --- | --- |
| `max_open_trades` | 25 | Across all bots |
| `daily_loss_limit_pct` | 4 | No new trades for the rest of the UTC day once equity is this far below the day's start |
| `max_drawdown_pct` | 20 | Kill switch: no new trades once equity is this far below its peak; with `close_all_on_max_drawdown` every bot trade is closed. Clear it with `forex-bot reset-risk` |
| `margin_buffer_pct` | 20 | Always keep this share of equity free as margin |
| `max_spread_pips` | gold 80, others 4 | Skip entries while the spread is wider |

Each bot may also set `max_trades` and `enabled: false`.

## Data sources

Set `data.source` in `config.yaml` or pass `--source`:

| Source | Account | Use it for |
| --- | --- | --- |
| `auto` (default) | | OANDA when `OANDA_API_TOKEN` is set, otherwise Yahoo |
| `yahoo` | none | Backtests and paper trading. Intraday history is limited (about 60 days of M5 to M30, 2 years of H1). Gold uses COMEX futures `GC=F`, which track spot closely but not exactly. Mid prices only, so spreads are modelled |
| `oanda` | free practice account | Best data: real bid/ask, years of intraday history, same prices you trade |
| `csv` | none | Offline backtests from `data/<INSTRUMENT>_<TF>.csv` (make them with `forex-bot fetch`) |
| `synthetic` | none | Reproducible made-up prices for testing |

## Going live on OANDA

1. Open an account at [oanda.com](https://www.oanda.com). Start with a **practice (demo)**
   account; it is free and uses demo money.
2. In the account portal open *Manage API Access* and generate a token.
3. Put the token and account id (looks like `101-004-1234567-001`) in `.env`:
   ```
   OANDA_API_TOKEN=...
   OANDA_ACCOUNT_ID=101-004-1234567-001
   OANDA_ENVIRONMENT=practice
   ```
4. Run `forex-bot check`. It shows the balance and whether the account allows hedging.
5. Set `LIVE_TRADING=true` in `.env` and run `forex-bot live`. On a practice account this
   places demo orders.
6. Only after weeks of good results, switch to `OANDA_ENVIRONMENT=live` with a real-money
   account token. The bot then asks you to type `YES` (or pass `--yes` when it runs as a
   service).

How live trading behaves:

- Stop loss and take profit are attached to each order on OANDA's servers, so they protect
  trades even while the bot is stopped.
- Each trade is tagged with its bot id. Bots only manage their own trades and never touch
  trades you open by hand.
- Accounts without hedging (all US accounts and many others) net opposite trades on the same
  instrument. There, give each bot its own instrument; the bot warns at start if two bots
  share one. US accounts also follow FIFO rules that can reject closing a newer trade first.
- `account_currency` must match the OANDA account currency.

## Telegram notifications

Optional. You get a message for every trade opened or closed, risk events, repeated errors
and a daily summary.

1. Talk to [@BotFather](https://t.me/BotFather), create a bot, copy its token.
2. Send your new bot any message, then get your chat id from
   [@userinfobot](https://t.me/userinfobot).
3. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to `.env`, then run
   `forex-bot check --telegram`.

## Running 24/7

**Docker** (recommended on a VPS):

```bash
forex-bot init .            # or copy config.yaml and .env into this folder
docker compose up -d --build
docker compose logs -f
```

The container runs `paper` by default; change `command` in `docker-compose.yml` to
`["live", "--yes"]` for live trading. State, logs and reports are written to the mounted
folder, so restarts continue where they left off.

**systemd** (Linux, without Docker), `/etc/systemd/system/forex-bot.service`:

```ini
[Unit]
Description=forex-bot
After=network-online.target

[Service]
WorkingDirectory=/home/you/lycas-crypto
ExecStart=/home/you/lycas-crypto/.venv/bin/forex-bot paper
Restart=always
User=you

[Install]
WantedBy=multi-user.target
```

Then `sudo systemctl enable --now forex-bot` and `journalctl -u forex-bot -f`.

The bot saves its state (strategy memory, risk counters, the paper account) after every
cycle to `state/forex_bot_state.paper.json` or `.live.json`. Paper and live never share a
state file. Logs rotate in `logs/`.

## How the simulation works

- All bots share one account, as they would live.
- A decision made when a candle closes is filled at the **next candle's open**; no
  strategy ever sees a price before it could have.
- Buys fill at the ask, sells at the bid. With mid-price data the spread is modelled from
  `paper.spread_pips` (gold 35 pips = 0.35 USD per ounce by default).
- Stops and targets are checked against each candle's high and low. If one candle touches
  both, the stop is assumed to hit first. A gap through a stop fills at the open price.
- Overnight financing (swap) is not simulated; it matters for trades held for weeks.
- Commission (`paper.commission_per_trade`) and slippage (`paper.slippage_pips`) are optional.

## What the backtests showed

The default `config.yaml` was backtested on 22 November 2026 on real Yahoo Finance data
(November 2025 to November 2026, 10,000 USD account). Results change as new data arrives;
these are a snapshot, not a forecast:

| Bot | Trades | Net P/L (USD) |
| --- | --- | --- |
| gold-dca | 45 | +1,307 |
| gold-trend | 12 | -152 |
| gbpusd-trend | 23 | -166 |
| eurusd-grid | 0 in the combined run, 66 alone (53 days of M15 data) | -198 alone |
| **Account** | 80 | **+989 (+9.9%), max drawdown 20.6%** |

- The gold DCA bot earned in the gold rally, then lost three cycles in a row in March 2026
  when gold fell about 21% in three weeks.
- On 23 March 2026 the 20% drawdown kill switch closed every trade and blocked new ones,
  which is why the grid (whose M15 data starts in September) made no trades in the combined
  run. Backtest a bot on its own with `--bot ID` to judge it separately.
- One year is a small sample. None of these numbers shows a dependable edge.

Treat the default settings as a starting point to test and tune, not as a recommendation,
and beware of tuning until the past looks perfect.

## Development

```bash
pip install -e ".[dev]"
pytest          # unit and integration tests, no network or credentials needed
ruff check src tests
```

The layout under `src/forex_bot/`:

| Module | Role |
| --- | --- |
| `strategies/` | `ema_rsi`, `grid`, `dca`. Each turns candles into open/close intents |
| `execution.py` | Sizes intents and checks them against the risk rules |
| `risk.py` | Position sizing, daily loss limit, drawdown kill switch |
| `brokers/` | `PaperBroker` (simulation) and `OandaBroker` (live REST API) |
| `feeds/` | OANDA, Yahoo Finance, CSV and synthetic data |
| `backtest.py`, `report.py`, `metrics.py` | Backtesting and reports |
| `engine.py` | The paper/live loop with state saving and notifications |
| `cli.py` | The `forex-bot` command |

To add a strategy, subclass `Strategy` in `strategies/`, return `OpenIntent` /
`CloseIntent` objects from `on_candle`, and register it in `strategies/__init__.py`.
