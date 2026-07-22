#!/usr/bin/env python3
"""Main entrypoint: wire market data, strategy, and paper execution."""

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
from execution.paper_engine import PaperBroker
from models.db import create_db_engine, create_session_factory, init_db
from strategies.momentum_strategy import EMACrossoverStrategy

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")

LOOP_INTERVAL_SECONDS = float(os.getenv("LOOP_INTERVAL_SECONDS", "10"))
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
POSITION_SIZE_PCT = float(os.getenv("POSITION_SIZE_PCT", "0.25"))


def _utcnow_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _print_status(
    *,
    price: float,
    usd: float,
    btc: float,
    equity: float,
    signal: str,
) -> None:
    """Overwrite a single live status line in the terminal."""
    line = (
        f"[{_utcnow_label()}] "
        f"Current BTC Price ${price:,.2f} | "
        f"USD Balance ${usd:,.2f} | "
        f"BTC Balance {btc:.8f} | "
        f"Total Equity ${equity:,.2f} | "
        f"Active Signal {signal:<4}"
    )
    print(f"\r{line:<120}", end="", flush=True)


def _print_performance(stats: dict[str, Any]) -> None:
    print()
    print("=" * 60)
    print("  FINAL PERFORMANCE SUMMARY")
    print("=" * 60)
    print(f"  Time            : {_utcnow_label()}")
    print(f"  Starting USD    : ${stats['starting_balance']:,.2f}")
    print(f"  Ending USD      : ${stats['usd_balance']:,.2f}")
    print(f"  Ending BTC      : {stats['btc_balance']:.8f}")
    if stats["current_price"]:
        print(f"  Last BTC price  : ${stats['current_price']:,.2f}")
    print(f"  Total equity    : ${stats['equity']:,.2f}")
    total_pnl = stats["total_pnl"]
    pnl_label = f"+${total_pnl:,.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):,.2f}"
    print(f"  Total P/L       : {pnl_label} ({stats['total_return_pct']:+.2f}%)")
    realized = stats["realized_pnl"]
    unrealized = stats["unrealized_pnl"]
    print(
        f"  Realized P/L    : "
        f"{'+' if realized >= 0 else '-'}${abs(realized):,.2f}"
    )
    print(
        f"  Unrealized P/L  : "
        f"{'+' if unrealized >= 0 else '-'}${abs(unrealized):,.2f}"
    )
    print(
        f"  Trades          : {stats['trade_count']} "
        f"({stats['buy_count']} buys / {stats['sell_count']} sells)"
    )
    if stats["sell_count"]:
        print(
            f"  Win rate        : {stats['win_rate_pct']:.1f}% "
            f"({stats['win_count']}W / {stats['loss_count']}L)"
        )
    print("=" * 60)


async def run_bot() -> None:
    settings = load_settings()
    symbol = settings.symbol
    provider = settings.data_provider

    print("=" * 60)
    print("  BTC PAPER TRADING BOT")
    print("=" * 60)
    print(f"  Symbol          : {symbol}")
    print(f"  Timeframe       : {TIMEFRAME}")
    print(f"  Provider        : {provider}")
    print(f"  Loop interval   : {LOOP_INTERVAL_SECONDS:.0f}s")
    print(f"  Position size   : {POSITION_SIZE_PCT * 100:.0f}% of USD on BUY")
    print(f"  Database        : {settings.database_url}")
    print(f"  Started         : {_utcnow_label()}")
    print("=" * 60)
    print("  Press Ctrl+C to stop and print performance stats.")
    print("=" * 60)
    print()

    engine = create_db_engine(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    broker = PaperBroker(
        session_factory,
        initial_balance=settings.paper_initial_balance,
        symbol=symbol,
        engine=engine,
    )
    strategy = EMACrossoverStrategy()

    exchange: Any = None
    last_price: Optional[float] = None
    last_handled_candle: Any = None
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
            last_price = price
            candles = snapshot["candles"]
            signal = strategy.generate_signal(candles)

            candle_ts = candles.index[-1] if len(candles.index) else None
            if (
                signal in ("BUY", "SELL")
                and candle_ts is not None
                and candle_ts != last_handled_candle
            ):
                print()
                trade = broker.execute_order(
                    signal,
                    current_price=price,
                    position_size_pct=POSITION_SIZE_PCT,
                )
                if trade is not None:
                    last_handled_candle = candle_ts

            portfolio = broker.get_portfolio()
            usd = float(portfolio["usd_balance"])
            btc = float(portfolio["btc_balance"])
            equity = usd + btc * price
            _print_status(
                price=price,
                usd=usd,
                btc=btc,
                equity=equity,
                signal=signal,
            )

            await asyncio.sleep(LOOP_INTERVAL_SECONDS)
    finally:
        await close_exchange(exchange)
        stats = broker.get_performance_stats(current_price=last_price)
        _print_performance(stats)


def main() -> int:
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        # run_bot's finally prints stats and closes the exchange; newline after live status.
        print()
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
