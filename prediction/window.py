"""Wall-clock aligned 15-minute prediction windows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


def _to_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def floor_to_window(ts: datetime, minutes: int = 15) -> datetime:
    """Floor a timestamp to the start of its prediction window (UTC)."""
    ts = _to_utc(ts)
    minute = (ts.minute // minutes) * minutes
    return ts.replace(minute=minute, second=0, microsecond=0)


@dataclass
class PredictionWindow:
    """One above/below contract window (Kalshi/Robinhood-style)."""

    start: datetime
    end: datetime
    strike: Optional[float] = None
    opening_price: Optional[float] = None
    strike_source: str = "auto"  # auto | manual
    settled: bool = False
    settlement_price: Optional[float] = None
    outcome: Optional[str] = None  # ABOVE | BELOW | PUSH

    @property
    def window_id(self) -> str:
        return self.start.strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    def seconds_remaining(self, now: Optional[datetime] = None) -> float:
        now = _to_utc(now or datetime.now(timezone.utc))
        return max(0.0, (self.end - now).total_seconds())

    def seconds_elapsed(self, now: Optional[datetime] = None) -> float:
        now = _to_utc(now or datetime.now(timezone.utc))
        return min(self.duration_seconds, max(0.0, (now - self.start).total_seconds()))

    def is_active(self, now: Optional[datetime] = None) -> bool:
        now = _to_utc(now or datetime.now(timezone.utc))
        return self.start <= now < self.end

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        now = _to_utc(now or datetime.now(timezone.utc))
        return now >= self.end

    def lock_strike(self, price: float) -> None:
        if self.strike is None and price > 0:
            self.strike = float(price)
            self.opening_price = float(price)

    def set_strike(self, price: float, *, source: str = "manual") -> bool:
        """
        Force-set / override the window strike (e.g. Robinhood BRTI target).

        Returns True if the strike value changed.
        """
        if price <= 0:
            raise ValueError("strike must be positive")
        new_price = float(price)
        changed = self.strike is None or abs(float(self.strike) - new_price) > 1e-9
        self.strike = new_price
        if self.opening_price is None:
            self.opening_price = new_price
        self.strike_source = source
        return changed

    def settle(self, final_price: float) -> str:
        if self.strike is None:
            raise ValueError("Cannot settle window without a strike")
        self.settlement_price = float(final_price)
        self.settled = True
        if final_price > self.strike:
            self.outcome = "ABOVE"
        elif final_price < self.strike:
            self.outcome = "BELOW"
        else:
            self.outcome = "PUSH"
        return self.outcome


class WindowManager:
    """Tracks the current 15m window and rolls forward on expiry."""

    def __init__(self, window_minutes: int = 15) -> None:
        self.window_minutes = window_minutes
        self.current: Optional[PredictionWindow] = None

    def _build_window(self, now: datetime) -> PredictionWindow:
        start = floor_to_window(now, self.window_minutes)
        end = start + timedelta(minutes=self.window_minutes)
        return PredictionWindow(start=start, end=end)

    def update(
        self,
        mark_price: float,
        now: Optional[datetime] = None,
        *,
        strike_price: Optional[float] = None,
    ) -> tuple[PredictionWindow, Optional[PredictionWindow]]:
        """
        Sync window state with the clock and live price.

        Parameters
        ----------
        mark_price:
            Latest traded / mid price (used for settlement).
        strike_price:
            Optional price used only when locking a new window strike
            (e.g. the current 15m candle open). Defaults to ``mark_price``.
        """
        now = _to_utc(now or datetime.now(timezone.utc))
        lock_px = float(strike_price) if strike_price and strike_price > 0 else float(mark_price)
        expired: Optional[PredictionWindow] = None

        if self.current is None:
            self.current = self._build_window(now)
            self.current.lock_strike(lock_px)
            return self.current, None

        if self.current.is_expired(now) and not self.current.settled:
            expired = self.current
            expired.settle(mark_price)
            self.current = self._build_window(now)
            self.current.lock_strike(lock_px)
            return self.current, expired

        # Rolled past without settle path (e.g. clock jump)
        expected_start = floor_to_window(now, self.window_minutes)
        if self.current.start != expected_start:
            if not self.current.settled and self.current.strike is not None:
                expired = self.current
                expired.settle(mark_price)
            self.current = self._build_window(now)
            self.current.lock_strike(lock_px)
            return self.current, expired

        if self.current.strike is None:
            self.current.lock_strike(lock_px)

        return self.current, expired

    def apply_manual_strike(self, price: float) -> bool:
        """Override the active window strike. Returns True if it changed."""
        if self.current is None:
            return False
        return self.current.set_strike(price, source="manual")
