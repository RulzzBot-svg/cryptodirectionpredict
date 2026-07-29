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

### Durability + alerts

- **Backups:** every few minutes the bot copies `paper_trading.db`,
  `logs/bot.log`, `logs/calibration.csv`, and a bets CSV into
  `BACKUP_DIR` (default `/opt/cursor/artifacts/paper-bot-backups`).
  On startup, if the local DB is missing, it restores the latest backup.
- **Calibration log:** `logs/calibration.csv` records model % vs market
  asks over time (and window outcomes) for later review.
- **Telegram:** set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` to get pings
  on bet placed, settlement, and window stats.
  1. Message `@BotFather` → `/newbot` → copy token
  2. Message your bot once, then get chat id via
     `https://api.telegram.org/bot<TOKEN>/getUpdates`
  3. Put both values in `.env` and restart

Settlement prefers the **official Kalshi YES/NO result** when available,
falling back to Coinbase spot otherwise.

### Kalshi cash-out + API prep (before live)

**Saving profits early is the right move.** On real Kalshi money: leave a fixed
working bank on the platform (e.g. ~$100 at $5 face) and withdraw settled cash
above that to your linked US bank (ACH, usually a few business days). Paper
mode does not move bank money — that habit is for live.

**Get API credentials this weekend** so launch next week is just wiring secrets,
not hunting for keys:

1. Log in at [kalshi.com](https://kalshi.com) (or [demo.kalshi.co](https://demo.kalshi.co) to practice)
2. **Account & security → API Keys → Create Key**
3. Save the downloaded **`.key` private key** somewhere safe (not in git — shown once)
4. Copy the **API Key ID** shown on screen
5. Put both in `.env` (see `.env.example`), then smoke-test:

```bash
pip install -r requirements.txt
python scripts/check_kalshi_auth.py
```

That call only reads `/portfolio/balance`. It does **not** place orders.
Paper trading keeps using the public Kalshi feed; authenticated keys are for
live later. Keep `LIVE_TRADING=false` until after the paper week review.

**Live scaffold (orders still off):** Kalshi V2 order payloads are built in
`execution/live_kalshi.py`. Real submits are hard-blocked until
**2026-07-30** and require `LIVE_CONFIRM=YES_I_FINISHED_PAPER_WEEK`.

```bash
python scripts/check_live_ready.py   # auth + sample payloads, no orders
# Optional on a non-Render copy: LIVE_DRY_RUN=true  (Telegram LIVE DRY-RUN pings)
```

**Order plumbing smoke test.** Verifies orders actually work before arming the
bot. Phase 1 places a 1¢ resting bid that cannot fill and cancels it (free);
phase 2 optionally buys one tiny contract to confirm a real fill.

```bash
python scripts/live_smoke_test.py                        # demo, cancel test only
python scripts/live_smoke_test.py --env prod --i-understand-real-money
python scripts/live_smoke_test.py --env prod --i-understand-real-money \
    --spend --contracts 1 --max-cost 1.00
```

Capped at 5 contracts and a `--max-cost` ceiling. It never touches
`LIVE_TRADING` — the bot keeps papering.

On Render, store `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PEM` (or mount the
`.key` file on the disk) as secrets — never commit them. Do **not** flip
`LIVE_TRADING` on the paper worker yet.

### Useful env vars

| Variable | Default | Meaning |
|----------|---------|---------|
| `SYMBOL` | `BTC/USD` | CCXT symbol |
| `DATA_PROVIDER` | `coinbase` | `coinbase` / `binance` |
| `PAPER_INITIAL_BALANCE` | `100` | Starting paper bankroll ($) |
| `MIN_EDGE` | `0.08` | Minimum edge vs ask before betting (8¢) |
| `MARKET_PROB_ABOVE` | `0.50` | Fallback YES ask if Kalshi quotes missing |
| `STAKE_NOTIONAL` | `5` | Face value per bet (5 contracts ⇒ pay `5 × share_price`) |
| `VAULT_ENABLED` | `true` | Auto paper-withdraw profits above working bank |
| `VAULT_TRIGGER_PROFIT` | `55` | Vault when cash ≥ working bank + this |
| `VAULT_WITHDRAW_AMOUNT` | `50` | Dollars moved to “put aside” each trigger |
| `VAULT_GOAL` | `300` | Stop auto-vault once put aside reaches this |
| `CONTRACT_COST` | `0.50` | Legacy; ignored when using notional stake sizing |
| `AUTO_BET` | `true` | Place paper bets automatically |
| `LOOP_INTERVAL_SECONDS` | `10` | Poll cadence |
| `LOG_DIR` / `LOG_FILE` | `logs` / `bot.log` | File that mirrors terminal output |
| `BACKUP_DIR` | `/opt/cursor/artifacts/paper-bot-backups` | Durable copy location |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | Enable Telegram trade alerts |
| `KALSHI_SERIES` | `KXBTC15M` | Contract series (ETH/SOL later if desired) |
| `KALSHI_API_KEY_ID` | — | Auth Key ID (live prep; not needed for paper) |
| `KALSHI_PRIVATE_KEY_PATH` / `KALSHI_PRIVATE_KEY_PEM` | — | RSA private key for request signing |
| `KALSHI_ENV` | `prod` | `prod` or `demo` (sets API base URL) |
| `LIVE_TRADING` | `false` | Must stay off until paper week is done |

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

### Paper profit vault

Same working bank forever while profits get “put aside”:

- Working bank **$100**, face **$5**
- When cash reaches **+$55** over the bank (≥ **$155**), withdraw **$50**
- Leaves ~**$105** so the next loss doesn’t start red under $100
- Repeats until **$300** is put aside, then auto-vault pauses

Telegram gets a `PAPER VAULT` ping on each withdraw. All-in P/L still counts
vaulted cash. Live Kalshi later: same habit, real ACH withdraw.

Reset paper W/L anytime:

```bash
python main.py --reset-paper
# or RESET_PAPER_HISTORY=true in .env for one run
```

## Legacy spot paper trader

The original EMA crossover + spot `PaperBroker` modules remain under
`strategies/` and `execution/paper_engine.py` if you want directional BTC
inventory simulation. The default `main.py` path is the prediction-market loop.
