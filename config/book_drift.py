"""Book vs Kalshi cash drift: one Telegram ping, not one per heartbeat."""

from __future__ import annotations

from typing import Optional


def signed_drift(book_bank: float, vaulted: float, kalshi_cash: float) -> float:
    """Kalshi is truth. Prefer the reading (bank-only vs bank+vault) closer to it."""
    bank_only = float(book_bank) - float(kalshi_cash)
    with_vault = bank_only + float(vaulted or 0.0)
    return min((bank_only, with_vault), key=abs)


def should_notify_drift(
    *,
    drift: float,
    threshold: float,
    now_ts: float,
    last_sent_ts: float,
    last_sent_drift: Optional[float],
    cooldown_seconds: float,
) -> bool:
    """Ping on first breach, if the gap moves by ≥ threshold, or after cooldown."""
    if threshold <= 0:
        return False
    if abs(drift) <= threshold:
        return False
    if last_sent_drift is None:
        return True
    if abs(drift - last_sent_drift) >= threshold:
        return True
    if cooldown_seconds <= 0:
        return True
    return (now_ts - last_sent_ts) >= cooldown_seconds
