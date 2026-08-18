"""Local-time betting blackout (e.g. 7–11 PM America/Los_Angeles).

During a blackout the bot still prices windows and settles fills, but it must
not submit new paper/live orders. Resting maker quotes should be cancelled so
they cannot get hit inside the window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _parse_hhmm(raw: str, *, field: str) -> time:
    text = (raw or "").strip()
    try:
        parts = text.split(":")
        if len(parts) != 2:
            raise ValueError
        hour = int(parts[0])
        minute = int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return time(hour=hour, minute=minute)
    except ValueError as exc:
        raise ValueError(
            f"{field} must be HH:MM (00:00–23:59), got {raw!r}"
        ) from exc


@dataclass(frozen=True)
class BetBlackout:
    """Half-open local window [start, end). Supports overnight wrap."""

    enabled: bool
    tz_name: str
    start: time
    end: time

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz_name)
        except ZoneInfoNotFoundError as exc:
            raise ZoneInfoNotFoundError(
                f"Unknown timezone {self.tz_name!r}. Install the tzdata "
                "package or set BET_BLACKOUT_TZ to a valid IANA name."
            ) from exc

    def active(self, now: Optional[datetime] = None) -> bool:
        if not self.enabled:
            return False
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        local = instant.astimezone(self.tz)
        stamp = local.timetz().replace(tzinfo=None)
        if self.start == self.end:
            # Degenerate config = always on when enabled would be surprising;
            # treat as never blacked out.
            return False
        if self.start < self.end:
            return self.start <= stamp < self.end
        # Overnight wrap, e.g. 22:00 → 06:00
        return stamp >= self.start or stamp < self.end

    def label(self) -> str:
        if not self.enabled:
            return "OFF"
        return (
            f"{self.start.strftime('%H:%M')}–{self.end.strftime('%H:%M')} "
            f"{self.tz_name} (no new bets)"
        )


def load_bet_blackout(
    *,
    enabled: bool,
    tz_name: str,
    start_raw: str,
    end_raw: str,
) -> BetBlackout:
    return BetBlackout(
        enabled=bool(enabled),
        tz_name=(tz_name or "America/Los_Angeles").strip() or "America/Los_Angeles",
        start=_parse_hhmm(start_raw, field="BET_BLACKOUT_START"),
        end=_parse_hhmm(end_raw, field="BET_BLACKOUT_END"),
    )
