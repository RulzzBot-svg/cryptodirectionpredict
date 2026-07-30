"""Live BTC price held in memory, updated by a background WebSocket stream.

The bot's edge is supposed to come from Kalshi lagging the spot market. That
only works if our own view of spot is fresher than Kalshi's. Polling REST every
10 seconds means acting on a price that can be seconds old — old enough that
the "mispricing" is our staleness rather than theirs.

This keeps the latest trade price in memory and reports how old it is, so
callers can prefer it over a REST snapshot and fall back when it goes stale.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_SECONDS = 3.0


class PriceTape:
    """Background task maintaining the most recent spot price."""

    def __init__(
        self,
        symbol: str,
        *,
        provider: Optional[str] = None,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    ) -> None:
        self.symbol = symbol
        self.provider = (provider or os.getenv("DATA_PROVIDER", "coinbase")).strip().lower()
        self.max_age_seconds = float(max_age_seconds)
        self._price: Optional[float] = None
        self._updated_at: float = 0.0
        self._task: Optional[asyncio.Task] = None
        self._exchange: Any = None
        self._stop = False
        self.updates = 0
        self.transport = "none"

    @property
    def price(self) -> Optional[float]:
        return self._price

    @property
    def age_seconds(self) -> Optional[float]:
        if self._updated_at <= 0:
            return None
        return time.time() - self._updated_at

    @property
    def fresh(self) -> bool:
        age = self.age_seconds
        return self._price is not None and age is not None and age <= self.max_age_seconds

    def fresh_price(self) -> Optional[float]:
        """Latest price, or None when too stale to trust."""
        return self._price if self.fresh else None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop = False
        self._task = asyncio.create_task(self._run(), name="price-tape")

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self._close_exchange()

    async def _close_exchange(self) -> None:
        exchange, self._exchange = self._exchange, None
        if exchange is None:
            return
        close = getattr(exchange, "close", None)
        if close is None:
            return
        try:
            await close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Price tape close failed: %s", exc)

    def _record(self, ticker: dict[str, Any]) -> None:
        price = ticker.get("last") or ticker.get("close")
        if price is None:
            bid, ask = ticker.get("bid"), ticker.get("ask")
            if bid and ask:
                price = (float(bid) + float(ask)) / 2.0
        if price is None:
            return
        try:
            self._price = float(price)
        except (TypeError, ValueError):
            return
        self._updated_at = time.time()
        self.updates += 1

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop:
            try:
                await self._close_exchange()
                self._exchange = self._make_exchange(use_pro=True)
                if hasattr(self._exchange, "watch_ticker"):
                    self.transport = "websocket"
                    logger.info("Price tape streaming %s over WebSocket", self.symbol)
                    while not self._stop:
                        ticker = await self._exchange.watch_ticker(self.symbol)
                        self._record(ticker)
                        backoff = 1.0
                else:
                    raise AttributeError("watch_ticker unavailable")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — must keep the tape alive
                logger.warning(
                    "Price tape stream failed (%s); polling REST for %.0fs",
                    exc,
                    backoff,
                )
                await self._poll_rest_for(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _poll_rest_for(self, seconds: float) -> None:
        """Keep the tape warm over REST while the stream is unavailable."""
        self.transport = "rest-fallback"
        deadline = time.time() + max(1.0, seconds)
        exchange = None
        try:
            exchange = self._make_exchange(use_pro=False)
            while not self._stop and time.time() < deadline:
                try:
                    self._record(await exchange.fetch_ticker(self.symbol))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Price tape REST poll failed: %s", exc)
                await asyncio.sleep(1.0)
        finally:
            if exchange is not None:
                close = getattr(exchange, "close", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:  # noqa: BLE001
                        pass

    def _make_exchange(self, *, use_pro: bool) -> Any:
        if use_pro:
            import ccxt.pro as ccxtpro

            cls = getattr(ccxtpro, self.provider, None)
            if cls is None:
                raise AttributeError(f"ccxt.pro has no exchange '{self.provider}'")
            return cls({"enableRateLimit": True})

        import ccxt.async_support as ccxt_async

        cls = getattr(ccxt_async, self.provider, None)
        if cls is None:
            raise AttributeError(f"ccxt.async_support has no exchange '{self.provider}'")
        return cls({"enableRateLimit": True})
