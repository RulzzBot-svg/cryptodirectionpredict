"""Automatic no-bet guards so the bot can run without a babysitter.

Evening hours are handled by ``config.bet_blackout``. This module covers the
other kill rules from live MAD v2:

- stop for the rest of the local day after a −$15 day (or ≤56% WR on enough bets)
- stop while the cash bank is at/below a floor (don't grind to $0)

Halts clear themselves: day rules reset at local midnight, bank floor clears
when cash recovers (settles / deposit). They do **not** flip LIVE_TRADING.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class RiskSnapshot:
    bank: float
    today: date
    today_n: int
    today_wins: int
    today_pnl: float

    @property
    def today_wr(self) -> float:
        if self.today_n <= 0:
            return 0.0
        return self.today_wins / self.today_n


@dataclass(frozen=True)
class HaltDecision:
    reason: str
    detail: str


@dataclass(frozen=True)
class AutoHalt:
    enabled: bool
    tz_name: str
    bank_floor: float
    day_loss: float
    day_min_bets: int
    day_max_wr: float

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz_name)
        except ZoneInfoNotFoundError as exc:
            raise ZoneInfoNotFoundError(
                f"Unknown timezone {self.tz_name!r}. Install tzdata or set "
                "BET_HALT_TZ to a valid IANA name."
            ) from exc

    def label(self) -> str:
        if not self.enabled:
            return "OFF"
        parts = []
        if self.day_loss > 0:
            parts.append(f"day −${self.day_loss:g}")
        if self.day_min_bets > 0:
            parts.append(
                f"or ≤{self.day_max_wr * 100:.0f}% WR (n≥{self.day_min_bets})"
            )
        if self.bank_floor > 0:
            parts.append(f"bank ≤ ${self.bank_floor:g}")
        body = " ".join(parts) if parts else "on"
        return f"{body}; resume next {self.tz_name} morning"

    def evaluate(self, snap: RiskSnapshot) -> Optional[HaltDecision]:
        if not self.enabled:
            return None
        if self.bank_floor > 0 and snap.bank <= self.bank_floor + 1e-9:
            return HaltDecision(
                "bank floor",
                f"bank ${snap.bank:,.2f} ≤ ${self.bank_floor:g} floor",
            )
        if (
            self.day_loss > 0
            and snap.today_n >= 8
            and snap.today_pnl <= -self.day_loss
        ):
            return HaltDecision(
                "day loss",
                (
                    f"{snap.today.isoformat()} {snap.today_n} bets "
                    f"${snap.today_pnl:+.2f} (limit −${self.day_loss:g})"
                ),
            )
        if (
            self.day_min_bets > 0
            and snap.today_n >= self.day_min_bets
            and snap.today_pnl < 0
            and snap.today_wr <= self.day_max_wr + 1e-12
        ):
            return HaltDecision(
                "day wr",
                (
                    f"{snap.today.isoformat()} WR {snap.today_wr * 100:.0f}% "
                    f"on {snap.today_n} (limit ≤{self.day_max_wr * 100:.0f}%)"
                ),
            )
        return None


def load_auto_halt(
    *,
    enabled: bool,
    tz_name: str,
    bank_floor: float,
    day_loss: float,
    day_min_bets: int,
    day_max_wr: float,
) -> AutoHalt:
    wr = float(day_max_wr)
    if wr > 1.0:
        wr = wr / 100.0
    return AutoHalt(
        enabled=bool(enabled),
        tz_name=(tz_name or "America/Los_Angeles").strip()
        or "America/Los_Angeles",
        bank_floor=max(0.0, float(bank_floor)),
        day_loss=max(0.0, float(day_loss)),
        day_min_bets=max(0, int(day_min_bets)),
        day_max_wr=min(1.0, max(0.0, wr)),
    )


def _as_utc(raw: object) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_today(tz_name: str, now: Optional[datetime] = None) -> date:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(ZoneInfo(tz_name)).date()


def snapshot_from_rows(
    *,
    bank: float,
    rows: list[tuple[object, object, object]],
    tz_name: str,
    now: Optional[datetime] = None,
) -> RiskSnapshot:
    """Build a snapshot from (pnl, settled_at, status) settled rows."""
    today = local_today(tz_name, now=now)
    tz = ZoneInfo(tz_name)
    today_n = 0
    today_wins = 0
    today_pnl = 0.0
    for pnl, settled_at, status in rows:
        when = _as_utc(settled_at)
        if when is None:
            continue
        if when.astimezone(tz).date() != today:
            continue
        today_n += 1
        today_pnl += float(pnl or 0.0)
        if str(status).upper() == "WON" or float(pnl or 0.0) > 0:
            today_wins += 1
    return RiskSnapshot(
        bank=float(bank),
        today=today,
        today_n=today_n,
        today_wins=today_wins,
        today_pnl=today_pnl,
    )
