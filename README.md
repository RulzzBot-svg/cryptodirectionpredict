# BTC 15-Minute Prediction Edge Bot

Estimates the probability that Bitcoin finishes **above** or **below** the
current 15-minute strike (Robinhood / Kalshi-style windows) and recommends
whether to buy that side.

## What it does

Every ~10 seconds the bot:

1. Locks (or refreshes) the current wall-clock **15m window** and **strike**
2. Estimates realized volatility from recent candles
3. Computes **P(finish ABOVE strike)** with a driftless lognormal model
4. Compares that probability to a reference market price (default 50¢)
5. Recommends **ABOVE**, **BELOW**, or **SKIP**
6. Optionally papers a position sized by `STAKE_NOTIONAL` when edge ≥ `MIN_EDGE`

Live status line example:

```text
BTC $65,914.06 | Strike $65,900.00 | T-08:42 | Above 57.20% | Below 42.80% | Mkt 53.0¢ | Edge +4.2¢ | SKIP  | Bank $100.00
```

## Project Structure

```
.
├── config/              # Settings / env loading
├── data/                # CCXT market data + Kalshi public feed
├── prediction/          # Windowing, probability, advisor
├── models/              # SQLAlchemy schemas
├── strategies/          # Legacy EMA spot strategy (optional)
├── execution/           # Paper prediction book (+ legacy spot broker)
├── main.py              # Async entrypoint
├── .env.example
└── requirements.txt
```

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python main.py --reset-paper       # wipe W/L and start bank at $100
python main.py
```

### Paste Robinhood strike (optional now)

By default the bot **auto-pulls the current Kalshi window** by event ticker
(e.g. `KXBTC15M-26JUL231400` — the suffix is the window end time in ET), then
reads `floor_strike` + YES/NO asks. This matches the Robinhood BTC 15m contracts.

Status shows `(KL)` plus the event ticker. Stale `manual_strike.txt` files are
**ignored** while Kalshi auto mode is on. Empty/0¢ books are skipped (no fake 1¢ fills).

```bash
python main.py --strike 64737.27 --market-cents 55
```

Or while running:

```bash
echo 64737.27 > manual_strike.txt
echo 55 > market_cents.txt
python main.py --no-kalshi
```

Status line shows `(KL)` for Kalshi or `(RH)` for manual override.

Ctrl+C prints settlement / bankroll performance and closes the exchange client.

Terminal output (status ticks, bets, settlements) is also appended to
`logs/bot.log` (rotates when large). Bets themselves still live in
`paper_trading.db`.

### Useful env vars

| Variable | Default | Meaning |
|----------|---------|---------|
| `SYMBOL` | `BTC/USD` | CCXT symbol |
| `DATA_PROVIDER` | `coinbase` | `coinbase` / `binance` |
| `PAPER_INITIAL_BALANCE` | `100` | Starting paper bankroll ($) |
| `MIN_EDGE` | `0.08` | Minimum edge vs ask before betting (8¢) |
| `MARKET_PROB_ABOVE` | `0.50` | Fallback YES ask if Kalshi quotes missing |
| `STAKE_NOTIONAL` | `5` | Face value per bet (5 contracts ⇒ pay `5 × share_price`) |
| `CONTRACT_COST` | `0.50` | Legacy; ignored when using notional stake sizing |
| `AUTO_BET` | `true` | Place paper bets automatically |
| `LOOP_INTERVAL_SECONDS` | `10` | Poll cadence |
| `LOG_DIR` / `LOG_FILE` | `logs` / `bot.log` | File that mirrors terminal output |

> **Note:** Cursor Cloud / some VPS regions get HTTP 451 from Binance. Prefer Coinbase (`BTC/USD`) or Kraken there.

## Probability model

For spot `S`, strike `K`, seconds remaining `τ`, and σ estimated from recent
log-returns:

\[
P(S_T > K) = N(d_2), \quad d_2 = \frac{\ln(S/K) - \tfrac{1}{2}\sigma^2\tau}{\sigma\sqrt{\tau}}
\]

Fair YES ≈ `prob_above * 100¢`, fair NO ≈ `prob_below * 100¢`.

## Paper contracts

`execution/prediction_book.py` mirrors Robinhood/Kalshi share math.

Example: YES **34¢** / NO **66¢**, face **$20** (20 contracts):

| Side | You pay now | If correct | If wrong |
|------|-------------|------------|----------|
| YES/ABOVE | `$6.80` | receive `$20` total (= `$6.80` stake back + `$13.20` profit) | lose `$6.80` stake |
| NO/BELOW | `$13.20` | receive `$20` total (= `$13.20` stake back + `$6.80` profit) | lose `$13.20` stake |

Default sizing is `STAKE_NOTIONAL=5` (same math at $5 face).

Reset paper W/L anytime:

```bash
python main.py --reset-paper
# or RESET_PAPER_HISTORY=true in .env for one run
```

## Legacy spot paper trader

The original EMA crossover + spot `PaperBroker` modules remain under
`strategies/` and `execution/paper_engine.py` if you want directional BTC
inventory simulation. The default `main.py` path is the prediction-market loop.
