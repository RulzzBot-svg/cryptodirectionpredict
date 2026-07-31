#!/usr/bin/env python3
"""
BTC 15-minute prediction-market edge bot.

Estimates P(finish ABOVE strike) for each wall-clock 15m window, recommends
ABOVE / BELOW / SKIP, and optionally papers the bet against a 50/50 book.

Manual / Kalshi strike sources
------------------------------
By default the bot pulls strike + YES price from Kalshi's public API
(same BTC 15m contracts Robinhood shows).

Override manually if needed:
  python main.py --strike 64737.27 --market-cents 55
  echo 64737.27 > manual_strike.txt
  echo 55 > market_cents.txt
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from config.bot_logging import setup_bot_logging, shutdown_bot_logging
from config.live_gate import (
    EARLIEST_LIVE_DATE,
    enforce_live_gate,
    live_dry_run_requested,
    live_trading_requested,
)
from config.settings import load_settings
from data.backup import backup_now, restore_latest_backup
from data.calibration import CalibrationLog
from data.feed import close_exchange, create_rest_exchange, fetch_latest_snapshot
from data.kalshi import (
    fetch_current_btc_15m,
    fetch_orderbook,
    fetch_window_settlement,
)
from data.kalshi_auth import KalshiAuthClient, KalshiAuthError, credentials_configured
from data.price_tape import PriceTape
from execution.live_kalshi import LiveKalshiExecutor, RestingOrder
from execution.prediction_book import PredictionBook
from models.db import create_db_engine, create_session_factory, init_db
from notifications import TelegramNotifier
from prediction.advisor import PredictionAdvisor
from prediction.window import WindowManager

load_dotenv()

logger = logging.getLogger("main")

LOOP_INTERVAL_SECONDS = float(os.getenv("LOOP_INTERVAL_SECONDS", "10"))
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.08"))
CONTRACT_COST = float(os.getenv("CONTRACT_COST", "0.50"))  # legacy unused
STAKE_NOTIONAL = float(os.getenv("STAKE_NOTIONAL", "5"))
AUTO_BET = os.getenv("AUTO_BET", "true").strip().lower() in {"1", "true", "yes", "on"}
MIN_SECONDS_TO_BET = float(os.getenv("MIN_SECONDS_TO_BET", "20"))
RESET_PAPER_HISTORY = os.getenv("RESET_PAPER_HISTORY", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BACKUP_EVERY_SECONDS = float(os.getenv("BACKUP_EVERY_SECONDS", "300"))
CALIBRATION_EVERY_SECONDS = float(os.getenv("CALIBRATION_EVERY_SECONDS", "60"))
# Telegram "still alive" ping (0 disables)
HEARTBEAT_EVERY_SECONDS = float(os.getenv("HEARTBEAT_EVERY_SECONDS", "900"))
# kalshi (default) | manual | auto
STRIKE_SOURCE = os.getenv("STRIKE_SOURCE", "kalshi").strip().lower()
KALSHI_SERIES = os.getenv("KALSHI_SERIES", "KXBTC15M")

# Paper profit vault: when cash ≥ working + trigger, withdraw amount into "put aside"
VAULT_ENABLED = os.getenv("VAULT_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
VAULT_TRIGGER_PROFIT = float(os.getenv("VAULT_TRIGGER_PROFIT", "55"))
VAULT_WITHDRAW_AMOUNT = float(os.getenv("VAULT_WITHDRAW_AMOUNT", "50"))
VAULT_GOAL = float(os.getenv("VAULT_GOAL", "300"))

# Live scaffold (orders OFF by default; hard-gated until paper week ends)
LIVE_TRADING = live_trading_requested()
LIVE_DRY_RUN = live_dry_run_requested()
# A missed IOC order may be retried on later ticks, but only while the edge
# still qualifies at the *current* ask — never by chasing the old price.
LIVE_MAX_ATTEMPTS = int(os.getenv("LIVE_MAX_ATTEMPTS", "5"))
# Bid this far above the displayed ask so a 1¢ tick doesn't cost the trade.
# A limit order still pays the best available price, so this only costs money
# on fills that would otherwise have missed entirely.
LIVE_PRICE_TOLERANCE = float(os.getenv("LIVE_PRICE_TOLERANCE_CENTS", "1")) / 100.0
# taker  = cross the spread now (IOC), fills instantly or not at all
# maker  = rest a post-only limit and let the market come to us. Higher fill
#          rate over a 15m window and a quarter of the fee, at the cost of
#          adverse selection. Short expiry keeps the quote from going stale.
LIVE_ORDER_MODE = os.getenv("LIVE_ORDER_MODE", "taker").strip().lower()
LIVE_REST_SECONDS = float(os.getenv("LIVE_REST_SECONDS", "45"))
# Stream spot instead of reading it over REST once a loop. Any edge against
# Kalshi depends on our price being fresher than theirs, not older.
SPOT_STREAM = os.getenv("SPOT_STREAM", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
SPOT_MAX_AGE_SECONDS = float(os.getenv("SPOT_MAX_AGE_SECONDS", "3"))
# Kalshi finalizes a market a little after the window closes. Settling from our
# own spot reading instead disagrees with Kalshi whenever price is near the
# strike — which is nearly always — so wait for the official result.
SETTLE_REQUIRE_OFFICIAL = os.getenv("SETTLE_REQUIRE_OFFICIAL", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
SETTLE_WAIT_SECONDS = float(os.getenv("SETTLE_WAIT_SECONDS", "600"))

STRIKE_FILE = Path(os.getenv("MANUAL_STRIKE_FILE", "manual_strike.txt"))
MARKET_CENTS_FILE = Path(os.getenv("MARKET_CENTS_FILE", "market_cents.txt"))


def _utcnow_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _parse_number(raw: str) -> Optional[float]:
    text = raw.strip().replace(",", "").replace("$", "").replace("¢", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _read_number_file(path: Path) -> Optional[float]:
    if not path.exists():
        return None
    try:
        return _parse_number(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _print_status(
    *,
    price: float,
    strike: float,
    strike_source: str,
    remaining: float,
    p_above: float,
    p_below: float,
    action: str,
    edge: float,
    bankroll: float,
    market_prob: float,
    spot_source: str = "rest",
) -> None:
    src = {"manual": "RH", "kalshi": "KL", "auto": "auto"}.get(strike_source, strike_source)
    spot_tag = {"websocket": "ws", "rest-fallback": "rest*", "rest": "rest"}.get(
        spot_source, spot_source
    )
    line = (
        f"[{_utcnow_label()}] "
        f"BTC ${price:,.2f} ({spot_tag}) | "
        f"Strike ${strike:,.2f} ({src}) | "
        f"T-{_fmt_mmss(remaining)} | "
        f"Above {p_above * 100:5.2f}% | "
        f"Below {p_below * 100:5.2f}% | "
        f"Mkt {market_prob * 100:4.1f}¢ | "
        f"Edge {edge * 100:+5.1f}¢ | "
        f"{action:<5} | "
        f"Bank ${bankroll:,.2f}"
    )
    # Render/log hosts aren't TTYs — \r status lines never appear. Use newlines there.
    if sys.stdout.isatty():
        print(f"\r{line:<150}", end="", flush=True)
    else:
        print(line, flush=True)


def _print_performance(stats: dict[str, Any], *, kalshi_event: str = "") -> None:
    print()
    print("=" * 60)
    print("  PREDICTION MARKET — FINAL PERFORMANCE")
    print("=" * 60)
    print(f"  Time            : {_utcnow_label()}")
    if kalshi_event:
        print(f"  Last Kalshi mkt : {kalshi_event}")
    print(f"  Starting bank   : ${stats['starting_balance']:,.2f}")
    print(f"  Cash bankroll   : ${stats['usd_balance']:,.2f}")
    print(f"  Vault (aside)   : ${float(stats.get('vaulted_usd') or 0):,.2f}")
    print(f"  Equity (all-in) : ${stats['equity']:,.2f}")
    total_pnl = stats["total_pnl"]
    pnl_label = f"+${total_pnl:,.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):,.2f}"
    print(f"  Total P/L       : {pnl_label} ({stats['total_return_pct']:+.2f}%)")
    print(f"  Realized P/L    : ${stats['realized_pnl']:,.2f}")
    print(
        f"  Contracts       : {stats['bet_count']} placed / "
        f"{stats['settled_count']} settled / {stats['open_bets']} open"
    )
    if stats["win_count"] or stats["loss_count"]:
        print(
            f"  Win rate        : {stats['win_rate_pct']:.1f}% "
            f"({stats['win_count']}W / {stats['loss_count']}L / "
            f"{stats['push_count']}P)"
        )
    print("=" * 60)


def _print_window_performance(stats: dict[str, Any]) -> None:
    """Compact running scoreboard after each 15m window settles."""
    total_pnl = float(stats["total_pnl"])
    realized = float(stats["realized_pnl"])
    total_txt = f"+${total_pnl:,.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):,.2f}"
    realized_txt = f"+${realized:,.2f}" if realized >= 0 else f"-${abs(realized):,.2f}"
    if stats["win_count"] or stats["loss_count"]:
        wr = (
            f"{stats['win_rate_pct']:.1f}% "
            f"({stats['win_count']}W/{stats['loss_count']}L/"
            f"{stats['push_count']}P)"
        )
    else:
        wr = "n/a (no settled bets yet)"
    vaulted = float(stats.get("vaulted_usd") or 0.0)
    print("-" * 60)
    print(
        f"  WINDOW STATS | Total P/L {total_txt} | "
        f"Realized P/L {realized_txt} | Win rate {wr}"
    )
    print(
        f"  Bank ${float(stats['usd_balance']):,.2f} | "
        f"Vault ${vaulted:,.2f} | "
        f"All-in ${float(stats['equity']):,.2f}"
    )
    print("-" * 60)


def _limit_price_with_tolerance(
    *,
    ask: float,
    model_prob: float,
    tolerance: float = LIVE_PRICE_TOLERANCE,
    min_edge: float = MIN_EDGE,
) -> float:
    """Highest price worth bidding for this side.

    Willing to pay a little above the displayed ask so a one-cent tick doesn't
    cost the trade, but never past the point where the edge drops below
    ``min_edge``. Rounded down to a whole cent to stay on Kalshi's tick grid.
    """
    if tolerance <= 0:
        return ask
    edge_ceiling = model_prob - min_edge
    limit = min(ask + tolerance, edge_ceiling)
    limit = math.floor(limit * 100.0) / 100.0
    # Never bid below the ask we already qualified on
    return max(ask, min(limit, 0.98))


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BTC 15m prediction-market edge bot",
    )
    parser.add_argument(
        "--strike",
        type=str,
        default=os.getenv("MANUAL_STRIKE"),
        help="Manual strike override (e.g. 64737.27). Overrides Kalshi.",
    )
    parser.add_argument(
        "--market-cents",
        type=str,
        default=None,
        help="Manual YES price in cents (e.g. 55). Overrides Kalshi.",
    )
    parser.add_argument(
        "--no-kalshi",
        action="store_true",
        help="Disable Kalshi auto strike/odds (use candle open / manual only).",
    )
    parser.add_argument(
        "--reset-paper",
        action="store_true",
        help="Clear paper W/L history and reset bankroll before starting.",
    )
    return parser.parse_args(argv)


def _normalize_market_prob(value: Optional[float]) -> Optional[float]:
    """Normalize cents or dollars; treat 0/1 as missing (empty book)."""
    if value is None:
        return None
    if value > 1.0:
        value = value / 100.0
    if value <= 0.0 or value >= 1.0:
        return None
    return float(value)


async def run_bot(
    *,
    initial_strike: Optional[float] = None,
    initial_market_prob: Optional[float] = None,
    use_kalshi: bool = True,
    reset_paper: bool = False,
) -> None:
    settings = load_settings()
    symbol = settings.symbol
    provider = settings.data_provider
    # Priority later: manual file > CLI > Kalshi > 0.50 default
    fallback_mkt = _normalize_market_prob(
        float(os.getenv("MARKET_PROB_ABOVE", "0.50"))
    ) or 0.50
    market_prob_above = (
        initial_market_prob if initial_market_prob is not None else fallback_mkt
    )
    market_locked = initial_market_prob is not None
    yes_ask: Optional[float] = market_prob_above if market_locked else None
    no_ask: Optional[float] = (
        (1.0 - market_prob_above) if market_locked else None
    )

    # Survive cloud rebuilds: restore DB from durable artifacts if local file missing
    if not reset_paper and not RESET_PAPER_HISTORY:
        if restore_latest_backup(database_url=settings.database_url):
            print(f"[{_utcnow_label()}] Restored paper DB from durable backup")

    notifier = TelegramNotifier()
    calibration = CalibrationLog()

    print("=" * 60)
    print("  BTC 15m PREDICTION EDGE BOT")
    print("=" * 60)
    print(f"  Symbol          : {symbol}")
    print(f"  Provider        : {provider}")
    print(f"  Candle TF       : {TIMEFRAME}")
    print(f"  Loop interval   : {LOOP_INTERVAL_SECONDS:.0f}s")
    print(f"  Min edge        : {MIN_EDGE * 100:.0f}¢")
    print(f"  Strike source   : {'kalshi' if use_kalshi else 'manual/auto'}")
    if market_locked:
        print(
            f"  Market YES/NO   : {market_prob_above * 100:.1f}¢ / "
            f"{(1.0 - market_prob_above) * 100:.1f}¢ (manual)"
        )
    else:
        print("  Market YES/NO   : live Kalshi asks (skip if empty/0¢)")
    print(
        f"  Stake notional  : ${STAKE_NOTIONAL:,.2f} face "
        f"(pay share_price × {STAKE_NOTIONAL:g} contracts)"
    )
    print(f"  Starting bank   : ${settings.paper_initial_balance:,.2f}")
    if VAULT_ENABLED:
        print(
            f"  Paper vault     : ON — at +${VAULT_TRIGGER_PROFIT:g} over bank "
            f"withdraw ${VAULT_WITHDRAW_AMOUNT:g} "
            f"(goal ${VAULT_GOAL:g} put aside)"
        )
    else:
        print("  Paper vault     : OFF")
    print(f"  Auto-bet        : {'ON' if AUTO_BET else 'OFF (advice only)'}")
    if LIVE_TRADING:
        print("  Live trading    : ARMED (real Kalshi orders)")
    elif LIVE_DRY_RUN:
        print(
            "  Live trading    : DRY-RUN (log/Telegram payloads only; "
            "paper bets still run)"
        )
    else:
        print(
            f"  Live trading    : OFF (earliest unlock {EARLIEST_LIVE_DATE.isoformat()})"
        )
    print(f"  Telegram alerts : {'ON' if notifier.active else 'OFF (set token/chat id)'}")
    if initial_strike:
        print(f"  Manual strike   : ${initial_strike:,.2f}")
    else:
        print("  Manual strike   : none (Kalshi/auto)")
    print(f"  Database        : {settings.database_url}")
    print(f"  Log file        : {os.getenv('_BOT_LOG_PATH', 'logs/bot.log')}")
    print(f"  Calibration log : {calibration.path}")
    print(f"  Backup dir      : {os.getenv('BACKUP_DIR', '/opt/cursor/artifacts/paper-bot-backups')}")
    print(f"  Started         : {_utcnow_label()}")
    print("=" * 60)
    print("  Kalshi auto-selects the current ET window ticker")
    print("  (e.g. KXBTC15M-26JUL231400). Manual files are ignored in this mode.")
    print("  Settles prefer official Kalshi YES/NO result when available.")
    print("  Win: receive full face (= stake back + profit). Lose: lose stake paid.")
    print("  DB/log backed up periodically to survive cloud rebuilds.")
    print("  Press Ctrl+C to stop and print performance stats.")
    print("=" * 60)
    print()

    engine = create_db_engine(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    book = PredictionBook(
        session_factory,
        initial_balance=settings.paper_initial_balance,
        symbol=symbol,
        stake_notional=STAKE_NOTIONAL,
        vault_enabled=VAULT_ENABLED,
        vault_working_bank=settings.paper_initial_balance,
        vault_trigger_profit=VAULT_TRIGGER_PROFIT,
        vault_withdraw_amount=VAULT_WITHDRAW_AMOUNT,
        vault_goal=VAULT_GOAL,
        engine=engine,
    )

    if LIVE_TRADING:
        notifier.prefix = "LIVE"
        prior = book.get_performance_stats()
        if prior["settled_count"] > 0:
            print(
                f"[{_utcnow_label()}] Live mode resuming a book with "
                f"{prior['settled_count']} settled bets "
                f"(bank ${prior['usd_balance']:,.2f}). If this database was "
                "used for paper, point DATABASE_URL at a fresh file so live "
                "stats stay clean."
            )

    live_exec: Optional[LiveKalshiExecutor] = None
    if LIVE_TRADING or LIVE_DRY_RUN:
        auth_client = None
        if credentials_configured():
            try:
                auth_client = KalshiAuthClient.from_env()
                bal = auth_client.get_balance()
                print(
                    f"[{_utcnow_label()}] Kalshi auth OK | "
                    f"cash ${bal.balance_usd:,.2f} | base {auth_client.base_url}"
                )
            except KalshiAuthError as exc:
                print(f"[{_utcnow_label()}] Kalshi auth FAILED: {exc}")
                if LIVE_TRADING:
                    raise SystemExit("LIVE_TRADING requires working Kalshi credentials") from exc
        elif LIVE_TRADING:
            raise SystemExit("LIVE_TRADING requires KALSHI_API_KEY_ID + private key")
        else:
            print(
                f"[{_utcnow_label()}] LIVE_DRY_RUN on without creds — "
                "will only log local payloads"
            )
        live_exec = LiveKalshiExecutor(
            auth_client,
            dry_run=not LIVE_TRADING,
            stake_notional=STAKE_NOTIONAL,
        )
    if reset_paper or RESET_PAPER_HISTORY:
        book.reset_paper_history(balance=settings.paper_initial_balance)
    windows = WindowManager(window_minutes=15)
    advisor = PredictionAdvisor(
        min_edge=MIN_EDGE,
        market_prob_above=market_prob_above,
        min_seconds_to_bet=MIN_SECONDS_TO_BET,
    )
    backup_now(database_url=settings.database_url)
    if notifier.active:
        # Always lead with the wiped-run recap so Telegram has the history
        notifier.previous_run_recap()
        stats0 = book.get_performance_stats()
        vault_txt = (
            f"vault +${VAULT_TRIGGER_PROFIT:g}→${VAULT_WITHDRAW_AMOUNT:g} "
            f"(goal ${VAULT_GOAL:g})"
            if VAULT_ENABLED
            else "vault OFF"
        )
        notifier.info(
            f"NEW RUN STARTED | bank ${stats0['usd_balance']:,.2f} | "
            f"aside ${float(stats0.get('vaulted_usd') or 0):,.2f} | "
            f"{stats0['win_count']}W/{stats0['loss_count']}L | "
            f"face ${STAKE_NOTIONAL:g} | {vault_txt} | series {KALSHI_SERIES}"
        )

    tape: Optional[PriceTape] = None
    if SPOT_STREAM:
        tape = PriceTape(
            symbol, provider=provider, max_age_seconds=SPOT_MAX_AGE_SECONDS
        )
        await tape.start()
        print(
            f"[{_utcnow_label()}] Spot stream starting "
            f"(fall back to REST if older than {SPOT_MAX_AGE_SECONDS:g}s)"
        )

    exchange: Any = None
    consecutive_errors = 0
    last_announced_strike: Optional[float] = None
    pending_manual_strike = initial_strike  # CLI / env only
    kalshi_strike: Optional[float] = None
    kalshi_event: str = ""
    kalshi_market_ticker: str = ""
    # Per-window live order state (paper book already guards itself)
    live_attempts: dict[str, int] = {}
    live_filled_windows: set[str] = set()
    live_fill_count = 0
    live_miss_count = 0
    resting: Optional[RestingOrder] = None
    # Why the bot didn't bet, counted per window. Without this a bot that is
    # correctly declining every price looks identical to a frozen one.
    skip_counts: dict[str, int] = {}

    def note_skip(reason: str) -> None:
        skip_counts[reason] = skip_counts.get(reason, 0) + 1

    # Windows whose outcome Kalshi hasn't published yet: (window, ticker, closed_at)
    pending_settlements: list[tuple[Any, str, float]] = []

    # Advice/window captured when the order was placed — the model view may have
    # moved on by the time it fills, but the bet belongs to the original call.
    resting_advice: Any = None
    resting_window: Any = None
    warned_stale_file = False
    last_backup_at = datetime.now(timezone.utc).timestamp()
    last_calibration_at = 0.0
    last_heartbeat_at = 0.0
    last_cal_window = ""

    try:
        exchange = create_rest_exchange(provider)
        while True:
            # Manual file overrides only when NOT using Kalshi auto mode
            if not use_kalshi:
                file_strike = _read_number_file(STRIKE_FILE)
                if file_strike is not None:
                    pending_manual_strike = file_strike
                file_mkt = _read_number_file(MARKET_CENTS_FILE)
                if file_mkt is not None:
                    normalized = _normalize_market_prob(file_mkt)
                    if normalized is not None:
                        market_prob_above = normalized
                        yes_ask = normalized
                        no_ask = 1.0 - normalized
                        market_locked = True
            elif not warned_stale_file and (
                STRIKE_FILE.exists() or MARKET_CENTS_FILE.exists()
            ):
                print()
                print(
                    f"[{_utcnow_label()}] Ignoring {STRIKE_FILE.name}/"
                    f"{MARKET_CENTS_FILE.name} while Kalshi auto mode is on. "
                    f"Delete those files or pass --no-kalshi to use them."
                )
                warned_stale_file = True

            # Kalshi public feed: exact current ET window ticker (…-26JUL231400)
            if use_kalshi:
                try:
                    kalshi = await asyncio.to_thread(
                        fetch_current_btc_15m, series_ticker=KALSHI_SERIES
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Kalshi fetch failed: %s", exc)
                    kalshi = None
                if kalshi is not None:
                    kalshi_event = kalshi.event_ticker or kalshi.ticker
                    kalshi_market_ticker = kalshi.ticker or kalshi_market_ticker
                    if kalshi.strike is not None:
                        kalshi_strike = float(kalshi.strike)
                    if not market_locked:
                        yes_ask = kalshi.buy_yes_price
                        no_ask = kalshi.buy_no_price
                        if yes_ask is not None:
                            market_prob_above = float(yes_ask)

            try:
                snapshot = await fetch_latest_snapshot(
                    symbol,
                    timeframe=TIMEFRAME,
                    provider=provider,
                    exchange=exchange,
                )
                consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                consecutive_errors += 1
                logger.warning(
                    "Market data fetch failed (%s). Retrying in %.0fs [%d]",
                    exc,
                    LOOP_INTERVAL_SECONDS,
                    consecutive_errors,
                )
                if consecutive_errors >= 3:
                    await close_exchange(exchange)
                    exchange = create_rest_exchange(provider)
                    consecutive_errors = 0
                await asyncio.sleep(LOOP_INTERVAL_SECONDS)
                continue

            price = float(snapshot["last_price"] or 0.0)
            # Prefer the streamed price — a REST snapshot can be seconds old,
            # and an edge measured against a stale spot is our lag, not Kalshi's.
            spot_source = "rest"
            if tape is not None:
                streamed = tape.fresh_price()
                if streamed is not None and streamed > 0:
                    price = streamed
                    spot_source = tape.transport
            if price <= 0:
                await asyncio.sleep(LOOP_INTERVAL_SECONDS)
                continue

            candles = snapshot["candles"]
            strike_hint = None
            if candles is not None and not candles.empty and "open" in candles.columns:
                try:
                    last_open = float(candles.iloc[-1]["open"])
                    if last_open > 0:
                        strike_hint = last_open
                except (TypeError, ValueError, KeyError):
                    strike_hint = None

            # Prefer Kalshi strike as the lock price for new windows
            lock_price = kalshi_strike or strike_hint
            window, expired = windows.update(price, strike_price=lock_price)
            if expired is not None:
                # Don't settle yet — Kalshi hasn't finalized the market. Queue it
                # and resolve once the official outcome is available.
                pending_settlements.append(
                    (expired, kalshi_market_ticker, datetime.now(timezone.utc).timestamp())
                )
                print()
                print(
                    f"[{_utcnow_label()}] Window {expired.window_id} closed "
                    f"(strike ${float(expired.strike):,.2f}) — awaiting Kalshi result"
                )
                # CLI manual strike applies to one window unless re-passed
                if initial_strike is None:
                    pending_manual_strike = None
                last_announced_strike = None
                kalshi_strike = None
                kalshi_event = ""
                kalshi_market_ticker = ""
                live_attempts.pop(expired.window_id, None)
                live_filled_windows.discard(expired.window_id)
                if resting is not None and live_exec is not None:
                    # Never let an order outlive the window it was priced for
                    await asyncio.to_thread(live_exec.cancel_resting, resting)
                    print(
                        f"[{_utcnow_label()}] LIVE cancelled resting order "
                        f"at window close ({resting.advice_side} "
                        f"@{resting.limit_price*100:.0f}¢)"
                    )
                    resting = None
                    resting_advice = None
                    resting_window = None
                if not market_locked:
                    yes_ask = None
                    no_ask = None

            # Apply explicit overrides / Kalshi strike onto active window
            applied_source = None
            target_strike = None
            if pending_manual_strike is not None:
                target_strike = pending_manual_strike
                applied_source = "manual"
            elif kalshi_strike is not None:
                target_strike = kalshi_strike
                applied_source = "kalshi"

            if target_strike is not None:
                changed = windows.apply_manual_strike(
                    target_strike, source=applied_source or "manual"
                )
                if changed and (
                    last_announced_strike is None
                    or abs(last_announced_strike - target_strike) > 1e-9
                ):
                    print()
                    label = "Manual" if applied_source == "manual" else "Kalshi"
                    print(
                        f"[{_utcnow_label()}] {label} strike set to "
                        f"${target_strike:,.2f}"
                    )
                    last_announced_strike = target_strike

            if window.strike is None:
                await asyncio.sleep(LOOP_INTERVAL_SECONDS)
                continue

            advice = advisor.advise(
                window,
                price,
                snapshot["candles"],
                market_prob_above=market_prob_above,
                yes_ask=yes_ask,
                no_ask=no_ask,
            )

            now_ts = datetime.now(timezone.utc).timestamp()
            if (
                window.window_id != last_cal_window
                or (now_ts - last_calibration_at) >= CALIBRATION_EVERY_SECONDS
            ):
                calibration.log_advice(
                    window_id=window.window_id,
                    symbol=symbol,
                    spot=price,
                    strike=float(window.strike),
                    seconds_remaining=window.seconds_remaining(),
                    prob_above=advice.prob_above,
                    prob_below=advice.prob_below,
                    yes_ask=yes_ask,
                    no_ask=no_ask,
                    action=advice.action,
                    edge=advice.edge,
                )
                last_calibration_at = now_ts
                last_cal_window = window.window_id

            # Resolve any closed window whose official outcome has landed.
            if pending_settlements:
                still_pending: list[tuple[Any, str, float]] = []
                for closed, closed_ticker, closed_at in pending_settlements:
                    outcome_side: Optional[str] = None
                    settle_price: Optional[float] = None
                    settle_source = "kalshi_official"
                    if use_kalshi:
                        side, exp_val, src = await asyncio.to_thread(
                            fetch_window_settlement,
                            series_ticker=KALSHI_SERIES,
                            window_end=closed.end,
                            market_ticker=closed_ticker or None,
                        )
                        if side in ("ABOVE", "BELOW"):
                            outcome_side = side
                            settle_source = src
                            if exp_val is not None and exp_val > 0:
                                settle_price = float(exp_val)

                    waited = datetime.now(timezone.utc).timestamp() - closed_at
                    if outcome_side is None:
                        if SETTLE_REQUIRE_OFFICIAL and waited < SETTLE_WAIT_SECONDS:
                            still_pending.append((closed, closed_ticker, closed_at))
                            continue
                        # Out of patience. Our own spot disagrees with Kalshi near
                        # the strike, so say so loudly rather than book it quietly.
                        outcome_side = None
                        settle_price = float(closed.settlement_price or price)
                        settle_source = "coinbase_spot_fallback"
                        print(
                            f"[{_utcnow_label()}] WARNING: no Kalshi result for "
                            f"{closed.window_id} after {waited:.0f}s — settling from "
                            "spot, which may disagree with Kalshi"
                        )

                    if settle_price is None:
                        settle_price = float(closed.settlement_price or price)
                    closed.outcome = outcome_side
                    closed.settlement_price = settle_price
                    print(
                        f"[{_utcnow_label()}] Window {closed.window_id} settled "
                        f"{outcome_side or '(from spot)'} @ ${settle_price:,.2f} "
                        f"(strike ${float(closed.strike):,.2f}) via {settle_source} "
                        f"after {waited:.0f}s"
                    )
                    settled_bet = book.settle_window(
                        closed,
                        settle_price,
                        outcome_side=outcome_side
                        if outcome_side in ("ABOVE", "BELOW")
                        else None,
                    )
                    if settled_bet is not None:
                        notifier.bet_settled(settled_bet)
                    vault_event = book.maybe_vault_profits()
                    if vault_event is not None:
                        notifier.vault_withdrawal(vault_event)
                    stats = book.get_performance_stats()
                    _print_window_performance(stats)
                    skip_summary = ", ".join(
                        f"{count}× {reason}"
                        for reason, count in sorted(
                            skip_counts.items(), key=lambda kv: -kv[1]
                        )
                    )
                    notifier.window_stats(stats, skips=skip_summary)
                    skip_counts.clear()
                    calibration.log_settle(
                        window_id=closed.window_id,
                        symbol=symbol,
                        strike=float(closed.strike) if closed.strike is not None else None,
                        outcome=outcome_side,
                        settlement_price=settle_price,
                        settlement_source=settle_source,
                    )
                    backup_now(database_url=settings.database_url)
                    last_backup_at = datetime.now(timezone.utc).timestamp()
                pending_settlements = still_pending

            # A resting order may have been hit since the last tick.
            if resting is not None and live_exec is not None:
                rest_fill = await asyncio.to_thread(live_exec.poll_resting, resting)
                if rest_fill is not None:
                    live_filled_windows.add(resting.window_id)
                    live_fill_count += 1
                    print(
                        f"[{_utcnow_label()}] LIVE RESTING FILLED "
                        f"{resting.advice_side} {rest_fill.contracts:g} @ "
                        f"{rest_fill.price_paid*100:.1f}¢ (cost ${rest_fill.cost:.2f})"
                    )
                    if (
                        resting_advice is not None
                        and resting_window is not None
                        and book.get_open_bet(resting.window_id) is None
                    ):
                        placed = book.place_bet(
                            resting_window,
                            resting_advice,
                            contract_price=rest_fill.price_paid,
                            stake_notional=rest_fill.contracts,
                        )
                        if placed is not None:
                            notifier.bet_placed(placed, reason=resting_advice.reason)
                            backup_now(database_url=settings.database_url)
                            last_backup_at = datetime.now(timezone.utc).timestamp()
                    resting = None
                    resting_advice = None
                    resting_window = None
                elif resting.is_expired() or resting.window_id != window.window_id:
                    await asyncio.to_thread(live_exec.cancel_resting, resting)
                    live_miss_count += 1
                    total_orders = live_fill_count + live_miss_count
                    print(
                        f"[{_utcnow_label()}] LIVE resting expired unfilled "
                        f"({resting.advice_side} @{resting.limit_price*100:.0f}¢) | "
                        f"fill rate {live_fill_count}/{total_orders} "
                        f"({live_fill_count / total_orders * 100:.0f}%)"
                    )
                    resting = None
                    resting_advice = None
                    resting_window = None

            if (
                AUTO_BET
                and advice.should_bet
                and resting is None
                and book.get_open_bet(window.window_id) is None
            ):
                print()
                # Optional live rehearsal / (gated) live submit
                live_filled = False
                live_plan = None
                if live_exec is not None and advice.entry_share_price is not None:
                    ticker = kalshi_market_ticker.strip()
                    attempts = live_attempts.get(window.window_id, 0)
                    if not ticker:
                        note_skip("no ticker")
                        print(f"[{_utcnow_label()}] LIVE skip — no market ticker yet")
                    elif window.window_id in live_filled_windows:
                        note_skip("already holding")
                    elif attempts >= LIVE_MAX_ATTEMPTS:
                        note_skip("attempts used")
                    else:
                        # Authoritative check: a restart must not double a live bet
                        held = (
                            live_exec.get_position_count(ticker)
                            if LIVE_TRADING
                            else 0.0
                        )
                        if held is None:
                            note_skip("position check failed")
                            print(
                                f"[{_utcnow_label()}] LIVE skip — could not verify "
                                f"position on {ticker}"
                            )
                        elif held > 0:
                            note_skip("already holding")
                            live_filled_windows.add(window.window_id)
                            print(
                                f"[{_utcnow_label()}] LIVE skip — already holding "
                                f"{held:g} on {ticker}"
                            )
                        else:
                            ask_now = float(advice.entry_share_price)
                            model_now = float(
                                advice.prob_above
                                if advice.action == "ABOVE"
                                else advice.prob_below
                            )
                            # The snapshot ask is derived and often stale. Price
                            # off the real book instead, fetched right now.
                            book_now = await asyncio.to_thread(
                                fetch_orderbook, ticker
                            )
                            depth_note = ""
                            send_order = True
                            if book_now is not None:
                                true_ask = book_now.ask_for(advice.action)
                                fresh_edge = (
                                    model_now - true_ask if true_ask is not None else None
                                )
                                if true_ask is None:
                                    # Unreadable book is not evidence of a bad
                                    # price. Fall back to the snapshot rather
                                    # than refusing to trade at all.
                                    depth_note = " (book unreadable, using snapshot)"
                                elif fresh_edge is not None and fresh_edge < MIN_EDGE:
                                    send_order = False
                                    note_skip("edge gone on real book")
                                    print(
                                        f"[{_utcnow_label()}] LIVE skip — edge gone "
                                        f"at real ask {true_ask*100:.0f}¢ "
                                        f"(snapshot said {ask_now*100:.0f}¢, "
                                        f"edge {fresh_edge*100:+.1f}¢)"
                                    )
                                else:
                                    depth = book_now.depth_for(advice.action)
                                    depth_note = f" book {true_ask*100:.0f}¢ x{depth:g}"
                                    ask_now = true_ask

                            if not send_order:
                                # Deciding not to order is not an attempt. Only
                                # submitted orders count, or a few bad ticks
                                # would lock the bot out of the whole window.
                                pass
                            elif LIVE_ORDER_MODE == "maker":
                                # A maker order sits on the BID. Resting at the
                                # ask would cross, and post_only would reject it.
                                best_bid = (
                                    book_now.bid_for(advice.action)
                                    if book_now is not None
                                    else None
                                )
                                if best_bid is None:
                                    note_skip("no bid to join")
                                    print(
                                        f"[{_utcnow_label()}] LIVE skip — no "
                                        f"{advice.action} bid to join on {ticker}"
                                    )
                                    await asyncio.sleep(LOOP_INTERVAL_SECONDS)
                                    continue
                                # Improve on the best bid by a tick when the
                                # spread allows, but never reach the ask.
                                rest_price = min(
                                    best_bid + 0.01,
                                    ask_now - 0.01,
                                    model_now - MIN_EDGE,
                                )
                                rest_price = math.floor(rest_price * 100.0) / 100.0
                                if rest_price < 0.01:
                                    note_skip("no maker price with edge")
                                    print(
                                        f"[{_utcnow_label()}] LIVE skip — no maker "
                                        f"price leaves {MIN_EDGE*100:.0f}¢ edge "
                                        f"(bid {best_bid*100:.0f}¢ ask {ask_now*100:.0f}¢)"
                                    )
                                    await asyncio.sleep(LOOP_INTERVAL_SECONDS)
                                    continue
                                resting = await asyncio.to_thread(
                                    live_exec.place_resting,
                                    market_ticker=ticker,
                                    window_id=window.window_id,
                                    advice_side=advice.action,
                                    share_price=rest_price,
                                    rest_seconds=LIVE_REST_SECONDS,
                                )
                                live_attempts[window.window_id] = attempts + 1
                                if resting is None:
                                    print(
                                        f"[{_utcnow_label()}] LIVE rest rejected "
                                        f"{advice.action} @{rest_price*100:.0f}¢ "
                                        f"{ticker}{depth_note}"
                                    )
                                else:
                                    resting_advice = advice
                                    resting_window = window
                                    print(
                                        f"[{_utcnow_label()}] LIVE RESTING "
                                        f"{advice.action} @{rest_price*100:.0f}¢ "
                                        f"x{resting.contracts:g} {ticker} "
                                        f"for {LIVE_REST_SECONDS:.0f}s "
                                        f"(try {attempts + 1}/{LIVE_MAX_ATTEMPTS})"
                                        f"{depth_note}"
                                    )
                            else:
                                bid_price = _limit_price_with_tolerance(
                                    ask=ask_now, model_prob=model_now
                                )
                                live_plan = live_exec.execute(
                                    live_exec.plan_order(
                                        market_ticker=ticker,
                                        advice_side=advice.action,  # type: ignore[arg-type]
                                        share_price=bid_price,
                                    )
                                )
                                live_attempts[window.window_id] = attempts + 1
                                note = "ORDER" if live_plan.submitted else "DRY-RUN"
                                pad = ""
                                if bid_price > ask_now:
                                    pad = (
                                        f" (ask {ask_now*100:.0f}¢ "
                                        f"+{(bid_price-ask_now)*100:.0f}¢)"
                                    )
                                print(
                                    f"[{_utcnow_label()}] LIVE {note} {live_plan.advice_side} "
                                    f"{live_plan.book_side} @{live_plan.yes_book_price*100:.1f}¢ "
                                    f"x{live_plan.contracts:.2f} {live_plan.market_ticker} "
                                    f"(try {attempts + 1}/{LIVE_MAX_ATTEMPTS}){pad}{depth_note}"
                                    + (f" err={live_plan.error}" if live_plan.error else "")
                                )
                                notifier.live_order_plan(live_plan, note=note)
                                live_filled = bool(live_plan.submitted and live_plan.filled)
                                if live_plan.submitted:
                                    if live_filled:
                                        live_fill_count += 1
                                    else:
                                        live_miss_count += 1
                                    total_orders = live_fill_count + live_miss_count
                                    print(
                                        f"[{_utcnow_label()}] LIVE fill rate "
                                        f"{live_fill_count}/{total_orders} "
                                        f"({live_fill_count / total_orders * 100:.0f}%)"
                                    )
                                if live_filled:
                                    live_filled_windows.add(window.window_id)
                # The book mirrors whatever actually happened: the paper
                # assumption when papering, the real fill when live. Without
                # this, live mode would track no W/L, P/L, settlement or vault.
                record_price: Optional[float] = advice.entry_share_price
                record_qty: float = STAKE_NOTIONAL
                if LIVE_TRADING:
                    # Only a real fill becomes a position; a miss is not a bet.
                    if live_filled and live_plan is not None:
                        record_price = live_plan.effective_price
                        record_qty = float(live_plan.fill_count)
                        # A fill should cost roughly what we bid. A large gap
                        # means the price was read off the wrong leg.
                        intended = float(live_plan.share_price)
                        if (
                            record_price is not None
                            and abs(record_price - intended) > 0.10
                        ):
                            print(
                                f"[{_utcnow_label()}] WARNING: booked "
                                f"{record_price*100:.1f}¢ for a {intended*100:.1f}¢ "
                                f"{live_plan.advice_side} order — not recording. "
                                "Check fill-price handling."
                            )
                            record_price = None
                    else:
                        record_price = None

                if record_price is not None and record_qty > 0:
                    placed = book.place_bet(
                        window,
                        advice,
                        contract_price=record_price,
                        stake_notional=record_qty,
                    )
                    if placed is not None:
                        notifier.bet_placed(placed, reason=advice.reason)
                        backup_now(database_url=settings.database_url)
                        last_backup_at = datetime.now(timezone.utc).timestamp()

            status_mkt = yes_ask if yes_ask is not None else market_prob_above
            _print_status(
                price=price,
                strike=float(window.strike),
                strike_source=getattr(window, "strike_source", "auto"),
                remaining=window.seconds_remaining(),
                p_above=advice.prob_above,
                p_below=advice.prob_below,
                action=advice.action,
                edge=advice.edge,
                bankroll=book.get_balance(),
                market_prob=status_mkt if status_mkt is not None else 0.0,
                spot_source=spot_source,
            )

            if (now_ts - last_backup_at) >= BACKUP_EVERY_SECONDS:
                backup_now(database_url=settings.database_url)
                last_backup_at = now_ts

            if (
                notifier.active
                and HEARTBEAT_EVERY_SECONDS > 0
                and (now_ts - last_heartbeat_at) >= HEARTBEAT_EVERY_SECONDS
            ):
                stats_hb = book.get_performance_stats()
                # In live mode the book is a mirror of Kalshi; show both so any
                # drift between them is visible instead of silent.
                extras = ""
                if tape is not None:
                    age = tape.age_seconds
                    age_txt = f"{age:.1f}s" if age is not None else "n/a"
                    extras += f"Spot {tape.transport} {age_txt} | "
                if LIVE_TRADING and live_exec is not None and live_exec.client is not None:
                    try:
                        balance = live_exec.client.get_balance().balance_usd
                        extras += f"Kalshi ${balance:,.2f} | "
                    except KalshiAuthError as exc:
                        logger.warning("Heartbeat balance check failed: %s", exc)
                    orders_sent = live_fill_count + live_miss_count
                    if orders_sent:
                        extras += (
                            f"Fills {live_fill_count}/{orders_sent} "
                            f"({live_fill_count / orders_sent * 100:.0f}%) | "
                        )
                notifier.info(
                    f"HEARTBEAT alive | BTC ${price:,.2f} | "
                    f"{advice.action} edge {advice.edge*100:+.1f}¢ | "
                    f"{extras}"
                    f"Bank ${stats_hb['usd_balance']:,.2f} | "
                    f"Vault ${float(stats_hb.get('vaulted_usd') or 0):,.2f} | "
                    f"{stats_hb['win_count']}W/{stats_hb['loss_count']}L | "
                    f"T-{_fmt_mmss(window.seconds_remaining())}"
                )
                last_heartbeat_at = now_ts

            await asyncio.sleep(LOOP_INTERVAL_SECONDS)
    finally:
        if tape is not None:
            await tape.stop()
        if resting is not None and live_exec is not None:
            # Don't leave a live order on the book after the process stops
            try:
                await asyncio.to_thread(live_exec.cancel_resting, resting)
                print(f"[{_utcnow_label()}] Cancelled resting order on shutdown")
            except Exception as exc:  # noqa: BLE001 — shutdown must not raise
                logger.warning("Could not cancel resting order: %s", exc)
        await close_exchange(exchange)
        backup_now(database_url=settings.database_url)
        final_stats = book.get_performance_stats()
        _print_performance(final_stats, kalshi_event=kalshi_event)
        if notifier.active:
            notifier.window_stats(final_stats)
            notifier.info(
                "Bot process stopped (Render redeploy/restart/crash). "
                "If hosted with auto-restart it should come back in a few seconds."
            )


def main(argv: Optional[list[str]] = None) -> int:
    log_path = setup_bot_logging()
    os.environ["_BOT_LOG_PATH"] = str(log_path.resolve())
    logging.getLogger("data.feed").setLevel(logging.WARNING)
    logging.getLogger("data.kalshi").setLevel(logging.WARNING)

    # Hard refuse real live orders before paper week ends (and without confirm phrase)
    enforce_live_gate()

    args = _parse_args(argv)
    strike = _parse_number(args.strike) if args.strike else None
    # Prefer explicit CLI; else env MARKET_CENTS if set
    market_arg = args.market_cents
    if market_arg is None:
        market_arg = os.getenv("MARKET_CENTS")
    market_raw = _parse_number(market_arg) if market_arg else None
    market_prob = _normalize_market_prob(market_raw)
    use_kalshi = (STRIKE_SOURCE in {"kalshi", "auto"}) and not args.no_kalshi
    try:
        asyncio.run(
            run_bot(
                initial_strike=strike,
                initial_market_prob=market_prob,
                use_kalshi=use_kalshi,
                reset_paper=bool(args.reset_paper),
            )
        )
    except KeyboardInterrupt:
        print()
        return 0
    finally:
        shutdown_bot_logging()
    return 0


if __name__ == "__main__":
    sys.exit(main())
