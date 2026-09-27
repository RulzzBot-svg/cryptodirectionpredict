#!/usr/bin/env python3
"""Move Kalshi predictions cash from Default (0) to Crypto (2).

BTC 15m lives on shard 2. Cash left on 0 404s as insufficient_shard_balance.

On Render (after this file is deployed):
  .venv/bin/python scripts/fund_crypto_shard.py
  .venv/bin/python scripts/fund_crypto_shard.py --usd 60 --yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from data.kalshi_auth import (  # noqa: E402
    KALSHI_CRYPTO_SHARD,
    KALSHI_DEFAULT_SHARD,
    KalshiAuthClient,
    KalshiAuthError,
    credentials_configured,
)


def _print_shards(client: KalshiAuthClient) -> tuple[float, float]:
    total = client.get_balance()
    print(f"all shards : ${total.balance_usd:,.2f}")
    default = 0.0
    crypto = 0.0
    try:
        default = client.get_balance(exchange_index=KALSHI_DEFAULT_SHARD).balance_usd
        print(f"default  0 : ${default:,.2f}")
    except KalshiAuthError as exc:
        print(f"default  0 : unavailable ({exc})")
    try:
        crypto = client.get_balance(exchange_index=KALSHI_CRYPTO_SHARD).balance_usd
        print(f"crypto   {KALSHI_CRYPTO_SHARD} : ${crypto:,.2f}")
    except KalshiAuthError as exc:
        print(f"crypto   {KALSHI_CRYPTO_SHARD} : unavailable ({exc})")
    return default, crypto


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--usd", type=float, default=0.0, help="dollars to move 0 → 2")
    parser.add_argument("--yes", action="store_true", help="actually transfer")
    args = parser.parse_args()

    if not credentials_configured():
        print("Kalshi API keys not set")
        return 1
    client = KalshiAuthClient.from_env()
    default, crypto = _print_shards(client)
    if not args.yes:
        print("\nDry look only. To move $60:  --usd 60 --yes")
        return 0
    usd = float(args.usd)
    if usd <= 0:
        usd = max(0.0, default - 5.0)
        print(f"\nNo --usd; moving ${usd:,.2f} (leave ~$5 on default)")
    if usd < 5:
        print("Nothing to move (need at least $5 on default above a $5 pad).")
        return 2
    try:
        raw = client.transfer_between_shards(usd=usd)
    except KalshiAuthError as exc:
        print(f"TRANSFER FAILED: {exc}")
        return 3
    print(f"transfer accepted: {raw}")
    _print_shards(client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
