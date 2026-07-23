"""Kalshi public market-data client for BTC 15-minute up/down contracts."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEFAULT_SERIES = "KXBTC15M"


@dataclass(frozen=True)
class KalshiBtcWindow:
    """Current (or nearest) Kalshi BTC 15m contract snapshot."""

    ticker: str
    event_ticker: str
    title: str
    strike: Optional[float]
    yes_bid: Optional[float]
    yes_ask: Optional[float]
    yes_last: Optional[float]
    open_time: Optional[datetime]
    close_time: Optional[datetime]
    status: str
    raw: dict[str, Any]

    @property
    def yes_mid(self) -> Optional[float]:
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2.0
        return self.yes_ask or self.yes_last or self.yes_bid

    @property
    def market_prob_above(self) -> Optional[float]:
        """Implied P(above) from Kalshi YES price in [0, 1]."""
        mid = self.yes_mid
        if mid is None:
            return None
        return max(0.0, min(1.0, float(mid)))


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _request_json(url: str, *, timeout: float = 15.0) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "cryptodirectionpredict/kalshi-feed",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch_markets(
    *,
    series_ticker: str = DEFAULT_SERIES,
    status: Optional[str] = "open",
    limit: int = 20,
    base_url: str = DEFAULT_BASE_URL,
) -> list[dict[str, Any]]:
    params: dict[str, str] = {
        "series_ticker": series_ticker,
        "limit": str(limit),
    }
    if status:
        params["status"] = status
    url = f"{base_url.rstrip('/')}/markets?{urllib.parse.urlencode(params)}"
    payload = _request_json(url)
    return list(payload.get("markets") or [])


def _from_market(market: dict[str, Any]) -> KalshiBtcWindow:
    return KalshiBtcWindow(
        ticker=str(market.get("ticker") or ""),
        event_ticker=str(market.get("event_ticker") or ""),
        title=str(market.get("title") or market.get("yes_sub_title") or ""),
        strike=_parse_float(market.get("floor_strike")),
        yes_bid=_parse_float(market.get("yes_bid_dollars")),
        yes_ask=_parse_float(market.get("yes_ask_dollars")),
        yes_last=_parse_float(market.get("last_price_dollars")),
        open_time=_parse_dt(market.get("open_time")),
        close_time=_parse_dt(market.get("close_time")),
        status=str(market.get("status") or ""),
        raw=market,
    )


def fetch_current_btc_15m(
    *,
    series_ticker: str = DEFAULT_SERIES,
    base_url: str = DEFAULT_BASE_URL,
    now: Optional[datetime] = None,
) -> Optional[KalshiBtcWindow]:
    """
    Fetch the active Kalshi BTC 15m market (same family Robinhood shows).

    Prefers ``status=open`` markets with a locked ``floor_strike``. Falls back to
    the soonest initialized market if nothing is open yet.
    """
    now = now or datetime.now(timezone.utc)
    try:
        open_markets = fetch_markets(
            series_ticker=series_ticker, status="open", base_url=base_url
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Kalshi open-market fetch failed: %s", exc)
        open_markets = []

    parsed = [_from_market(m) for m in open_markets]
    with_strike = [m for m in parsed if m.strike is not None]
    if with_strike:
        # Prefer the market whose window contains now; else nearest close_time
        active = [
            m
            for m in with_strike
            if m.open_time and m.close_time and m.open_time <= now < m.close_time
        ]
        if active:
            return active[0]
        with_strike.sort(
            key=lambda m: abs((m.close_time or now) - now).total_seconds()
        )
        return with_strike[0]

    # Fallback: upcoming/recent markets (strike often still TBD before open)
    try:
        upcoming = fetch_markets(
            series_ticker=series_ticker, status=None, limit=50, base_url=base_url
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Kalshi markets fetch failed: %s", exc)
        return None

    parsed_all = [_from_market(m) for m in upcoming]
    candidates = [m for m in parsed_all if m.open_time is not None]
    candidates.sort(key=lambda m: abs((m.open_time or now) - now).total_seconds())
    return candidates[0] if candidates else None
