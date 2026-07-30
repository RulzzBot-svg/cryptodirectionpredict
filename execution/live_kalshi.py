"""Live Kalshi order helper (V2 events/orders API).

Builds ABOVE/BELOW IOC orders that mirror paper sizing. Submission is opt-in:
dry-run only logs the payload; live submit requires the live gate to pass.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Optional

from data.kalshi_auth import KalshiAuthClient, KalshiAuthError

logger = logging.getLogger(__name__)

SideName = Literal["ABOVE", "BELOW"]


def _fp_count(contracts: float) -> str:
    return f"{float(contracts):.2f}"


def _fp_price(dollars: float) -> str:
    # Kalshi accepts up to 6 decimals; 4 is plenty for cent markets
    return f"{float(dollars):.4f}"


def _price_to_dollars(raw: Any) -> Optional[float]:
    """Kalshi prices arrive as cents or fixed-point dollar strings."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value / 100.0 if value >= 1.0 else value


def maker_fee(price: float, contracts: float) -> float:
    """Kalshi maker fee for an order: 0.0175 * C * P * (1-P), rounded up."""
    raw = 0.0175 * float(contracts) * price * (1.0 - price)
    return math.ceil(raw * 100.0) / 100.0


@dataclass(frozen=True)
class RestingOrder:
    """A limit order left on the book, waiting to be hit."""

    order_id: str
    client_order_id: str
    window_id: str
    market_ticker: str
    advice_side: SideName
    limit_price: float  # what we pay per contract for our side
    contracts: float
    placed_at: float
    expires_at: float

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at


@dataclass(frozen=True)
class RestingFill:
    """What a resting order actually got."""

    contracts: float
    price_paid: float  # per contract, fee included

    @property
    def cost(self) -> float:
        return self.contracts * self.price_paid


@dataclass(frozen=True)
class LiveOrderPlan:
    """What we would / did send to Kalshi for one window bet."""

    market_ticker: str
    advice_side: SideName
    book_side: Literal["bid", "ask"]
    share_price: float  # price paid for ABOVE/YES or BELOW/NO
    yes_book_price: float  # price field on the YES book
    contracts: float
    client_order_id: str
    payload: dict[str, Any]
    dry_run: bool
    submitted: bool = False
    order_id: Optional[str] = None
    fill_count: float = 0.0
    remaining_count: float = 0.0
    average_fill_price: Optional[float] = None
    average_fee_paid: Optional[float] = None
    raw_response: Optional[dict[str, Any]] = None
    error: Optional[str] = None

    @property
    def filled(self) -> bool:
        return float(self.fill_count) > 0

    @property
    def effective_price(self) -> Optional[float]:
        """Price actually paid per contract for our side, including fee.

        Kalshi V2 quotes every fill from the YES leg, so a BELOW bet — which is
        submitted as *selling* YES — reports ``average_fill_price`` as the YES
        price. What we paid for NO is its complement. Booking the raw number
        would record a 10¢ NO purchase as a 90¢ one.
        """
        if self.average_fill_price is None:
            return None
        fill = float(self.average_fill_price)
        paid = fill if self.advice_side == "ABOVE" else 1.0 - fill
        paid += float(self.average_fee_paid or 0.0)
        if not 0.0 < paid < 1.0:
            logger.error(
                "Implausible fill price for %s %s: fill=%s fee=%s -> %s",
                self.advice_side,
                self.market_ticker,
                self.average_fill_price,
                self.average_fee_paid,
                paid,
            )
            return None
        return paid


class LiveKalshiExecutor:
    """Translate advisor ABOVE/BELOW into Kalshi V2 IOC orders."""

    def __init__(
        self,
        client: Optional[KalshiAuthClient],
        *,
        dry_run: bool = True,
        stake_notional: float = 5.0,
        time_in_force: str = "immediate_or_cancel",
    ) -> None:
        self.client = client
        self.dry_run = bool(dry_run)
        self.stake_notional = max(0.01, float(stake_notional))
        self.time_in_force = time_in_force

    @staticmethod
    def build_payload(
        *,
        market_ticker: str,
        advice_side: SideName,
        share_price: float,
        contracts: float,
        client_order_id: Optional[str] = None,
        time_in_force: str = "immediate_or_cancel",
    ) -> tuple[dict[str, Any], Literal["bid", "ask"], float]:
        """Map prediction side → YES-book bid/ask.

        ABOVE → bid YES at share_price
        BELOW → ask YES at (1 - share_price)  (== buy NO at share_price)
        """
        price = float(share_price)
        if price < 0.02 or price > 0.98:
            raise ValueError(f"share_price out of tradable range: {price}")
        qty = max(0.01, float(contracts))
        coid = client_order_id or str(uuid.uuid4())

        if advice_side == "ABOVE":
            book_side: Literal["bid", "ask"] = "bid"
            yes_price = price
        elif advice_side == "BELOW":
            book_side = "ask"
            yes_price = 1.0 - price
        else:
            raise ValueError(f"unsupported advice_side {advice_side}")

        payload = {
            "ticker": market_ticker,
            "client_order_id": coid,
            "side": book_side,
            "count": _fp_count(qty),
            "price": _fp_price(yes_price),
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
        }
        return payload, book_side, yes_price

    def plan_order(
        self,
        *,
        market_ticker: str,
        advice_side: SideName,
        share_price: float,
        contracts: Optional[float] = None,
    ) -> LiveOrderPlan:
        qty = self.stake_notional if contracts is None else float(contracts)
        payload, book_side, yes_price = self.build_payload(
            market_ticker=market_ticker,
            advice_side=advice_side,
            share_price=share_price,
            contracts=qty,
            time_in_force=self.time_in_force,
        )
        return LiveOrderPlan(
            market_ticker=market_ticker,
            advice_side=advice_side,
            book_side=book_side,
            share_price=float(share_price),
            yes_book_price=yes_price,
            contracts=qty,
            client_order_id=str(payload["client_order_id"]),
            payload=payload,
            dry_run=self.dry_run,
        )

    def place_resting(
        self,
        *,
        market_ticker: str,
        window_id: str,
        advice_side: SideName,
        share_price: float,
        contracts: Optional[float] = None,
        rest_seconds: float = 45.0,
    ) -> Optional[RestingOrder]:
        """Leave a post-only limit order on the book with a short expiry.

        Post-only guarantees maker treatment (a quarter of the taker fee) and
        guarantees we never cross the spread by accident. The expiry keeps the
        order from outliving the model view that justified it.
        """
        qty = self.stake_notional if contracts is None else float(contracts)
        payload, _side, _yes_price = self.build_payload(
            market_ticker=market_ticker,
            advice_side=advice_side,
            share_price=share_price,
            contracts=qty,
            time_in_force="good_till_canceled",
        )
        expires_at = time.time() + max(5.0, float(rest_seconds))
        payload["expiration_time"] = int(expires_at)
        payload["post_only"] = True

        if self.dry_run or self.client is None:
            logger.info("LIVE DRY-RUN resting order: %s", payload)
            return None

        try:
            raw = self.client.create_order_v2(payload)
        except KalshiAuthError as exc:
            logger.error("Resting order failed: %s", exc)
            return None

        order_id = str(raw.get("order_id") or "")
        if not order_id:
            logger.error("Resting order returned no order_id: %s", raw)
            return None

        return RestingOrder(
            order_id=order_id,
            client_order_id=str(payload["client_order_id"]),
            window_id=window_id,
            market_ticker=market_ticker,
            advice_side=advice_side,
            limit_price=float(share_price),
            contracts=qty,
            placed_at=time.time(),
            expires_at=expires_at,
        )

    def poll_resting(self, order: RestingOrder) -> Optional[RestingFill]:
        """How much of a resting order has filled, and at what true cost.

        Returns None when nothing has filled yet or the fills can't be read —
        callers must not treat that as "definitely unfilled" when deciding
        whether to place another order.
        """
        if self.client is None:
            return None
        try:
            payload = self.client.get_fills(
                ticker=order.market_ticker, order_id=order.order_id
            )
        except KalshiAuthError as exc:
            logger.warning("Fill check failed for %s: %s", order.order_id, exc)
            return None

        total = 0.0
        cost = 0.0
        for fill in payload.get("fills") or []:
            if str(fill.get("order_id") or "") != order.order_id:
                continue
            count = fill.get("count", fill.get("count_fp"))
            try:
                qty = float(count)
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            if order.advice_side == "ABOVE":
                price = _price_to_dollars(
                    fill.get("yes_price", fill.get("yes_price_dollars"))
                )
            else:
                price = _price_to_dollars(
                    fill.get("no_price", fill.get("no_price_dollars"))
                )
            if price is None:
                continue
            fee_total = _price_to_dollars(fill.get("fee"))
            if fee_total is None:
                fee_total = maker_fee(price, qty)
            per_contract_fee = fee_total / qty if qty else 0.0
            total += qty
            cost += qty * (price + per_contract_fee)

        if total <= 0:
            return None
        return RestingFill(contracts=total, price_paid=cost / total)

    def cancel_resting(self, order: RestingOrder) -> bool:
        if self.client is None:
            return False
        try:
            self.client.cancel_order_v2(order.order_id)
            return True
        except KalshiAuthError as exc:
            # Already filled or already expired both land here; not an error
            logger.info("Cancel of %s returned: %s", order.order_id, exc)
            return False

    def get_position_count(self, market_ticker: str) -> Optional[float]:
        """Contracts currently held on this market.

        Returns 0.0 when flat, a positive count when holding either leg, and
        ``None`` when the position could not be verified. Callers must treat
        ``None`` as "do not place" — an unverifiable position is the one case
        where retrying could double a live bet.
        """
        if self.client is None:
            return None
        try:
            payload = self.client.get_positions(ticker=market_ticker)
        except KalshiAuthError as exc:
            logger.warning("Position check failed for %s: %s", market_ticker, exc)
            return None

        markets = payload.get("market_positions") or []
        total = 0.0
        for row in markets:
            if str(row.get("ticker") or "") != market_ticker:
                continue
            raw = row.get("position", 0)
            try:
                total += abs(float(raw))
            except (TypeError, ValueError):
                continue
        return total

    def execute(self, plan: LiveOrderPlan) -> LiveOrderPlan:
        """Submit plan unless dry_run. Returns updated plan with fill info."""
        if plan.dry_run or self.dry_run:
            logger.info(
                "LIVE DRY-RUN order | %s %s qty=%s pay≈%.0f¢ ticker=%s payload=%s",
                plan.advice_side,
                plan.book_side,
                plan.contracts,
                plan.share_price * 100,
                plan.market_ticker,
                plan.payload,
            )
            return plan

        if self.client is None:
            return LiveOrderPlan(
                market_ticker=plan.market_ticker,
                advice_side=plan.advice_side,
                book_side=plan.book_side,
                share_price=plan.share_price,
                yes_book_price=plan.yes_book_price,
                contracts=plan.contracts,
                client_order_id=plan.client_order_id,
                payload=plan.payload,
                dry_run=False,
                submitted=False,
                error="no Kalshi auth client",
            )

        try:
            raw = self.client.create_order_v2(plan.payload)
        except KalshiAuthError as exc:
            logger.error("Live order failed: %s", exc)
            return LiveOrderPlan(
                market_ticker=plan.market_ticker,
                advice_side=plan.advice_side,
                book_side=plan.book_side,
                share_price=plan.share_price,
                yes_book_price=plan.yes_book_price,
                contracts=plan.contracts,
                client_order_id=plan.client_order_id,
                payload=plan.payload,
                dry_run=False,
                submitted=False,
                error=str(exc),
            )

        fill = float(raw.get("fill_count") or 0)
        rem = float(raw.get("remaining_count") or 0)
        avg = raw.get("average_fill_price")
        avg_f = float(avg) if avg is not None else None
        fee = raw.get("average_fee_paid")
        fee_f = float(fee) if fee is not None else None
        return LiveOrderPlan(
            market_ticker=plan.market_ticker,
            advice_side=plan.advice_side,
            book_side=plan.book_side,
            share_price=plan.share_price,
            yes_book_price=plan.yes_book_price,
            contracts=plan.contracts,
            client_order_id=plan.client_order_id,
            payload=plan.payload,
            dry_run=False,
            submitted=True,
            order_id=str(raw.get("order_id") or ""),
            fill_count=fill,
            remaining_count=rem,
            average_fill_price=avg_f,
            average_fee_paid=fee_f,
            raw_response=raw,
        )
