#!/usr/bin/env python3
"""
Paper BTC spot bot (not the Kalshi 15m predictor).

Long-only Coinbase BTC/USD on 1h EMA crossover, with taker fees and a stop.
LIVE_CRYPTO is hard-off in v1 — this learns fills/P/L plumbing on paper first.

  python crypto_bot.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

from dotenv import load_dotenv

from config.bot_logging import setup_bot_logging, shutdown_bot_logging
from config.settings import load_settings
from data.backup import backup_now, restore_latest_backup
from execution.crypto_risk import (
    assert_crypto_paper_ready,
    day_buy_halted,
    decide_crypto_action,
    format_crypto_digest,
    stop_hit,
)
from execution.digest import digest_due, digest_stamp_path, read_stamp, write_stamp
from execution.paper_engine import PaperBroker
from notifications import TelegramNotifier
from strategies import EMACrossoverStrategy

load_dotenv()


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _utcnow_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


async def run_crypto_paper(
    *,
    database_url: str,
    symbol: str,
    provider: str,
    timeframe: str,
    loop_seconds: float,
    fee_rate: float,
    stop_pct: float,
    position_pct: float,
    day_loss: float,
    tz_name: str,
    initial_balance: float,
    ohlcv_limit: int,
    digest_hour: int,
    heartbeat_seconds: float,
    backup_seconds: float,
    reset_paper: bool,
) -> None:
    assert_crypto_paper_ready(database_url, live_crypto=_flag("LIVE_CRYPTO"))
    restore_latest_backup(database_url=database_url)

    notifier = TelegramNotifier()
    notifier.prefix = "CRYPTO PAPER"
    strategy = EMACrossoverStrategy()
    broker = PaperBroker.from_database_url(
        database_url,
        initial_balance=initial_balance,
        symbol=symbol,
        fee_rate=fee_rate,
    )

    print("=" * 60)
    print("  CRYPTO VENTURE — BTC SPOT PAPER")
    print("=" * 60)
    print(f"  Symbol       : {symbol}")
    print(f"  Provider     : {provider}")
    print(f"  Timeframe    : {timeframe}")
    print(f"  Strategy     : {strategy.name} (not a proven edge — paper only)")
    print(f"  Fee          : {fee_rate * 100:.2f}% taker (both sides)")
    print(f"  Stop         : {stop_pct * 100:.1f}% from all-in entry")
    print(f"  Size         : {position_pct * 100:.0f}% of USD, one position")
    print(f"  Day buy-halt : -${day_loss:g} realized ({tz_name})")
    print(f"  Live         : LOCKED (LIVE_CRYPTO must stay false)")
    print(f"  Database     : {database_url}")
    print(f"  Started      : {_utcnow_label()}")
    print("=" * 60)

    if reset_paper:
        broker.reset_paper_history(balance=initial_balance)

    stats0 = broker.get_performance_stats()
    notifier.info(
        f"CRYPTO PAPER START | {symbol} {timeframe} | "
        f"bank ${stats0['usd_balance']:,.2f} | fee {fee_rate*100:.2f}% | "
        f"stop {stop_pct*100:.1f}% | live locked",
        important=True,
    )
    backup_now(database_url=database_url)

    from data.feed import fetch_latest_snapshot

    halted_note_sent = False
    last_heartbeat = 0.0
    last_backup = 0.0
    try:
        while True:
            snap = await fetch_latest_snapshot(
                symbol,
                timeframe,
                provider=provider,
                ohlcv_limit=ohlcv_limit,
            )
            price = float(snap.get("last_price") or 0.0)
            candles = snap.get("candles")
            if price <= 0 or candles is None or getattr(candles, "empty", True):
                print(f"\r[{_utcnow_label()}] waiting for {provider} {symbol}   ", end="")
                await asyncio.sleep(loop_seconds)
                continue

            signal = strategy.generate_signal(candles)
            pf = broker.get_portfolio()
            in_pos = float(pf["btc_balance"]) > 1e-10
            entry = float(pf["avg_entry_price"] or 0.0)
            trades = broker.get_trades()
            buy_halted = day_buy_halted(
                trades, day_loss=day_loss, tz_name=tz_name
            )
            if buy_halted and not halted_note_sent:
                notifier.info(
                    f"CRYPTO buy-halt — realized day loss hit -${day_loss:g}. "
                    "Stops/sells still run; new buys wait until local midnight.",
                    important=True,
                )
                halted_note_sent = True
            if not buy_halted:
                halted_note_sent = False

            action = decide_crypto_action(
                signal=signal,
                in_position=in_pos,
                entry=entry,
                price=price,
                stop_pct=stop_pct,
                buy_halted=buy_halted,
            )
            trade = None
            if action in {"BUY", "SELL"}:
                trade = broker.execute_order(
                    action, price, position_size_pct=position_pct
                )
                if trade is not None:
                    pnl = float(trade.realized_pnl or 0.0)
                    extra = ""
                    if action == "SELL" and in_pos and stop_hit(entry, price, stop_pct):
                        extra = " STOP"
                    notifier.info(
                        f"{action}{extra} {symbol} @ ${price:,.2f}\n"
                        f"USD ${float(trade.usd_balance_after):,.2f} | "
                        f"BTC {float(trade.btc_balance_after):.8f}"
                        + (f"\nRealized ${pnl:+,.2f}" if action == "SELL" else ""),
                        important=True,
                    )

            equity = float(pf["usd_balance"]) + float(pf["btc_balance"]) * price
            if trade is not None:
                pf = broker.get_portfolio()
                equity = float(pf["usd_balance"]) + float(pf["btc_balance"]) * price
            pos = "LONG" if float(pf["btc_balance"]) > 1e-10 else "FLAT"
            halt = " HALT" if buy_halted else ""
            print(
                f"\r[{_utcnow_label()}] {symbol} ${price:,.2f} | {timeframe} | "
                f"{signal:4s} → {action:4s} | {pos}{halt} | "
                f"eq ${equity:,.2f}   ",
                end="",
                flush=True,
            )

            now_ts = time.time()
            if heartbeat_seconds > 0 and (now_ts - last_heartbeat) >= heartbeat_seconds:
                last_heartbeat = now_ts
                notifier.info(
                    f"CRYPTO heartbeat | {symbol} ${price:,.2f} | {pos} | "
                    f"eq ${equity:,.2f}{halt}",
                )
            if backup_seconds > 0 and (now_ts - last_backup) >= backup_seconds:
                last_backup = now_ts
                backup_now(database_url=database_url)
            stamp = digest_stamp_path(database_url)
            if notifier.active and digest_due(
                tz_name=tz_name,
                hour=digest_hour,
                last_sent=read_stamp(stamp),
            ):
                stats = broker.get_performance_stats(current_price=price)
                local_day = (
                    datetime.now(timezone.utc)
                    .astimezone(ZoneInfo(tz_name))
                    .date()
                    .isoformat()
                )
                text = format_crypto_digest(stats, local_day=local_day, symbol=symbol)
                print(f"\n[{_utcnow_label()}] {text.replace(chr(10), ' | ')}")
                if notifier.daily_digest(text):
                    write_stamp(stamp, datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name)).date())

            await asyncio.sleep(loop_seconds)
    except KeyboardInterrupt:
        print()
        stats = broker.get_performance_stats(current_price=price if "price" in locals() else None)
        print(
            f"[{_utcnow_label()}] stopped | equity ${stats['equity']:,.2f} | "
            f"realized ${stats['realized_pnl']:+,.2f} | "
            f"{stats['win_count']}W/{stats['loss_count']}L"
        )
        notifier.info(
            f"CRYPTO PAPER STOP | eq ${stats['equity']:,.2f} | "
            f"realized ${stats['realized_pnl']:+,.2f} | "
            f"{stats['win_count']}W/{stats['loss_count']}L",
            important=True,
        )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paper BTC spot bot")
    parser.add_argument(
        "--database-url",
        default=os.getenv("CRYPTO_DATABASE_URL", "sqlite:///./crypto_paper.db"),
    )
    parser.add_argument(
        "--reset-paper",
        action="store_true",
        help="Wipe crypto paper trades and reset the paper bank",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    setup_bot_logging()
    args = parse_args(argv)
    settings = load_settings()
    try:
        asyncio.run(
            run_crypto_paper(
                database_url=args.database_url,
                symbol=os.getenv("CRYPTO_SYMBOL", settings.symbol or "BTC/USD"),
                provider=os.getenv("CRYPTO_DATA_PROVIDER", settings.data_provider or "coinbase"),
                timeframe=os.getenv("CRYPTO_TIMEFRAME", "1h"),
                loop_seconds=float(os.getenv("CRYPTO_LOOP_SECONDS", "60")),
                fee_rate=float(os.getenv("CRYPTO_FEE_RATE", "0.006")),
                stop_pct=float(os.getenv("CRYPTO_STOP_PCT", "0.03")),
                position_pct=float(os.getenv("CRYPTO_POSITION_PCT", "0.25")),
                day_loss=float(os.getenv("CRYPTO_DAY_LOSS", "10")),
                tz_name=os.getenv("CRYPTO_TZ", "America/Los_Angeles"),
                initial_balance=float(
                    os.getenv("CRYPTO_PAPER_BALANCE", str(settings.paper_initial_balance))
                ),
                ohlcv_limit=int(float(os.getenv("CRYPTO_OHLCV_LIMIT", "80"))),
                digest_hour=int(float(os.getenv("DIGEST_HOUR", "7"))),
                heartbeat_seconds=float(os.getenv("HEARTBEAT_EVERY_SECONDS", "900")),
                backup_seconds=float(os.getenv("BACKUP_EVERY_SECONDS", "300")),
                reset_paper=bool(args.reset_paper) or _flag("RESET_PAPER_HISTORY"),
            )
        )
    finally:
        shutdown_bot_logging()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"crypto_bot failed: {exc}", file=sys.stderr)
        raise
