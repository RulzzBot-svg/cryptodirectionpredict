"""Risk helpers for the paper crypto spot bot.

Kalshi 15m direction is a different game. This is long-only BTC with a stop
and a daily buy-halt. Live orders are not part of v1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo


def stop_hit(entry: float, price: float, stop_pct: float) -> bool:
    """True when mark has fallen ``stop_pct`` from the all-in entry."""
    if entry <= 0 or price <= 0 or stop_pct <= 0:
        return False
    return float(price) <= float(entry) * (1.0 - float(stop_pct))


def decide_crypto_action(
    *,
    signal: str,
    in_position: bool,
    entry: float,
    price: float,
    stop_pct: float,
    buy_halted: bool,
) -> str:
    """Turn a strategy signal + risk into BUY / SELL / HOLD.

    One position at a time. Stops and exits still fire when buys are halted.
    """
    sig = (signal or "HOLD").strip().upper()
    if in_position and stop_hit(entry, price, stop_pct):
        return "SELL"
    if sig == "SELL" and in_position:
        return "SELL"
    if sig == "BUY" and (not in_position) and (not buy_halted):
        return "BUY"
    return "HOLD"


def day_buy_halted(
    sells: Iterable,
    *,
    day_loss: float,
    now: Optional[datetime] = None,
    tz_name: str = "America/Los_Angeles",
) -> bool:
    """Halt new buys after realized day P/L hits ``-day_loss`` (local day)."""
    cap = abs(float(day_loss))
    if cap <= 0:
        return False
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    tz = ZoneInfo(tz_name)
    today = instant.astimezone(tz).date()
    pnl = 0.0
    for trade in sells:
        side = str(getattr(trade, "side", "") or "").upper()
        if side != "SELL":
            continue
        when = getattr(trade, "timestamp", None)
        if when is None:
            continue
        if isinstance(when, datetime):
            stamp = when
        else:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp.astimezone(tz).date() != today:
            continue
        pnl += float(getattr(trade, "realized_pnl", 0.0) or 0.0)
    return pnl <= -cap
