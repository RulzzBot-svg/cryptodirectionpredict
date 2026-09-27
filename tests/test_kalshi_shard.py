#!/usr/bin/env python3
"""Crypto shard routing + the 404 the live bot hit on Sep 20."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.kalshi_auth import humanize_kalshi_order_error
from execution.live_kalshi import LiveKalshiExecutor


def test_orders_auto_route_off_default_shard() -> None:
    payload, side, yes = LiveKalshiExecutor.build_payload(
        market_ticker="KXBTC15M-26SEP200500-00",
        advice_side="BELOW",
        share_price=0.66,
        contracts=5,
    )
    assert side == "ask"
    assert abs(yes - 0.34) < 1e-9
    assert payload["exchange_index"] == -1


def test_shard_404_is_actionable() -> None:
    raw = (
        'Create order failed (404): {"error":{"code":"insufficient_shard_balance",'
        '"message":"insufficient shard balance",'
        '"details":"Exchange user not found. For Predictions: reference '
        'documentation Exchange Sharding documentation."}}'
    )
    text = humanize_kalshi_order_error(raw)
    assert "crypto shard" in text.lower()
    assert "index 2" in text
    assert "DRY-RUN" not in text


if __name__ == "__main__":
    test_orders_auto_route_off_default_shard()
    test_shard_404_is_actionable()
    print("test_kalshi_shard ok")
