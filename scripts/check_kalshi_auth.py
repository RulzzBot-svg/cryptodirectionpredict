#!/usr/bin/env python3
"""Smoke-test Kalshi API credentials (balance only — no orders).

Usage:
  1. Create a key at kalshi.com → Account & security → API Keys
  2. Save the .key file outside the repo (e.g. ~/.config/kalshi/prod.key)
  3. Put Key ID + path in .env (see .env.example)
  4. python scripts/check_kalshi_auth.py

This never places trades. Live trading stays off until you explicitly enable it
after the paper week.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python scripts/check_kalshi_auth.py` from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from data.kalshi_auth import (  # noqa: E402
    KalshiAuthClient,
    KalshiAuthError,
    credentials_configured,
)


def main() -> int:
    if not credentials_configured():
        print(
            "Kalshi credentials not configured.\n\n"
            "Do this once (this weekend is fine):\n"
            "  1. Log in at https://kalshi.com (or https://demo.kalshi.co for practice)\n"
            "  2. Account & security → API Keys → Create Key\n"
            "  3. Save the downloaded .key file somewhere safe (NOT in git)\n"
            "  4. Copy the API Key ID shown on screen\n"
            "  5. In .env set:\n"
            "       KALSHI_API_KEY_ID=...\n"
            "       KALSHI_PRIVATE_KEY_PATH=/path/to/your.key\n"
            "       KALSHI_ENV=prod   # or demo\n"
            "  6. Re-run: python scripts/check_kalshi_auth.py\n"
        )
        return 1

    try:
        client = KalshiAuthClient.from_env()
        balance = client.get_balance()
    except KalshiAuthError as exc:
        print(f"Auth check FAILED: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 — CLI should show any failure clearly
        print(f"Auth check FAILED: {exc}")
        return 2

    print("Kalshi auth OK (read-only balance check).")
    print(f"  base URL : {client.base_url}")
    print(f"  cash     : ${balance.balance_usd:,.2f}")
    if balance.portfolio_value_usd is not None:
        print(f"  portfolio: ${balance.portfolio_value_usd:,.2f}")
    print(
        "\nNo orders were placed. Keep LIVE trading off until after paper week."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
