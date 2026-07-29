#!/usr/bin/env python3
"""One-shot Kalshi order smoke test — verify plumbing, not run the strategy.

Two phases:

  1. REST/CANCEL (free): place a 1¢ resting bid that cannot fill, confirm an
     order_id comes back, then cancel it. Proves auth + create + cancel work.
  2. FILL (costs money, opt-in): buy a tiny marketable IOC order at the current
     ask and report what actually filled, at what price, and the fee.

This does NOT enable the trading bot. `LIVE_TRADING` is untouched.

Examples:
  python scripts/live_smoke_test.py                       # demo, cancel test only
  python scripts/live_smoke_test.py --env prod --i-understand-real-money
  python scripts/live_smoke_test.py --env prod --i-understand-real-money \
      --spend --contracts 1 --max-cost 1.00
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from data.kalshi import fetch_current_btc_15m  # noqa: E402
from data.kalshi_auth import (  # noqa: E402
    DEFAULT_DEMO_BASE,
    DEFAULT_PROD_BASE,
    KalshiAuthClient,
    KalshiAuthError,
    credentials_configured,
)
from execution.live_kalshi import LiveKalshiExecutor  # noqa: E402

MAX_CONTRACTS = 5
RESTING_BID_PRICE = 0.01  # 1¢ — far below any real market, will not fill


def _fee_estimate(contracts: float, price: float) -> float:
    """Kalshi taker fee ≈ 0.07 * C * P * (1-P), rounded up to the cent."""
    raw = 0.07 * contracts * price * (1.0 - price)
    return (int(raw * 100) + (1 if raw * 100 % 1 else 0)) / 100.0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Kalshi order plumbing smoke test")
    parser.add_argument("--env", choices=("demo", "prod"), default="demo")
    parser.add_argument(
        "--i-understand-real-money",
        action="store_true",
        help="Required for --env prod",
    )
    parser.add_argument(
        "--spend",
        action="store_true",
        help="Also place a tiny marketable order that can actually fill",
    )
    parser.add_argument("--contracts", type=float, default=1.0)
    parser.add_argument("--max-cost", type=float, default=1.00, help="Hard cap in USD")
    parser.add_argument("--series", default=os.getenv("KALSHI_SERIES", "KXBTC15M"))
    parser.add_argument(
        "--side",
        choices=("ABOVE", "BELOW"),
        default="ABOVE",
        help="Which leg to buy for the fill test",
    )
    args = parser.parse_args(argv)

    if args.env == "prod" and not args.i_understand_real_money:
        print(
            "Refusing: --env prod needs --i-understand-real-money.\n"
            "This places a REAL order with REAL funds."
        )
        return 2

    if args.contracts <= 0 or args.contracts > MAX_CONTRACTS:
        print(f"Refusing: --contracts must be between 0 and {MAX_CONTRACTS}")
        return 2

    if not credentials_configured():
        print("Kalshi credentials missing. Set KALSHI_API_KEY_ID + private key.")
        return 1

    base_url = DEFAULT_PROD_BASE if args.env == "prod" else DEFAULT_DEMO_BASE
    print("=" * 66)
    print("  KALSHI ORDER SMOKE TEST (bot stays on paper)")
    print("=" * 66)
    print(f"  Environment : {args.env}  ({base_url})")
    print(f"  Fill test   : {'YES — real order' if args.spend else 'no (cancel test only)'}")

    try:
        client = KalshiAuthClient.from_env()
    except KalshiAuthError as exc:
        print(f"  Auth build FAILED: {exc}")
        return 2
    # Respect the explicit --env choice over whatever .env said
    client.base_url = base_url.rstrip("/")

    try:
        start_balance = client.get_balance()
    except KalshiAuthError as exc:
        print(f"  Auth FAILED: {exc}")
        return 2
    print(f"  Balance     : ${start_balance.balance_usd:,.2f}")

    market = fetch_current_btc_15m(series_ticker=args.series)
    if market is None or not market.ticker:
        print("  No active BTC 15m market right now — try again shortly.")
        return 1

    yes_ask = market.buy_yes_price
    no_ask = market.buy_no_price
    print(f"  Market      : {market.ticker}")
    print(f"  Strike      : {market.strike}")
    print(
        f"  Asks        : YES "
        f"{f'{yes_ask*100:.0f}¢' if yes_ask else '—'} / NO "
        f"{f'{no_ask*100:.0f}¢' if no_ask else '—'}"
    )

    executor = LiveKalshiExecutor(client, dry_run=False, stake_notional=args.contracts)

    # ---- Phase 1: resting order that cannot fill, then cancel -------------
    print("\n[1/2] Resting-order test (1¢ bid, cannot fill)…")
    # Built inline rather than via build_payload: that helper deliberately
    # rejects sub-2¢ prices so the strategy can never take a fake fill.
    rest_payload = {
        "ticker": market.ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": "bid",
        "count": "1.00",
        "price": f"{RESTING_BID_PRICE:.4f}",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
    }
    try:
        created = client.create_order_v2(rest_payload)
    except KalshiAuthError as exc:
        print(f"  CREATE FAILED: {exc}")
        return 3

    order_id = str(created.get("order_id") or "")
    print(f"  created order_id={order_id or '(none)'} "
          f"remaining={created.get('remaining_count')}")
    if not order_id:
        print("  No order_id returned — cannot test cancel.")
        return 3

    time.sleep(1.0)
    try:
        cancelled = client.cancel_order_v2(order_id)
        print(f"  cancelled reduced_by={cancelled.get('reduced_by')}")
    except KalshiAuthError as exc:
        print(f"  CANCEL FAILED: {exc}")
        print(f"  !! Cancel this manually on Kalshi: {order_id}")
        return 3
    print("  Phase 1 PASS — auth, create, and cancel all work.")

    if not args.spend:
        print(
            "\nSkipping fill test (no --spend). Nothing was bought.\n"
            "Re-run with --spend to verify a real fill."
        )
        return 0

    # ---- Phase 2: tiny marketable order that can actually fill -----------
    ask = yes_ask if args.side == "ABOVE" else no_ask
    if ask is None:
        print(f"\n[2/2] No tradable {args.side} ask right now — skipping fill test.")
        return 1

    cost = args.contracts * ask
    if cost > args.max_cost:
        print(
            f"\n[2/2] Refusing: {args.contracts:g} contracts @ {ask*100:.0f}¢ "
            f"= ${cost:.2f}, over --max-cost ${args.max_cost:.2f}"
        )
        return 2

    print(f"\n[2/2] Fill test — buy {args.side} {args.contracts:g} @ {ask*100:.0f}¢ "
          f"(~${cost:.2f}, est fee ${_fee_estimate(args.contracts, ask):.2f})…")
    plan = executor.plan_order(
        market_ticker=market.ticker,
        advice_side=args.side,
        share_price=ask,
        contracts=args.contracts,
    )
    result = executor.execute(plan)

    if result.error:
        print(f"  ORDER FAILED: {result.error}")
        return 3

    print(f"  order_id      : {result.order_id}")
    print(f"  filled        : {result.fill_count:g} / {args.contracts:g}")
    print(f"  remaining     : {result.remaining_count:g}")
    if result.average_fill_price is not None:
        slip = (result.average_fill_price - ask) * 100
        print(
            f"  avg fill      : {result.average_fill_price*100:.1f}¢ "
            f"(vs {ask*100:.0f}¢ ask, {slip:+.1f}¢)"
        )

    try:
        end_balance = client.get_balance()
        delta = end_balance.balance_usd - start_balance.balance_usd
        print(f"  balance       : ${end_balance.balance_usd:,.2f} ({delta:+.2f})")
    except KalshiAuthError:
        pass

    if result.filled:
        print(
            "\n  Phase 2 PASS — real fill confirmed.\n"
            "  This position settles with its 15m window; nothing else to do."
        )
    else:
        print(
            "\n  Phase 2: order went through but did NOT fill (IOC, ask moved).\n"
            "  That itself is useful data — it's the miss-rate we care about."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
