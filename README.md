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
BTC $65,914.06 | Strike $65,900.00 | T-08:42 | Above 57.20% | Below 42.80% | Edge +7.2¢ | SKIP  | Bank $10,000.00
```

## Project Structure

```
.
├── config/              # Settings / env loading
├── data/                # CCXT market data feed
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
python main.py
```

### Paste Robinhood strike (optional now)

By default the bot **auto-pulls strike + YES odds from Kalshi** (`KXBTC15M`),
which matches the Robinhood BTC 15m contracts.

Manual override still works if needed:

```bash
python main.py --strike 64737.27 --market-cents 55
```

Or while running:

```bash
echo 64737.27 > manual_strike.txt
echo 55 > market_cents.txt
```

Status line shows `(KL)` for Kalshi or `(RH)` for manual override.

Ctrl+C prints settlement / bankroll performance and closes the exchange client.

### Useful env vars

| Variable | Default | Meaning |
|----------|---------|---------|
| `SYMBOL` | `BTC/USD` | CCXT symbol |
| `DATA_PROVIDER` | `coinbase` | `coinbase` / `binance` |
| `MIN_EDGE` | `0.08` | Minimum edge vs market before betting (8¢) |
| `MARKET_PROB_ABOVE` | `0.50` | Reference YES price (set to live Kalshi/RH odds when available) |
| `STAKE_NOTIONAL` | `20` | Face value bought per bet (20 contracts ⇒ pay `20 × share_price`) |
| `CONTRACT_COST` | `0.50` | Legacy; ignored when using notional stake sizing |
| `AUTO_BET` | `true` | Place paper bets automatically |
| `LOOP_INTERVAL_SECONDS` | `10` | Poll cadence |

> **Note:** Cursor Cloud / some VPS regions get HTTP 451 from Binance. Prefer Coinbase (`BTC/USD`) or Kraken there.

## Probability model

For spot `S`, strike `K`, seconds remaining `τ`, and σ estimated from recent
log-returns:

\[
P(S_T > K) = N(d_2), \quad d_2 = \frac{\ln(S/K) - \tfrac{1}{2}\sigma^2\tau}{\sigma\sqrt{\tau}}
\]

Fair YES ≈ `prob_above * 100¢`, fair NO ≈ `prob_below * 100¢`.

## Paper contracts

`execution/prediction_book.py` mirrors Robinhood/Kalshi share math:

- `STAKE_NOTIONAL=20` ⇒ buy 20 contracts that each pay **$1** if correct
- At 53¢ YES: pay `20 × $0.53 = $10.60` now
- Win ⇒ receive `$20` (profit **+$9.40**); lose ⇒ forfeit the `$10.60`

## Legacy spot paper trader

The original EMA crossover + spot `PaperBroker` modules remain under
`strategies/` and `execution/paper_engine.py` if you want directional BTC
inventory simulation. The default `main.py` path is the prediction-market loop.
