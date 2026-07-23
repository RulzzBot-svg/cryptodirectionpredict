#!/usr/bin/env python3
"""
BTC 15-minute prediction-market edge bot.

Estimates P(finish ABOVE strike) for each wall-clock 15m window, recommends
ABOVE / BELOW / SKIP, and optionally papers the bet against a 50/50 book.

Manual Robinhood strike override
--------------------------------
Pass at start:
  python main.py --strike 64737.27 --market-cents 55

Or while running, in another terminal:
  echo 64737.27 > manual_strike.txt
  echo 55 > market_cents.txt
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
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
logging.getLogger("data.feed").setLevel(logging.WARNING)

logger = logging.getLogger("main")

LOOP_INTERVAL_SECONDS = float(os.getenv("LOOP_INTERVAL_SECONDS", "10"))
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.08"))
CONTRACT_COST = float(os.getenv("CONTRACT_COST", "0.50"))
AUTO_BET = os.getenv("AUTO_BET", "true").strip().lower() in {"1", "true", "yes", "on"}
MIN_SECONDS_TO_BET = float(os.getenv("MIN_SECONDS_TO_BET", "20"))

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
) -> None:
    src = "RH" if strike_source == "manual" else "auto"
    line = (
        f"[{_utcnow_label()}] "
        f"BTC ${price:,.2f} | "
        f"Strike ${strike:,.2f} ({src}) | "
        f"T-{_fmt_mmss(remaining)} | "
        f"Above {p_above * 100:5.2f}% | "
        f"Below {p_below * 100:5.2f}% | "
        f"Mkt {market_prob * 100:4.1f}¢ | "
        f"Edge {edge * 100:+5.1f}¢ | "
        f"{action:<5} | "
        f"Bank ${bankroll:,.2f}"
    )
    print(f"\r{line:<150}", end="", flush=True)


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


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BTC 15m prediction-market edge bot",
    )
    parser.add_argument(
        "--strike",
        type=str,
        default=os.getenv("MANUAL_STRIKE"),
        help="Robinhood/Kalshi strike for the current window (e.g. 64737.27)",
    )
    parser.add_argument(
        "--market-cents",
        type=str,
        default=os.getenv("MARKET_CENTS", os.getenv("MARKET_PROB_ABOVE")),
        help="Robinhood YES price in cents (e.g. 55) or probability (e.g. 0.55)",
    )
    return parser.parse_args(argv)


def _normalize_market_prob(value: Optional[float]) -> float:
    if value is None:
        return float(os.getenv("MARKET_PROB_ABOVE", "0.50"))
    # Allow either 55 (cents) or 0.55 (probability)
    if value > 1.0:
        return max(0.0, min(1.0, value / 100.0))
    return max(0.0, min(1.0, value))


async def run_bot(
    *,
    initial_strike: Optional[float] = None,
    initial_market_prob: float = 0.50,
) -> None:
    settings = load_settings()
    symbol = settings.symbol
    provider = settings.data_provider
    market_prob_above = initial_market_prob

    print("=" * 60)
    print("  BTC 15m PREDICTION EDGE BOT")
    print("=" * 60)
    print(f"  Symbol          : {symbol}")
    print(f"  Provider        : {provider}")
    print(f"  Candle TF       : {TIMEFRAME}")
    print(f"  Loop interval   : {LOOP_INTERVAL_SECONDS:.0f}s")
    print(f"  Min edge        : {MIN_EDGE * 100:.0f}¢")
    print(f"  Market YES      : {market_prob_above * 100:.1f}¢")
    print(f"  Contract        : ${CONTRACT_COST:.2f} → $1.00 payout")
    print(f"  Auto-bet        : {'ON' if AUTO_BET else 'OFF (advice only)'}")
    if initial_strike:
        print(f"  Manual strike   : ${initial_strike:,.2f}")
    else:
        print("  Manual strike   : (auto — set with --strike or manual_strike.txt)")
    print(f"  Strike file     : {STRIKE_FILE}")
    print(f"  Market file     : {MARKET_CENTS_FILE}")
    print(f"  Database        : {settings.database_url}")
    print(f"  Started         : {_utcnow_label()}")
    print("=" * 60)
    print("  Tip: echo 64737.27 > manual_strike.txt   # Robinhood strike")
    print("       echo 55 > market_cents.txt          # Robinhood YES cents")
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
        market_prob_above=market_prob_above,
        min_seconds_to_bet=MIN_SECONDS_TO_BET,
    )

    exchange: Any = None
    consecutive_errors = 0
    last_announced_strike: Optional[float] = None
    pending_manual_strike = initial_strike

    try:
        exchange = create_rest_exchange(provider)
        while True:
            # Hot-reload overrides from files (edit anytime while bot runs)
            file_strike = _read_number_file(STRIKE_FILE)
            if file_strike is not None:
                pending_manual_strike = file_strike

            file_mkt = _read_number_file(MARKET_CENTS_FILE)
            if file_mkt is not None:
                market_prob_above = _normalize_market_prob(file_mkt)

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
                # Manual strike from CLI/env applies to the first window only unless
                # manual_strike.txt keeps getting updated for later windows.
                if not STRIKE_FILE.exists():
                    pending_manual_strike = None
                last_announced_strike = None

            if pending_manual_strike is not None:
                changed = windows.apply_manual_strike(pending_manual_strike)
                if changed and (
                    last_announced_strike is None
                    or abs(last_announced_strike - pending_manual_strike) > 1e-9
                ):
                    print()
                    print(
                        f"[{_utcnow_label()}] Manual strike set to "
                        f"${pending_manual_strike:,.2f} "
                        f"(Robinhood/Kalshi override)"
                    )
                    last_announced_strike = pending_manual_strike

            if window.strike is None:
                await asyncio.sleep(LOOP_INTERVAL_SECONDS)
                continue

            advice = advisor.advise(
                window,
                price,
                snapshot["candles"],
                market_prob_above=market_prob_above,
            )

            if AUTO_BET and advice.should_bet and book.get_open_bet(window.window_id) is None:
                print()
                book.place_bet(
                    window,
                    advice,
                    market_prob_above=market_prob_above,
                )

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
                market_prob=market_prob_above,
            )

            await asyncio.sleep(LOOP_INTERVAL_SECONDS)
    finally:
        await close_exchange(exchange)
        _print_performance(book.get_performance_stats())


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    strike = _parse_number(args.strike) if args.strike else None
    market_raw = _parse_number(args.market_cents) if args.market_cents else None
    market_prob = _normalize_market_prob(market_raw)
    try:
        asyncio.run(
            run_bot(
                initial_strike=strike,
                initial_market_prob=market_prob,
            )
        )
    except KeyboardInterrupt:
        print()
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
