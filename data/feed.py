"""Asynchronous BTC market data feed via CCXT (WebSocket or REST polling)."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator, Optional

import pandas as pd
from dotenv import load_dotenv
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

load_dotenv()

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = ("binance", "coinbase")
DEFAULT_OHLCV_LIMIT = 100
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 5.0

OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def _resolve_provider(provider: Optional[str] = None) -> str:
    name = (provider or os.getenv("DATA_PROVIDER", "binance")).strip().lower()
    if name not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported DATA_PROVIDER '{name}'. "
            f"Choose one of: {', '.join(SUPPORTED_PROVIDERS)}"
        )
    return name


def create_rest_exchange(provider: Optional[str] = None):
    """Create an async CCXT REST exchange client for the configured provider."""
    return _create_exchange(_resolve_provider(provider), use_pro=False)


async def close_exchange(exchange: Any) -> None:
    """Close an async CCXT exchange client if needed."""
    await _safe_close(exchange)


def _create_exchange(provider: str, *, use_pro: bool):
    """Instantiate a CCXT (pro) exchange client for public market data."""
    if use_pro:
        try:
            import ccxt.pro as ccxtpro

            exchange_cls = getattr(ccxtpro, provider, None)
            if exchange_cls is None:
                raise AttributeError(f"ccxt.pro has no exchange '{provider}'")
            return exchange_cls({"enableRateLimit": True})
        except Exception as exc:  # noqa: BLE001 — fall back to async REST
            logger.warning(
                "ccxt.pro unavailable for %s (%s); falling back to ccxt.async_support",
                provider,
                exc,
            )

    import ccxt.async_support as ccxt_async

    exchange_cls = getattr(ccxt_async, provider, None)
    if exchange_cls is None:
        raise AttributeError(f"ccxt.async_support has no exchange '{provider}'")
    return exchange_cls({"enableRateLimit": True})


def ohlcv_to_dataframe(ohlcv: list[list[float]]) -> pd.DataFrame:
    """Convert CCXT OHLCV rows into a timestamp-indexed DataFrame."""
    if not ohlcv:
        return pd.DataFrame(columns=OHLCV_COLUMNS).set_index(
            pd.DatetimeIndex([], name="timestamp")
        )

    frame = pd.DataFrame(ohlcv, columns=OHLCV_COLUMNS)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    frame = frame.set_index("timestamp").sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Append short-term technical indicators to an OHLCV DataFrame.

    Indicators:
      - ema_9  — 9-period exponential moving average of close
      - ema_21 — 21-period exponential moving average of close
      - rsi_14 — 14-period relative strength index of close
    """
    if df.empty or "close" not in df.columns:
        result = df.copy()
        result["ema_9"] = pd.Series(dtype="float64")
        result["ema_21"] = pd.Series(dtype="float64")
        result["rsi_14"] = pd.Series(dtype="float64")
        return result

    result = df.copy()
    close = result["close"]
    result["ema_9"] = EMAIndicator(close=close, window=9).ema_indicator()
    result["ema_21"] = EMAIndicator(close=close, window=21).ema_indicator()
    result["rsi_14"] = RSIIndicator(close=close, window=14).rsi()
    return result


def _is_rate_limit_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return (
        "ratelimit" in name
        or "ddos" in name
        or "rate limit" in message
        or "too many requests" in message
        or "429" in message
    )


def _is_network_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    network_tokens = (
        "network",
        "requesttimeout",
        "exchangenotavailable",
        "exchange not available",
        "connection",
        "disconnect",
        "reset by peer",
        "timed out",
        "websocket",
        "restricted location",
        "service unavailable",
    )
    return any(token in name or token in message for token in network_tokens)


def _is_not_supported_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return "notsupported" in name or "is not supported" in message


async def _safe_close(exchange: Any) -> None:
    close = getattr(exchange, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Error closing exchange connection: %s", exc)


async def _fetch_rest(
    exchange: Any,
    symbol: str,
    timeframe: str,
    ohlcv_limit: int,
) -> tuple[dict[str, Any], list[list[float]]]:
    return await asyncio.gather(
        exchange.fetch_ticker(symbol),
        exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=ohlcv_limit),
    )


async def _fetch_hybrid(
    exchange: Any,
    symbol: str,
    timeframe: str,
    ohlcv_limit: int,
) -> tuple[dict[str, Any], list[list[float]]]:
    """Live ticker over WebSocket; OHLCV over REST."""
    return await asyncio.gather(
        exchange.watch_ticker(symbol),
        exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=ohlcv_limit),
    )


async def _fetch_full_ws(
    exchange: Any,
    symbol: str,
    timeframe: str,
    ohlcv_limit: int,
) -> tuple[dict[str, Any], list[list[float]]]:
    ticker_task = asyncio.create_task(exchange.watch_ticker(symbol))
    ohlcv_task = asyncio.create_task(
        exchange.watch_ohlcv(symbol, timeframe=timeframe, limit=ohlcv_limit)
    )
    try:
        return await asyncio.gather(ticker_task, ohlcv_task)
    except Exception:
        for task in (ticker_task, ohlcv_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(ticker_task, ohlcv_task, return_exceptions=True)
        raise


def _build_snapshot(
    symbol: str,
    timeframe: str,
    ticker: dict[str, Any],
    ohlcv: list[list[float]],
    provider: Optional[str],
) -> dict[str, Any]:
    candles = add_indicators(ohlcv_to_dataframe(ohlcv))
    last_price = ticker.get("last")
    if last_price is None:
        last_price = ticker.get("close")
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "ticker": ticker,
        "last_price": last_price,
        "bid": ticker.get("bid"),
        "ask": ticker.get("ask"),
        "candles": candles,
        "provider": provider,
    }


async def fetch_latest_snapshot(
    symbol: Optional[str] = None,
    timeframe: str = "15m",
    *,
    provider: Optional[str] = None,
    ohlcv_limit: int = DEFAULT_OHLCV_LIMIT,
    exchange: Any = None,
) -> dict[str, Any]:
    """
    Fetch a single ticker + OHLCV snapshot over async REST.

    If ``exchange`` is provided it is reused; otherwise a temporary client is
    created and closed before returning.
    """
    resolved_symbol = symbol or os.getenv("SYMBOL", "BTC/USDT")
    resolved_provider = _resolve_provider(provider)
    owns_exchange = exchange is None

    if owns_exchange:
        exchange = _create_exchange(resolved_provider, use_pro=False)

    try:
        ticker, ohlcv = await _fetch_rest(
            exchange, resolved_symbol, timeframe, ohlcv_limit
        )
        return _build_snapshot(
            resolved_symbol,
            timeframe,
            ticker,
            ohlcv,
            getattr(exchange, "id", None),
        )
    finally:
        if owns_exchange:
            await _safe_close(exchange)


async def stream_btc_data(
    symbol: Optional[str] = None,
    timeframe: str = "15m",
    *,
    provider: Optional[str] = None,
    ohlcv_limit: int = DEFAULT_OHLCV_LIMIT,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> AsyncIterator[dict[str, Any]]:
    """
    Stream real-time BTC ticker prices and OHLCV candles with indicators.

    Uses ccxt.pro WebSocket streams when available; otherwise polls via
    ccxt.async_support REST endpoints. Automatically reconnects on
    disconnects and backs off on rate limits.

    Yields
    ------
    dict
        Keys: symbol, timeframe, ticker, last_price, bid, ask, candles, provider.
        ``candles`` is a pandas DataFrame with OHLCV plus ema_9, ema_21, rsi_14.
    """
    resolved_symbol = symbol or os.getenv("SYMBOL", "BTC/USDT")
    resolved_provider = _resolve_provider(provider)

    backoff = INITIAL_BACKOFF_SECONDS
    # Transport modes: "ws" (full websocket), "hybrid" (ticker WS + OHLCV REST), "rest"
    mode = "ws"

    logger.info(
        "Starting market data stream: provider=%s symbol=%s timeframe=%s",
        resolved_provider,
        resolved_symbol,
        timeframe,
    )

    while True:
        exchange = None
        try:
            exchange = _create_exchange(resolved_provider, use_pro=(mode != "rest"))
            if mode == "ws" and not (
                hasattr(exchange, "watch_ticker") and hasattr(exchange, "watch_ohlcv")
            ):
                mode = "rest" if not hasattr(exchange, "watch_ticker") else "hybrid"

            if mode == "ws":
                logger.info("Using WebSocket streams via ccxt.pro (%s)", resolved_provider)
            elif mode == "hybrid":
                logger.info(
                    "Using hybrid watch_ticker + REST OHLCV (%s)", resolved_provider
                )
            else:
                logger.info(
                    "Using async REST polling every %.1fs (%s)",
                    poll_interval,
                    resolved_provider,
                )

            while True:
                try:
                    if mode == "ws":
                        ticker, ohlcv = await _fetch_full_ws(
                            exchange, resolved_symbol, timeframe, ohlcv_limit
                        )
                    elif mode == "hybrid":
                        ticker, ohlcv = await _fetch_hybrid(
                            exchange, resolved_symbol, timeframe, ohlcv_limit
                        )
                    else:
                        ticker, ohlcv = await _fetch_rest(
                            exchange, resolved_symbol, timeframe, ohlcv_limit
                        )
                except Exception as exc:
                    if mode == "ws" and _is_not_supported_error(exc):
                        logger.warning(
                            "Full WebSocket OHLCV unsupported on %s (%s); "
                            "switching to hybrid mode",
                            resolved_provider,
                            exc,
                        )
                        mode = "hybrid"
                        break
                    if mode == "hybrid" and _is_not_supported_error(exc):
                        logger.warning(
                            "WebSocket ticker unsupported on %s (%s); "
                            "switching to REST polling",
                            resolved_provider,
                            exc,
                        )
                        mode = "rest"
                        break
                    raise

                backoff = INITIAL_BACKOFF_SECONDS
                yield _build_snapshot(
                    resolved_symbol,
                    timeframe,
                    ticker,
                    ohlcv,
                    getattr(exchange, "id", None),
                )

                # Full/hybrid watch_ticker blocks until the next tick; REST needs a pause
                if mode == "rest":
                    await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            logger.info("Market data stream cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 — reconnect loop must stay alive
            if _is_rate_limit_error(exc):
                sleep_for = min(max(backoff, 5.0), MAX_BACKOFF_SECONDS)
                logger.warning(
                    "Rate limit hit on %s (%s). Reconnecting in %.1fs",
                    resolved_provider,
                    exc,
                    sleep_for,
                )
            elif _is_network_error(exc):
                sleep_for = min(backoff, MAX_BACKOFF_SECONDS)
                logger.warning(
                    "Network/disconnect error on %s (%s). Reconnecting in %.1fs",
                    resolved_provider,
                    exc,
                    sleep_for,
                )
            else:
                sleep_for = min(backoff, MAX_BACKOFF_SECONDS)
                logger.exception(
                    "Unexpected market data error on %s. Reconnecting in %.1fs",
                    resolved_provider,
                    sleep_for,
                )
                mode = "rest"

            await asyncio.sleep(sleep_for)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        finally:
            await _safe_close(exchange)
