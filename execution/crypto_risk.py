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


_FORBIDDEN_DB_HINTS = ("live_trading", "live_mad")


def assert_crypto_paper_ready(database_url: str, *, live_crypto: bool) -> None:
    """Hard-stop live arming and Kalshi DB mix-ups."""
    if live_crypto:
        raise SystemExit(
            "LIVE_CRYPTO is on, but this venture is paper only. "
            "Set LIVE_CRYPTO=false. Do not spend leftover Kalshi cash here."
        )
    blob = (database_url or "").lower()
    for hint in _FORBIDDEN_DB_HINTS:
        if hint in blob:
            raise SystemExit(
                f"Refusing database {database_url!r} — that is the Kalshi book. "
                "Set CRYPTO_DATABASE_URL=sqlite:////var/data/crypto_paper.db"
            )


def format_crypto_digest(stats: dict, *, local_day: str, symbol: str) -> str:
    """One morning scorecard for the crypto paper book."""
    eq = float(stats.get("equity") or 0.0)
    usd = float(stats.get("usd_balance") or 0.0)
    btc = float(stats.get("btc_balance") or 0.0)
    realized = float(stats.get("realized_pnl") or 0.0)
    unreal = float(stats.get("unrealized_pnl") or 0.0)
    wins = int(stats.get("win_count") or 0)
    losses = int(stats.get("loss_count") or 0)
    n = int(stats.get("sell_count") or 0)
    wr = float(stats.get("win_rate_pct") or 0.0)
    ret = float(stats.get("total_return_pct") or 0.0)
    pos = "LONG" if btc > 1e-10 else "FLAT"
    return "\n".join(
        [
            f"CRYPTO DAILY {local_day}",
            f"{symbol} | paper | live locked | {pos}",
            f"Equity ${eq:,.2f} | USD ${usd:,.2f} | BTC {btc:.8f}",
            f"Realized ${realized:+,.2f} | Unrealized ${unreal:+,.2f} | {ret:+.1f}%",
            f"Closed {n} | {wins}W/{losses}L ({wr:.1f}%)",
            "Call: stay paper — no Coinbase keys, no Kalshi cash",
        ]
    )
