#!/usr/bin/env python3
"""Book vs Kalshi drift alerts should not fire every heartbeat."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.book_drift import should_notify_drift, signed_drift


def test_signed_drift_prefers_closer_reading() -> None:
    # Book $119.58, Kalshi $68.95, no vault → +$50.63 (the live spam case).
    assert abs(signed_drift(119.58, 0.0, 68.95) - 50.63) < 1e-9
    # Vault still on Kalshi: bank-only is 0, so no alarm.
    assert abs(signed_drift(68.95, 50.0, 68.95)) < 1e-9
    # Book low, vault explains most of it → tiny remainder.
    assert abs(signed_drift(18.32, 50.0, 68.95) - (-0.63)) < 1e-9


def test_notify_once_until_cooldown_or_move() -> None:
    assert should_notify_drift(
        drift=50.63,
        threshold=3.0,
        now_ts=100.0,
        last_sent_ts=0.0,
        last_sent_drift=None,
        cooldown_seconds=86400,
    )
    assert not should_notify_drift(
        drift=50.63,
        threshold=3.0,
        now_ts=100.0 + 900,
        last_sent_ts=100.0,
        last_sent_drift=50.63,
        cooldown_seconds=86400,
    )
    # Same stuck gap next day.
    assert should_notify_drift(
        drift=50.63,
        threshold=3.0,
        now_ts=100.0 + 86400,
        last_sent_ts=100.0,
        last_sent_drift=50.63,
        cooldown_seconds=86400,
    )
    # Gap jumped another $4 before cooldown.
    assert should_notify_drift(
        drift=54.63,
        threshold=3.0,
        now_ts=100.0 + 900,
        last_sent_ts=100.0,
        last_sent_drift=50.63,
        cooldown_seconds=86400,
    )
    assert not should_notify_drift(
        drift=1.50,
        threshold=3.0,
        now_ts=200.0,
        last_sent_ts=0.0,
        last_sent_drift=None,
        cooldown_seconds=86400,
    )


if __name__ == "__main__":
    test_signed_drift_prefers_closer_reading()
    test_notify_once_until_cooldown_or_move()
    print("test_book_drift ok")
