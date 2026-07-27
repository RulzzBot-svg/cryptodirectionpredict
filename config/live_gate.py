"""Hard gate so live Kalshi orders cannot arm before paper week ends."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Optional

# User-requested paper week through Thu/Fri before any live trading.
EARLIEST_LIVE_DATE = date(2026, 7, 30)  # Thursday
LIVE_CONFIRM_PHRASE = "YES_I_FINISHED_PAPER_WEEK"


def _today(now: Optional[datetime] = None) -> date:
    if now is None:
        return date.today()
    return now.date() if isinstance(now, datetime) else now


def live_trading_requested(env: Optional[dict[str, str]] = None) -> bool:
    source = env if env is not None else os.environ
    return (source.get("LIVE_TRADING") or "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def live_dry_run_requested(env: Optional[dict[str, str]] = None) -> bool:
    source = env if env is not None else os.environ
    return (source.get("LIVE_DRY_RUN") or "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def live_confirm_ok(env: Optional[dict[str, str]] = None) -> bool:
    source = env if env is not None else os.environ
    return (source.get("LIVE_CONFIRM") or "").strip() == LIVE_CONFIRM_PHRASE


def paper_week_complete(now: Optional[datetime] = None) -> bool:
    return _today(now) >= EARLIEST_LIVE_DATE


def describe_live_block_reason(
    env: Optional[dict[str, str]] = None,
    *,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Return a human reason live is blocked, or None if allowed."""
    if not live_trading_requested(env):
        return None
    if not paper_week_complete(now):
        return (
            f"LIVE_TRADING is blocked until {EARLIEST_LIVE_DATE.isoformat()} "
            f"(paper week through Thu/Fri). Today is {_today(now).isoformat()}. "
            "Use LIVE_DRY_RUN=true to rehearse order payloads without submitting."
        )
    if not live_confirm_ok(env):
        return (
            "LIVE_TRADING also requires "
            f"LIVE_CONFIRM={LIVE_CONFIRM_PHRASE} after the paper review."
        )
    return None


def enforce_live_gate(
    env: Optional[dict[str, str]] = None,
    *,
    now: Optional[datetime] = None,
) -> None:
    """Raise SystemExit if LIVE_TRADING is on but not allowed yet."""
    reason = describe_live_block_reason(env, now=now)
    if reason:
        raise SystemExit(f"Refusing to start: {reason}")
