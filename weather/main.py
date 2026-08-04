#!/usr/bin/env python3
"""Weather Kalshi paper bot — NYC (etc.) daily high temperature markets."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Allow `python main.py` from the weather/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from advisor import advise
from config import load_settings
from forecast import fetch_ensemble_high
from markets import fetch_open_weather_markets, resolve_target_date
from paper_book import PaperBook
from probability import build_temp_pmf, score_contracts


def _pct(x: float) -> str:
    return f"{100.0 * x:.1f}%"


def run_once(settings, book: PaperBook, *, verbose: bool = True) -> None:
    meta = settings.city_meta
    target = resolve_target_date(settings.target_date, meta["timezone"])

    forecast = fetch_ensemble_high(
        lat=meta["lat"],
        lon=meta["lon"],
        timezone=meta["timezone"],
        target=target,
    )
    pmf = build_temp_pmf(forecast.members_f)
    contracts = fetch_open_weather_markets(
        settings.kalshi_base,
        settings.kalshi_series,
        target,
    )
    scored = score_contracts(pmf, contracts)
    decision = advise(scored, min_edge=settings.min_edge)

    if verbose:
        print(
            f"\n{settings.city} {meta['station']} | high {target.isoformat()} | "
            f"ensemble mean {forecast.mean_f:.1f}°F "
            f"[{forecast.min_f:.1f}, {forecast.max_f:.1f}] "
            f"n={len(forecast.members_f)} | bank ${book.bankroll:,.2f}"
        )
        print(f"Kalshi open contracts: {len(contracts)} ({settings.kalshi_series})")
        for row in scored[:8]:
            marker = "<--" if decision.pick and row.ticker == decision.pick.ticker else ""
            print(
                f"  {row.label:<16} model {_pct(row.model_yes):>6}  "
                f"mkt {_pct(row.market_yes):>6}  edge {row.edge*100:+5.1f}¢  "
                f"{row.ticker} {marker}"
            )
        print(f"Advice: {decision.action} — {decision.reason}")

    if (
        settings.auto_bet
        and decision.action == "BUY_YES"
        and decision.pick is not None
    ):
        # Use ask as conservative entry if available via market_yes proxy;
        # paper fill at market_yes (mid). Live trading should use ask.
        price = max(decision.pick.market_yes, 0.01)
        fill = book.buy_yes(
            ticker=decision.pick.ticker,
            label=decision.pick.label,
            price=price,
            notional=settings.stake_notional,
            model_yes=decision.pick.model_yes,
            edge=decision.pick.edge,
        )
        if fill and verbose:
            print(
                f"PAPER BUY YES {fill.contracts:.2f} @ {fill.price:.3f} "
                f"on {fill.ticker} ({fill.label})"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Weather Kalshi paper bot")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan and exit",
    )
    args = parser.parse_args()

    settings = load_settings()
    book = PaperBook(bankroll=settings.paper_bankroll)

    print(
        f"Weather paper bot | city={settings.city} series={settings.kalshi_series} "
        f"target={settings.target_date} min_edge={settings.min_edge:.0%} "
        f"stake=${settings.stake_notional:g}"
    )

    if args.once:
        run_once(settings, book)
        return

    try:
        while True:
            try:
                run_once(settings, book)
            except Exception as exc:  # keep loop alive on transient API errors
                print(f"ERROR: {exc}")
            time.sleep(settings.loop_interval_seconds)
    except KeyboardInterrupt:
        print("\nStopped.")
        print(f"Bankroll: ${book.bankroll:,.2f}")
        print(f"Fills: {len(book.fills)}")
        for f in book.fills:
            print(
                f"  {f.ts} YES {f.contracts:.2f}@${f.price:.3f} "
                f"edge={f.edge:+.1%} {f.ticker}"
            )


if __name__ == "__main__":
    main()
