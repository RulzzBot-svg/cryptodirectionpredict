#!/usr/bin/env python3
"""Check Kalshi live readiness without placing orders.

- Verifies auth / balance
- Prints a sample ABOVE + BELOW V2 order payload
- Reminds that LIVE_TRADING is hard-gated until paper week ends
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from config.live_gate import (  # noqa: E402
    EARLIEST_LIVE_DATE,
    LIVE_CONFIRM_PHRASE,
    describe_live_block_reason,
    live_trading_requested,
    paper_week_complete,
)
from data.kalshi_auth import (  # noqa: E402
    KalshiAuthClient,
    KalshiAuthError,
    credentials_configured,
)
from execution.live_kalshi import LiveKalshiExecutor  # noqa: E402


def main() -> int:
    print("Kalshi live readiness check (no orders submitted)")
    print(f"  paper week unlock date : {EARLIEST_LIVE_DATE.isoformat()}")
    print(f"  paper week complete    : {paper_week_complete()}")
    print(f"  LIVE_TRADING requested : {live_trading_requested()}")
    if live_trading_requested():
        block = describe_live_block_reason()
        if block:
            print(f"  live arm status        : BLOCKED — {block}")
        else:
            print("  live arm status        : ALLOWED (still confirm carefully)")
    else:
        print("  live arm status        : OFF (LIVE_TRADING not set)")
        if not paper_week_complete():
            print(
                f"  note                   : real live stays locked until "
                f"{EARLIEST_LIVE_DATE.isoformat()}"
            )

    if not credentials_configured():
        print("\nCredentials missing. Set KALSHI_API_KEY_ID + private key first.")
        return 1

    try:
        client = KalshiAuthClient.from_env()
        bal = client.get_balance()
    except KalshiAuthError as exc:
        print(f"\nAuth FAILED: {exc}")
        return 2

    print(f"\nAuth OK | base {client.base_url}")
    print(f"  cash ${bal.balance_usd:,.2f}")

    ex = LiveKalshiExecutor(client, dry_run=True, stake_notional=5.0)
    above = ex.plan_order(
        market_ticker="KXBTC15M-EXAMPLE",
        advice_side="ABOVE",
        share_price=0.44,
    )
    below = ex.plan_order(
        market_ticker="KXBTC15M-EXAMPLE",
        advice_side="BELOW",
        share_price=0.28,
    )
    print("\nSample ABOVE payload (buy YES / bid):")
    print(f"  {above.payload}")
    print("Sample BELOW payload (buy NO via sell YES / ask):")
    print(f"  {below.payload}")
    print(
        "\nTo rehearse on the running paper bot without submitting:\n"
        "  LIVE_DRY_RUN=true\n"
        "Do NOT set LIVE_TRADING=true until after paper week, then also set:\n"
        f"  LIVE_CONFIRM={LIVE_CONFIRM_PHRASE}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
