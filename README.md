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

### Spot price freshness

The edge depends on Kalshi lagging the spot market, so our own spot has to be
fresher than theirs. Reading it over REST once per loop leaves it up to
`LOOP_INTERVAL_SECONDS` old — old enough that the apparent mispricing is our own
staleness. `SPOT_STREAM=true` (default) keeps a WebSocket price tape in memory,
typically sub-second, and falls back to the REST snapshot if it goes older than
`SPOT_MAX_AGE_SECONDS`.

The status line tags which source was used: `BTC $64,722.21 (ws)` versus
`(rest)`, and the heartbeat reports the tape's age.

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

**Arming live.** Use a **separate database** so live results aren't mixed into
paper history, and start the book at your real Kalshi cash:

```
LIVE_TRADING=true
LIVE_CONFIRM=YES_I_FINISHED_PAPER_WEEK
DATABASE_URL=sqlite:////var/data/live_trading.db
PAPER_INITIAL_BALANCE=108        # your actual Kalshi balance
```

The bot warns at startup if live mode is writing into a book that already holds
paper bets. In live mode the book **mirrors real fills** — a bet is recorded
only when an order actually fills, at the true fill price plus fee, for the
quantity that filled (partial fills included). Settlement, W/L, P/L, the vault,
and Telegram all work off that mirror, and the heartbeat shows your real Kalshi
cash next to it so any drift is visible. Telegram alerts are prefixed `[LIVE]`.

**Missed fills.** Live orders are IOC, so they either fill immediately or die.
A miss is retried on a later tick (up to `LIVE_MAX_ATTEMPTS`, default 3) but
only if the edge still clears `MIN_EDGE` against the **current** ask — the bot
never chases the price it originally wanted.

Immediately before each live order the bot pulls the **real orderbook** for
that market rather than trusting the market snapshot's derived `yes_ask`, which
lags. Kalshi's book lists bids on both legs, so the true cost to buy ABOVE is
`1 − best NO bid`. The edge is re-checked at that real price and the order is
abandoned if it no longer clears `MIN_EDGE`.

Orders are placed `LIVE_PRICE_TOLERANCE_CENTS` (default 1¢) above the ask
so a single tick doesn't cost the trade.

**Live pricing is orderbook-first.** Advice and orders use the real Kalshi
book, not the softer market snapshot. That stops the old loop where paper/live
saw an 8¢ edge, then skipped at submit (`edge gone on real book`) and barely
filled.

**`MIN_EDGE` is net of fees.** The gate subtracts estimated Kalshi taker/maker
fees before comparing to `MIN_EDGE`. Paper books those fees too (`PAPER_FEE_MODE`,
default `taker`), so paper stops looking free. Live never settles from Coinbase
spot while `LIVE_TRADING` is on. Resting orders are polled for fills before
cancel at window close.

**Taker vs maker (`LIVE_ORDER_MODE`).** The default `taker` mode crosses the
spread with an IOC order: it fills instantly or not at all, and pays the full
`0.07 × C × P × (1−P)` fee. Setting `maker` instead rests a **post-only** limit
order on the book for `LIVE_REST_SECONDS` (default 45) and lets the market come
to it. Maker edge is measured versus the **rest price (bid)**, not the ask, so
a wide ask no longer kills volume. Rest prices still respect `MIN_ENTRY_PRICE`
(no more sub-floor maker fills). That trades instant execution for a much
longer window to get hit, at roughly a **quarter of the fee**.

The trade-off is adverse selection — a resting bid tends to get filled exactly
when the price is moving against it. The short expiry limits how stale the
quote can get, orders are cancelled when the window rolls or the bot stops, and
`post_only` guarantees it never accidentally crosses. Compare the two modes at
1 contract before trusting either. Because these are limit orders you
still pay the **best available** price, so the tolerance only costs anything on
fills that would otherwise have missed — and the bid is capped so the edge never
drops below `MIN_EDGE`. Fill rate is printed after each order and included in
the heartbeat. Before each live order it checks
Kalshi for an existing position on that market, so a restart mid-window cannot
double a bet. If that check fails, the order is skipped rather than risked.

On Render, store `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PEM` (or mount the
`.key` file on the disk) as secrets — never commit them. Do **not** flip
`LIVE_TRADING` on the paper worker yet.

**Evening blackout (`BET_BLACKOUT_*`).** Live MAD showed a recurring knife in
the **7:00–11:00 PM America/Los_Angeles** window (02:00–06:00 UTC). With the
default blackout enabled, the bot still prices and settles, but it will not
place new paper/live orders (and it cancels any resting maker quote) inside
that local-time range. Set `BET_BLACKOUT_ENABLED=false` to disable, or change
`BET_BLACKOUT_START` / `END` / `TZ`.

**Run it without watching Render.** Keep `AUTO_BET=true` and `LIVE_TRADING=false`
until you explicitly arm live. Three automatic brakes then sit on top of the
edge filter:

1. **Evening blackout** — no new bets 7–11 PM America/Los_Angeles.
2. **Probability haircut (`PROB_HAIRCUT`, default 0.55)** — live MAD claimed
   ~78% and delivered ~67%. The haircut shrinks every probability toward 50¢
   (`p' = 0.5 + (p − 0.5) × 0.55`) so an 8¢ `MIN_EDGE` is real cents, not fake
   +12¢. Set `PROB_HAIRCUT=1` to disable. This is **not** online learning.
3. **Auto halt (`BET_HALT_*`)** — skip new bets (and cancel resting makers) for
   the rest of the local day after a **−$15** day or **≤56% WR on ≥25 bets**,
   and while cash **≤ $30**. Day halts lift at local midnight; the bank floor
   lifts when cash recovers. Telegram fires once when a halt starts.
4. **Daily Telegram digest (`DIGEST_HOUR=7` LA)** — one scorecard (bank, FULL,
   SINCE_HAIRCUT, TODAY, LAST50, halt, stay-paper vs sample-OK). Default
   `TELEGRAM_QUIET=true` turns off per-bet spam so you only see this plus
   halt/vault. Not an LLM predictor — 15m BTC does not need one.

Do **not** flip `LIVE_TRADING` on just because paper is automatic. Haircut +
halt + blackout are what make unattended paper safe-enough; live still needs a
human env change after a clean paper stretch.

### Useful env vars

| Variable | Default | Meaning |
|----------|---------|---------|
| `SYMBOL` | `BTC/USD` | CCXT symbol |
| `DATA_PROVIDER` | `coinbase` | `coinbase` / `binance` |
| `PAPER_INITIAL_BALANCE` | `100` | Starting paper bankroll ($) |
| `MIN_EDGE` | `0.08` | Minimum edge vs ask before betting (8¢) |
| `MIN_ENTRY_PRICE` | `0` (off) | Skip if share ask is below this (e.g. `0.45`) |
| `MAX_ENTRY_PRICE` | `0` (off) | Skip if share ask is above this (e.g. `0.74`) |
| `MIN_MODEL_PROB` | `0` (off) | Skip if model prob on the bet side is below this |
| `MAX_MODEL_PROB` | `0` (off) | Skip if model prob on the bet side is above this |
| `MARKET_PROB_ABOVE` | `0.50` | Fallback YES ask if Kalshi quotes missing |
| `STAKE_NOTIONAL` | `5` | Face value per bet (5 contracts ⇒ pay `5 × share_price`) |
| `VAULT_ENABLED` | `true` | Auto paper-withdraw profits above working bank |
| `VAULT_TRIGGER_PROFIT` | `55` | Vault when cash ≥ working bank + this |
| `VAULT_WITHDRAW_AMOUNT` | `50` | Dollars moved to “put aside” each trigger |
| `VAULT_GOAL` | `300` | Stop auto-vault once put aside reaches this |
| `CONTRACT_COST` | `0.50` | Legacy; ignored when using notional stake sizing |
| `AUTO_BET` | `true` | Place paper bets automatically |
| `BET_BLACKOUT_ENABLED` | `true` | Skip new bets in a local-time window |
| `BET_BLACKOUT_TZ` | `America/Los_Angeles` | IANA timezone for the blackout |
| `BET_BLACKOUT_START` / `END` | `19:00` / `23:00` | Half-open local window (7–11 PM LA) |
| `PROB_HAIRCUT` | `0.55` | Shrink model probs toward 50¢ (`1` = raw model) |
| `BET_HALT_ENABLED` | `true` | Auto-skip new bets on a kill day / bank floor |
| `BET_HALT_BANK_FLOOR` | `30` | No new bets while cash ≤ this |
| `BET_HALT_DAY_LOSS` | `15` | Halt rest of local day after −$15 |
| `BET_HALT_DAY_MIN_BETS` / `DAY_MAX_WR` | `25` / `0.56` | Halt rest of day if WR ≤56% on ≥25 bets |
| `DIGEST_HOUR` | `7` | Local hour to send the daily Telegram scorecard |
| `HAIRCUT_SINCE` | `2026-08-18 17:52:00` | Start of the post-haircut sample in the digest |
| `TELEGRAM_QUIET` | `true` | Skip per-bet/heartbeat spam; keep digest + halts + vault |
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

## Choosing a probability model

Live results forced this question. BTC's 15-minute returns have excess kurtosis
around **+16** — a sharp peak with rare violent jumps — so no single sigma
describes them. Measured over 645 windows:

| Measure | 15m move (1σ) |
|---|---|
| Standard deviation | 0.150% |
| MAD / IQR | 0.089% / 0.088% |
| What Kalshi's prices imply | 0.089% |

Fitting sigma to the standard deviation overstates the ordinary window, which
inflates the probability of reaching a nearby strike — and that is the side an
edge-seeking bot buys. Running that way, the model was **15.6 points
overconfident** on the bets it selected, claiming 11.7¢ of edge and realizing
−3.9¢.

`VOL_ESTIMATOR=mad` matches the market's implied width but has thin tails, which
understates jumps. `PROB_MODEL=empirical` uses the observed distribution instead
and keeps both the peak and the tails. It is **unverified** — it disagrees with
market pricing in the same direction the too-wide estimator did — so prove it
with a paper run and `calibration_report.py` before funding it.

MAD paper/live also showed a clean split by ticket price: **&lt;45¢ longshots and
&gt;74¢ / &gt;85% favorites lost**, while **45–74¢** carried the book. Use the entry /
model bounds above (e.g. `MIN_ENTRY_PRICE=0.45`, `MAX_ENTRY_PRICE=0.74`,
`MIN_MODEL_PROB=0.45`, `MAX_MODEL_PROB=0.85`) so those buckets cannot fire.

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
vaulted cash.

**The vault moves no money.** It is bookkeeping: it marks cash as set aside and
shrinks the bank the bot trades against, so a good run doesn't quietly inflate
how rich the bot thinks it is. On live, every vaulted dollar is still sitting in
Kalshi and still at risk until you withdraw it to your bank yourself. The alert
says so each time.

The trigger scales with the configured working bank, which is
`PAPER_INITIAL_BALANCE` — seed the book at $127 and the first vault fires at
$182, not $155.

### After depositing or withdrawing

The book can't see cash moving on the exchange, so withdrawing leaves it
believing it has money that isn't there. Set `RECONCILE_BANK=true` for one
restart: it sets the book's cash to the real Kalshi balance and **keeps all bet
history**, so calibration data survives. Set it back to `false` afterwards, or
the next restart will overwrite a live balance.

Leave enough on the exchange for the bot to keep trading — at `$5` face it risks
roughly $1–4 per window, so a working balance near $100 is comfortable and
anything under ~$20 will start getting orders rejected.

### Results breakdown (hour / side / price)

Read-only report — safe to run while the bot is live:

```bash
python scripts/hourly_pnl.py --block 6                      # 6-hour blocks, UTC
python scripts/hourly_pnl.py --tz America/Los_Angeles --block 3
python scripts/hourly_pnl.py --db /var/data/paper_trading.db
python scripts/hourly_pnl.py --csv /var/data/backups/bets.latest.csv
```

Shows settled count, W/L, win rate, average price paid, P/L, and ROI on money
risked — grouped by hour placed, by ABOVE/BELOW, and by entry-price bucket.
Useful for checking whether a time-of-day effect is real before acting on it.

### Model calibration

The edge only exists if the probabilities are honest — when the model says 27%,
those bets should win about 27% of the time. If they win materially less, the
edge is arithmetic on a wrong number and better fills won't help.

```bash
python scripts/calibration_report.py --db /var/data/live_v4.db
python scripts/calibration_report.py --db /var/data/paper_trading.db --buckets 5
```

Reports predicted versus actual win rate per confidence bucket, the Brier
score, and — most usefully — **edge claimed versus edge realized**. If realized
edge is near zero while claimed edge is 10¢+, the model is overconfident and
that's the thing to fix, not execution.

Reset paper W/L anytime:

```bash
python main.py --reset-paper
# or RESET_PAPER_HISTORY=true in .env for one run
```

## Legacy spot paper trader

The original EMA crossover + spot `PaperBroker` modules remain under
`strategies/` and `execution/paper_engine.py` if you want directional BTC
inventory simulation. The default `main.py` path is the prediction-market loop.
