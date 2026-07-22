#!/usr/bin/env python3
"""
BTC 15-minute prediction-market edge bot.

Estimates P(finish ABOVE strike) for each wall-clock 15m window, recommends
ABOVE / BELOW / SKIP, and optionally papers the bet against a 50/50 book.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv

from config.settings import load_settings
from data.feed import close_exchange, create_rest_exchange, fetch_latest_snapshot
from execution.prediction_book import PredictionBook
from models.db import create_db_engine, create_session_factory, init_db
from prediction.advisor import PredictionAdvisor
from prediction.window import WindowManager

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
# Keep the live status line readable
logging.getLogger("data.feed").setLevel(logging.WARNING)

logger = logging.getLogger("main")

LOOP_INTERVAL_SECONDS = float(os.getenv("LOOP_INTERVAL_SECONDS", "10"))
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.08"))
MARKET_PROB_ABOVE = float(os.getenv("MARKET_PROB_ABOVE", "0.50"))
CONTRACT_COST = float(os.getenv("CONTRACT_COST", "0.50"))
AUTO_BET = os.getenv("AUTO_BET", "true").strip().lower() in {"1", "true", "yes", "on"}
MIN_SECONDS_TO_BET = float(os.getenv("MIN_SECONDS_TO_BET", "20"))


def _utcnow_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _print_status(
    *,
    price: float,
    strike: float,
    remaining: float,
    p_above: float,
    p_below: float,
    action: str,
    edge: float,
    bankroll: float,
) -> None:
    line = (
        f"[{_utcnow_label()}] "
        f"BTC ${price:,.2f} | "
        f"Strike ${strike:,.2f} | "
        f"T-{_fmt_mmss(remaining)} | "
        f"Above {p_above * 100:5.2f}% | "
        f"Below {p_below * 100:5.2f}% | "
        f"Edge {edge * 100:+5.1f}¢ | "
        f"{action:<5} | "
        f"Bank ${bankroll:,.2f}"
    )
    print(f"\r{line:<140}", end="", flush=True)


def _print_performance(stats: dict[str, Any]) -> None:
    print()
    print("=" * 60)
    print("  PREDICTION MARKET — FINAL PERFORMANCE")
    print("=" * 60)
    print(f"  Time            : {_utcnow_label()}")
    print(f"  Starting bank   : ${stats['starting_balance']:,.2f}")
    print(f"  Cash bankroll   : ${stats['usd_balance']:,.2f}")
    print(f"  Equity          : ${stats['equity']:,.2f}")
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


async def run_bot() -> None:
    settings = load_settings()
    symbol = settings.symbol
    provider = settings.data_provider

    print("=" * 60)
    print("  BTC 15m PREDICTION EDGE BOT")
    print("=" * 60)
    print(f"  Symbol          : {symbol}")
    print(f"  Provider        : {provider}")
    print(f"  Candle TF       : {TIMEFRAME}")
    print(f"  Loop interval   : {LOOP_INTERVAL_SECONDS:.0f}s")
    print(f"  Min edge        : {MIN_EDGE * 100:.0f}¢ vs market {MARKET_PROB_ABOVE * 100:.0f}¢")
    print(f"  Contract        : ${CONTRACT_COST:.2f} → $1.00 payout")
    print(f"  Auto-bet        : {'ON' if AUTO_BET else 'OFF (advice only)'}")
    print(f"  Database        : {settings.database_url}")
    print(f"  Started         : {_utcnow_label()}")
    print("=" * 60)
    print("  Reads: % chance BTC finishes ABOVE the window strike.")
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
        contract_cost=CONTRACT_COST,
        engine=engine,
    )
    windows = WindowManager(window_minutes=15)
    advisor = PredictionAdvisor(
        min_edge=MIN_EDGE,
        market_prob_above=MARKET_PROB_ABOVE,
        min_seconds_to_bet=MIN_SECONDS_TO_BET,
    )

    exchange: Any = None
    consecutive_errors = 0

    try:
        exchange = create_rest_exchange(provider)
        while True:
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

            window, expired = windows.update(price, strike_price=strike_hint)
            if expired is not None:
                print()
                print(
                    f"[{_utcnow_label()}] Window {expired.window_id} settled "
                    f"{expired.outcome} @ ${float(expired.settlement_price):,.2f} "
                    f"(strike ${float(expired.strike):,.2f})"
                )
                book.settle_window(expired, float(expired.settlement_price or price))

            if window.strike is None:
                await asyncio.sleep(LOOP_INTERVAL_SECONDS)
                continue

            advice = advisor.advise(
                window,
                price,
                snapshot["candles"],
                market_prob_above=MARKET_PROB_ABOVE,
            )

            if AUTO_BET and advice.should_bet and book.get_open_bet(window.window_id) is None:
                print()
                book.place_bet(
                    window,
                    advice,
                    market_prob_above=MARKET_PROB_ABOVE,
                )

            _print_status(
                price=price,
                strike=float(window.strike),
                remaining=window.seconds_remaining(),
                p_above=advice.prob_above,
                p_below=advice.prob_below,
                action=advice.action,
                edge=advice.edge,
                bankroll=book.get_balance(),
            )

            await asyncio.sleep(LOOP_INTERVAL_SECONDS)
    finally:
        # Settle the active window mark-to-market if a bet is open
        if windows.current is not None and windows.current.strike is not None:
            open_bet = book.get_open_bet(windows.current.window_id)
            # leave OPEN bets open on shutdown (capital already deducted);
            # performance equity credits premium back.
            _ = open_bet
        await close_exchange(exchange)
        _print_performance(book.get_performance_stats())


def main() -> int:
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print()
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
