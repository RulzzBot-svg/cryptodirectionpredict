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


@dataclass(frozen=True)
class KalshiBook:
    """Best executable prices from the real orderbook, with available size.

    Kalshi's book lists *bids* on both legs. Buying YES means lifting the best
    NO bid at its complement, and vice versa — so the true ask for one side is
    ``1 - best_bid`` of the other.
    """

    ticker: str
    yes_ask: Optional[float]
    yes_ask_depth: float
    no_ask: Optional[float]
    no_ask_depth: float
    raw: dict[str, Any]
    yes_bid: Optional[float] = None
    no_bid: Optional[float] = None

    def ask_for(self, side: str) -> Optional[float]:
        """Price to buy this side right now (crossing the spread)."""
        return self.yes_ask if side == "ABOVE" else self.no_ask

    def bid_for(self, side: str) -> Optional[float]:
        """Best resting bid for this side — where a maker order must sit."""
        return self.yes_bid if side == "ABOVE" else self.no_bid

    def depth_for(self, side: str) -> float:
        return self.yes_ask_depth if side == "ABOVE" else self.no_ask_depth


def _book_price(raw: Any) -> Optional[float]:
    """Normalize a book price to dollars.

    Kalshi returns cents in the classic shape (``17``) and fixed-point dollar
    strings in the ``_fp`` shape (``"0.1700"``). Anything at or above 1 must be
    cents, since a contract can never be worth a dollar or more.
    """
    price = _parse_float(raw)
    if price is None or price <= 0:
        return None
    return price / 100.0 if price >= 1.0 else price


def _best_bid(levels: Any) -> tuple[Optional[float], float]:
    """Highest bid and its size from one side of a Kalshi book.

    Accepts the documented ``[[price_cents, count], ...]`` shape as well as the
    dict form some responses use, so a format change degrades to "unknown"
    rather than silently reporting an empty book.
    """
    best_price: Optional[float] = None
    best_size = 0.0
    if isinstance(levels, dict):
        levels = levels.get("levels") or levels.get("orders") or []
    for level in levels or []:
        price = size = None
        if isinstance(level, (list, tuple)) and len(level) >= 2:
            price = _book_price(level[0])
            size = _parse_float(level[1])
        elif isinstance(level, dict):
            price = _book_price(
                level.get("price", level.get("price_cents", level.get("p")))
            )
            size = _parse_float(
                level.get("count", level.get("quantity", level.get("size", level.get("q"))))
            )
        if price is None or size is None or size <= 0:
            continue
        if best_price is None or price > best_price:
            best_price = price
            best_size = size
    return best_price, best_size


def fetch_orderbook(
    ticker: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    depth: int = 10,
    timeout: float = 8.0,
) -> Optional[KalshiBook]:
    """Live orderbook for one market. Returns None if it can't be read."""
    if not ticker:
        return None
    url = f"{base_url.rstrip('/')}/markets/{urllib.parse.quote(ticker)}/orderbook"
    if depth:
        url = f"{url}?depth={int(depth)}"
    try:
        payload = _request_json(url, timeout=timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Kalshi orderbook fetch failed (%s): %s", ticker, exc)
        return None

    # Kalshi serves the classic `orderbook` (cents) and the fixed-point
    # `orderbook_fp` (dollar strings) shapes depending on the market.
    book = payload.get("orderbook") or payload.get("orderbook_fp") or payload or {}

    def _side(*names: str) -> Any:
        for name in names:
            if isinstance(book, dict) and book.get(name):
                return book[name]
        return None

    yes_bid, yes_bid_size = _best_bid(_side("yes", "yes_dollars", "yes_fp"))
    no_bid, no_bid_size = _best_bid(_side("no", "no_dollars", "no_fp"))
    if yes_bid is None and no_bid is None:
        # Never seen in testing; log the shape once so a format change is
        # diagnosable instead of looking like an empty market.
        logger.warning(
            "Kalshi orderbook for %s parsed as empty; keys=%s sample=%.200s",
            ticker,
            list(book.keys()) if isinstance(book, dict) else type(book).__name__,
            json.dumps(payload)[:200],
        )

    # Buying YES lifts the best NO bid at its complement (prices are in dollars)
    yes_ask = 1.0 - no_bid if no_bid is not None else None
    no_ask = 1.0 - yes_bid if yes_bid is not None else None

    def _sane(price: Optional[float]) -> Optional[float]:
        if price is None or price <= 0.0 or price >= 1.0:
            return None
        return price

    return KalshiBook(
        ticker=ticker,
        yes_ask=_sane(yes_ask),
        yes_ask_depth=no_bid_size,
        no_ask=_sane(no_ask),
        no_ask_depth=yes_bid_size,
        raw=payload,
        yes_bid=_sane(yes_bid),
        no_bid=_sane(no_bid),
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


def fetch_markets_for_event(
    event_ticker: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> list[KalshiBtcWindow]:
    try:
        markets = fetch_markets(
            event_ticker=event_ticker,
            status=None,
            limit=10,
            base_url=base_url,
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Kalshi settle fetch failed (%s): %s", event_ticker, exc)
        return []
    return [_from_market(m) for m in markets]


def kalshi_result_side(market: KalshiBtcWindow) -> Optional[str]:
    """
    Map Kalshi market ``result`` to ABOVE/BELOW when determined/finalized.

    YES → ABOVE, NO → BELOW for BTC 15m up/down contracts.
    """
    raw = market.raw or {}
    result = str(raw.get("result") or "").strip().lower()
    if result in {"yes", "y"}:
        return "ABOVE"
    if result in {"no", "n"}:
        return "BELOW"
    # settlement_value_dollars: 1.0 ⇒ YES won, 0.0 ⇒ NO won
    settle = _quote_dollars(raw, "settlement_value_dollars", "settlement_value")
    if settle is None:
        return None
    if settle >= 0.99:
        return "ABOVE"
    if settle <= 0.01:
        return "BELOW"
    return None


def fetch_window_settlement(
    *,
    series_ticker: str = DEFAULT_SERIES,
    window_end: datetime,
    base_url: str = DEFAULT_BASE_URL,
    market_ticker: Optional[str] = None,
) -> tuple[Optional[str], Optional[float], str]:
    """
    Resolve official Kalshi outcome for a just-ended 15m window.

    Returns (side, expiration_value_or_None, source_label).
    """
    # Event ticker is keyed by window END time in ET
    probe = window_end - timedelta(seconds=1)
    event_ticker = expected_event_ticker(series_ticker=series_ticker, now=probe)
    markets = fetch_markets_for_event(event_ticker, base_url=base_url)
    if market_ticker:
        markets = [m for m in markets if m.ticker == market_ticker] or markets
    for market in markets:
        side = kalshi_result_side(market)
        if side is None:
            continue
        exp_raw = (market.raw or {}).get("expiration_value")
        exp_val = _parse_float(exp_raw)
        return side, exp_val, f"kalshi:{market.ticker}:{market.status}"
    return None, None, f"kalshi_pending:{event_ticker}"
