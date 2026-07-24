"""Kalshi public market-data client for BTC 15-minute up/down contracts."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEFAULT_SERIES = "KXBTC15M"
_ET = ZoneInfo("America/New_York")
_MONTHS = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)


@dataclass(frozen=True)
class KalshiBtcWindow:
    """Current Kalshi BTC 15m contract snapshot."""

    ticker: str
    event_ticker: str
    title: str
    strike: Optional[float]
    yes_bid: Optional[float]
    yes_ask: Optional[float]
    yes_last: Optional[float]
    no_bid: Optional[float]
    no_ask: Optional[float]
    open_time: Optional[datetime]
    close_time: Optional[datetime]
    status: str
    raw: dict[str, Any]

    @staticmethod
    def _valid_quote(value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        price = float(value)
        # 0.0 from Kalshi usually means "no book yet", not a real 0¢ market
        if price <= 0.0 or price >= 1.0:
            return None
        return price

    @property
    def yes_mid(self) -> Optional[float]:
        bid = self._valid_quote(self.yes_bid)
        ask = self._valid_quote(self.yes_ask)
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        return ask or self._valid_quote(self.yes_last) or bid

    @property
    def buy_yes_price(self) -> Optional[float]:
        """Price to buy ABOVE/YES shares — ask only (no last/mid fallback)."""
        return self._valid_quote(self.yes_ask)

    @property
    def buy_no_price(self) -> Optional[float]:
        """Price to buy BELOW/NO shares — ask only, else complement of YES bid."""
        no_ask = self._valid_quote(self.no_ask)
        if no_ask is not None:
            return no_ask
        # Buying NO ≈ hitting the YES bid complement when NO ask is absent
        yes_bid = self._valid_quote(self.yes_bid)
        if yes_bid is None:
            return None
        return max(0.01, min(0.99, 1.0 - yes_bid))

    @property
    def market_prob_above(self) -> Optional[float]:
        """Implied P(above) from a tradable YES ask (else mid for display)."""
        return self.buy_yes_price or self.yes_mid

    @property
    def quotes_tradable(self) -> bool:
        yes = self.buy_yes_price
        no = self.buy_no_price
        return (
            yes is not None
            and no is not None
            and 0.02 <= yes <= 0.98
            and 0.02 <= no <= 0.98
        )


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


def window_bounds_et(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """Return [start, end) of the current 15m window in US/Eastern."""
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    et = now_utc.astimezone(_ET)
    minute = (et.minute // 15) * 15
    start = et.replace(minute=minute, second=0, microsecond=0)
    end = start + timedelta(minutes=15)
    return start, end


def expected_event_ticker(
    *,
    series_ticker: str = DEFAULT_SERIES,
    now: Optional[datetime] = None,
) -> str:
    """
    Build the Kalshi event ticker for the active window.

    Example: KXBTC15M-26JUL231400
    The trailing HHMM is the window END time in US/Eastern.
    """
    _, end_et = window_bounds_et(now)
    suffix = (
        f"{end_et.strftime('%y')}"
        f"{_MONTHS[end_et.month - 1]}"
        f"{end_et.strftime('%d%H%M')}"
    )
    return f"{series_ticker}-{suffix}"


def fetch_markets(
    *,
    series_ticker: Optional[str] = DEFAULT_SERIES,
    event_ticker: Optional[str] = None,
    status: Optional[str] = "open",
    limit: int = 20,
    base_url: str = DEFAULT_BASE_URL,
) -> list[dict[str, Any]]:
    params: dict[str, str] = {"limit": str(limit)}
    if event_ticker:
        params["event_ticker"] = event_ticker
    elif series_ticker:
        params["series_ticker"] = series_ticker
    if status:
        params["status"] = status
    url = f"{base_url.rstrip('/')}/markets?{urllib.parse.urlencode(params)}"
    payload = _request_json(url)
    return list(payload.get("markets") or [])


def _quote_dollars(market: dict[str, Any], *keys: str) -> Optional[float]:
    """Read the first present money field (dollars preferred, else cents/100)."""
    for key in keys:
        if key not in market or market.get(key) is None:
            continue
        raw = market.get(key)
        value = _parse_float(raw)
        if value is None:
            continue
        # Integer-ish cent fields (e.g. yes_bid=34) → dollars
        if "dollars" not in key and value > 1.0:
            value = value / 100.0
        return value
    return None


def _from_market(market: dict[str, Any]) -> KalshiBtcWindow:
    yes_bid = _quote_dollars(market, "yes_bid_dollars", "yes_bid")
    yes_ask = _quote_dollars(market, "yes_ask_dollars", "yes_ask")
    no_bid = _quote_dollars(market, "no_bid_dollars", "no_bid")
    no_ask = _quote_dollars(market, "no_ask_dollars", "no_ask")
    # Derive complementary NO book when Kalshi only publishes YES
    if no_ask is None and yes_bid is not None and 0.0 < yes_bid < 1.0:
        no_ask = 1.0 - yes_bid
    if no_bid is None and yes_ask is not None and 0.0 < yes_ask < 1.0:
        no_bid = 1.0 - yes_ask
    return KalshiBtcWindow(
        ticker=str(market.get("ticker") or ""),
        event_ticker=str(market.get("event_ticker") or ""),
        title=str(market.get("title") or market.get("yes_sub_title") or ""),
        strike=_parse_float(market.get("floor_strike")),
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        yes_last=_quote_dollars(market, "last_price_dollars", "last_price"),
        no_bid=no_bid,
        no_ask=no_ask,
        open_time=_parse_dt(market.get("open_time")),
        close_time=_parse_dt(market.get("close_time")),
        status=str(market.get("status") or ""),
        raw=market,
    )


def _pick_active(markets: list[KalshiBtcWindow], now: datetime) -> Optional[KalshiBtcWindow]:
    """Prefer the market whose [open, close) contains now and has a strike."""
    timed = [
        m
        for m in markets
        if m.open_time and m.close_time and m.open_time <= now < m.close_time
    ]
    with_strike = [m for m in timed if m.strike is not None]
    if with_strike:
        return with_strike[0]
    if timed:
        return timed[0]
    return None


def fetch_current_btc_15m(
    *,
    series_ticker: str = DEFAULT_SERIES,
    base_url: str = DEFAULT_BASE_URL,
    now: Optional[datetime] = None,
) -> Optional[KalshiBtcWindow]:
    """
    Fetch the Kalshi BTC 15m market for the *current* ET window only.

    Resolution order:
      1. event_ticker for this window (e.g. KXBTC15M-26JUL231400)
      2. status=open series markets whose open/close contain now
    Never returns finalized/settled markets from older windows.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    event_ticker = expected_event_ticker(series_ticker=series_ticker, now=now)

    # 1) Exact event for this 15m URL/window
    try:
        by_event = fetch_markets(
            event_ticker=event_ticker,
            status=None,
            limit=10,
            base_url=base_url,
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Kalshi event fetch failed (%s): %s", event_ticker, exc)
        by_event = []

    parsed_event = [_from_market(m) for m in by_event]
    # Ignore finalized leftovers if API ever returns mixed
    live_event = [
        m
        for m in parsed_event
        if (m.status or "").lower() in {"active", "open", "initialized", ""}
        or (m.open_time and m.close_time and m.open_time <= now < m.close_time)
    ]
    chosen = _pick_active(live_event or parsed_event, now)
    if chosen is not None and chosen.strike is not None:
        logger.info(
            "Kalshi window %s strike=%s yes=%s/%s",
            chosen.event_ticker,
            chosen.strike,
            chosen.yes_bid,
            chosen.yes_ask,
        )
        return chosen
    if chosen is not None:
        # Window exists but strike still TBD
        return chosen

    # 2) Fallback: currently open markets in the series
    try:
        open_markets = fetch_markets(
            series_ticker=series_ticker, status="open", base_url=base_url
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Kalshi open-market fetch failed: %s", exc)
        return None

    parsed_open = [_from_market(m) for m in open_markets]
    chosen = _pick_active(parsed_open, now)
    if chosen is not None:
        return chosen

    # If an open market exists but clock skew, take the only open one with a strike
    with_strike = [m for m in parsed_open if m.strike is not None]
    if len(with_strike) == 1:
        return with_strike[0]
    if with_strike:
        with_strike.sort(key=lambda m: abs((m.close_time or now) - now).total_seconds())
        return with_strike[0]

    logger.warning("No active Kalshi BTC 15m market found for %s", event_ticker)
    return None
