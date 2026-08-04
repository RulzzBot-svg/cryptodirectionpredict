"""Kalshi weather market client (public market data)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import requests

_TICKER_DATE = re.compile(
    r"-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})(?:-|$)"
)
_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


@dataclass(frozen=True)
class WeatherContract:
    ticker: str
    event_ticker: str
    title: str
    yes_sub_title: str
    strike_type: str  # between | greater | less
    floor_strike: Optional[float]
    cap_strike: Optional[float]
    yes_bid: float
    yes_ask: float
    last_yes: float
    target_date: date

    @property
    def market_yes(self) -> float:
        """Mid price when possible; fall back to last / ask."""
        if self.yes_bid > 0 and self.yes_ask > 0:
            return 0.5 * (self.yes_bid + self.yes_ask)
        if self.last_yes > 0:
            return self.last_yes
        if self.yes_ask > 0:
            return self.yes_ask
        return self.yes_bid

    @property
    def label(self) -> str:
        return self.yes_sub_title or self.title


def parse_ticker_date(ticker: str) -> Optional[date]:
    m = _TICKER_DATE.search(ticker.upper())
    if not m:
        return None
    mon = _MONTHS.get(m.group("mon"))
    if not mon:
        return None
    yy = int(m.group("yy"))
    dd = int(m.group("dd"))
    year = 2000 + yy
    try:
        return date(year, mon, dd)
    except ValueError:
        return None


def _money(val) -> float:
    if val is None:
        return 0.0
    return float(val)


def fetch_open_weather_markets(
    base_url: str,
    series_ticker: str,
    target: date,
    timeout: float = 20.0,
) -> list[WeatherContract]:
    """Pull open markets for a KXHIGH* series filtered to `target` date."""
    url = f"{base_url}/markets"
    params = {
        "series_ticker": series_ticker,
        "status": "open",
        "limit": 200,
    }
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    markets = resp.json().get("markets") or []

    out: list[WeatherContract] = []
    for m in markets:
        ticker = m.get("ticker") or ""
        d = parse_ticker_date(ticker)
        if d != target:
            continue
        st = (m.get("strike_type") or "").lower()
        if st not in {"between", "greater", "less"}:
            continue
        out.append(
            WeatherContract(
                ticker=ticker,
                event_ticker=m.get("event_ticker") or "",
                title=m.get("title") or "",
                yes_sub_title=m.get("yes_sub_title") or "",
                strike_type=st,
                floor_strike=m.get("floor_strike"),
                cap_strike=m.get("cap_strike"),
                yes_bid=_money(m.get("yes_bid_dollars")),
                yes_ask=_money(m.get("yes_ask_dollars")),
                last_yes=_money(m.get("last_price_dollars")),
                target_date=d,
            )
        )
    return out


def resolve_target_date(spec: str, timezone_name: str) -> date:
    """Interpret today | tomorrow | YYYY-MM-DD in the city timezone."""
    from zoneinfo import ZoneInfo

    spec = spec.strip().lower()
    now_local = datetime.now(ZoneInfo(timezone_name)).date()
    if spec in {"today", ""}:
        return now_local
    if spec == "tomorrow":
        from datetime import timedelta

        return now_local + timedelta(days=1)
    return date.fromisoformat(spec)
