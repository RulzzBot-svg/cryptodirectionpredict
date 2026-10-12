"""Turn model probabilities into ABOVE / BELOW / SKIP advice."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Literal, Optional, Union

from config.kalshi_fees import fee_per_contract, net_edge

from .probability import ProbabilityEstimate, estimate_prob_above
from .window import PredictionWindow

Side = Literal["ABOVE", "BELOW", "SKIP"]
VALID_SIDES = frozenset({"ABOVE", "BELOW"})
# Live taker autopsy (Sep 27–Oct 12): ABOVE 46% / -$11. Default drops that leg.
# Set ALLOWED_SIDES=ABOVE,BELOW to put it back. LIVE_TRADING is a separate knob.
DEFAULT_ALLOWED_SIDES = frozenset({"BELOW"})


def parse_allowed_sides(
    raw: Optional[Union[str, Iterable[str]]] = None,
) -> frozenset[str]:
    """Which contract legs may fire. Default BELOW-only after the live ABOVE bleed.

    Blank / garbage env falls back to BELOW so a missing Render var cannot
    silently re-arm ABOVE. Explicit ``ALL`` / ``ABOVE,BELOW`` restores both.
    """
    if raw is None:
        raw = os.getenv("ALLOWED_SIDES", "BELOW")
    if isinstance(raw, (set, frozenset, list, tuple)):
        chunks = [str(item) for item in raw]
    else:
        chunks = (
            str(raw)
            .replace(";", ",")
            .replace("|", ",")
            .replace("/", ",")
            .split(",")
        )
    tokens = [chunk.strip().upper() for chunk in chunks if chunk.strip()]
    if any(token in {"ALL", "BOTH", "*", "ANY"} for token in tokens):
        return frozenset(VALID_SIDES)
    sides: set[str] = set()
    for token in tokens:
        if token in {"YES", "Y"}:
            token = "ABOVE"
        elif token in {"NO", "N"}:
            token = "BELOW"
        if token in VALID_SIDES:
            sides.add(token)
    return frozenset(sides) if sides else frozenset(DEFAULT_ALLOWED_SIDES)


def allowed_sides_label(sides: Iterable[str]) -> str:
    present = frozenset(sides) & VALID_SIDES
    if present == VALID_SIDES:
        return "ABOVE,BELOW"
    if present == frozenset({"ABOVE"}):
        return "ABOVE"
    return "BELOW"


@dataclass(frozen=True)
class Advice:
    action: Side
    prob_above: float
    prob_below: float
    edge: float
    fair_yes_cents: float
    fair_no_cents: float
    reason: str
    estimate: ProbabilityEstimate
    # Tradable share prices used for this decision (None when unavailable)
    yes_ask: Optional[float] = None
    no_ask: Optional[float] = None

    @property
    def should_bet(self) -> bool:
        return self.action in ("ABOVE", "BELOW")

    @property
    def entry_share_price(self) -> Optional[float]:
        """What you actually pay per $1 face for the recommended side."""
        if self.action == "ABOVE":
            return self.yes_ask
        if self.action == "BELOW":
            return self.no_ask
        return None


class PredictionAdvisor:
    """
    Recommend a side when model edge vs live share asks is large enough.

    Edge is measured against the price you would pay, after estimated fees:
      ABOVE edge = P(above) - yes_ask - fee/contract
      BELOW edge = P(below) - no_ask - fee/contract

    Optional bounds kill the two buckets that keep showing up as losers:
      - cheap longshots (low ask / low model prob)
      - overconfident favorites (high ask / high model prob)
    Set any bound to 0 to disable it.
    """

    def __init__(
        self,
        *,
        min_edge: float = 0.08,
        market_prob_above: float = 0.50,
        max_seconds_to_bet: Optional[float] = None,
        min_seconds_to_bet: float = 20.0,
        require_tradable_quotes: bool = True,
        min_entry_price: float = 0.0,
        max_entry_price: float = 0.0,
        min_model_prob: float = 0.0,
        max_model_prob: float = 0.0,
        stake_notional: float = 5.0,
        fee_mode: str = "taker",  # taker | maker | none
        allowed_sides: Optional[Union[str, Iterable[str]]] = None,
    ) -> None:
        self.min_edge = min_edge
        self.market_prob_above = market_prob_above
        self.max_seconds_to_bet = max_seconds_to_bet
        self.min_seconds_to_bet = min_seconds_to_bet
        self.require_tradable_quotes = require_tradable_quotes
        self.min_entry_price = float(min_entry_price or 0.0)
        self.max_entry_price = float(max_entry_price or 0.0)
        self.min_model_prob = float(min_model_prob or 0.0)
        self.max_model_prob = float(max_model_prob or 0.0)
        self.stake_notional = max(float(stake_notional), 0.01)
        self.fee_mode = (fee_mode or "taker").strip().lower()
        self.allowed_sides = parse_allowed_sides(allowed_sides)

    @staticmethod
    def _valid_ask(value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        price = float(value)
        # Refuse empty books / extremes that produce fake 1¢ edges
        if price < 0.02 or price > 0.98:
            return None
        return price

    def _fee_drag(self, price: float) -> float:
        if self.fee_mode in {"", "none", "off", "0"}:
            return 0.0
        maker = self.fee_mode == "maker"
        return fee_per_contract(price, self.stake_notional, maker=maker)

    def _net_edge(self, model_prob: float, price: float) -> float:
        if self.fee_mode in {"", "none", "off", "0"}:
            return float(model_prob) - float(price)
        return net_edge(
            model_prob,
            price,
            self.stake_notional,
            maker=self.fee_mode == "maker",
        )

    def _filter_skip_reason(self, *, model_prob: float, entry: float) -> Optional[str]:
        """Return a human skip reason when a candidate bet is out of bounds."""
        if self.min_entry_price > 0 and entry < self.min_entry_price:
            return (
                f"ask {entry * 100:.1f}¢ below min "
                f"{self.min_entry_price * 100:.0f}¢"
            )
        if self.max_entry_price > 0 and entry > self.max_entry_price:
            return (
                f"ask {entry * 100:.1f}¢ above max "
                f"{self.max_entry_price * 100:.0f}¢"
            )
        if self.min_model_prob > 0 and model_prob < self.min_model_prob:
            return (
                f"model {model_prob * 100:.1f}% below min "
                f"{self.min_model_prob * 100:.0f}%"
            )
        if self.max_model_prob > 0 and model_prob > self.max_model_prob:
            return (
                f"model {model_prob * 100:.1f}% above max "
                f"{self.max_model_prob * 100:.0f}%"
            )
        return None

    def _skip(
        self,
        estimate: ProbabilityEstimate,
        *,
        edge: float,
        reason: str,
        yes: Optional[float],
        no: Optional[float],
    ) -> Advice:
        return Advice(
            action="SKIP",
            prob_above=estimate.prob_above,
            prob_below=estimate.prob_below,
            edge=edge,
            fair_yes_cents=estimate.prob_above * 100.0,
            fair_no_cents=estimate.prob_below * 100.0,
            reason=reason,
            estimate=estimate,
            yes_ask=yes,
            no_ask=no,
        )

    def advise(
        self,
        window: PredictionWindow,
        spot: float,
        candles,
        *,
        market_prob_above: Optional[float] = None,
        yes_ask: Optional[float] = None,
        no_ask: Optional[float] = None,
    ) -> Advice:
        if window.strike is None:
            raise ValueError("window strike is not locked yet")

        estimate = estimate_prob_above(
            spot=spot,
            strike=window.strike,
            seconds_remaining=window.seconds_remaining(),
            candles=candles,
        )

        # Prefer explicit asks; else derive from a single YES mid/prob reference
        yes = self._valid_ask(yes_ask)
        no = self._valid_ask(no_ask)
        if yes is None and no is None:
            # With no book at all, the reference price is a guess, not a quote.
            # Inventing one here would manufacture enormous fake edges near
            # expiry, so only fall back when quotes aren't required (manual mode).
            if not self.require_tradable_quotes:
                mkt = (
                    self.market_prob_above
                    if market_prob_above is None
                    else float(market_prob_above)
                )
                yes = self._valid_ask(mkt)
                if yes is not None:
                    no = self._valid_ask(1.0 - yes)
        elif yes is None and no is not None:
            yes = self._valid_ask(1.0 - no)
        elif no is None and yes is not None:
            no = self._valid_ask(1.0 - yes)

        remaining = estimate.seconds_remaining
        if remaining < self.min_seconds_to_bet:
            return self._skip(
                estimate,
                edge=0.0,
                reason=f"too close to expiry ({remaining:.0f}s left)",
                yes=yes,
                no=no,
            )
        if self.max_seconds_to_bet is not None and remaining > self.max_seconds_to_bet:
            return self._skip(
                estimate,
                edge=0.0,
                reason="waiting for more of the window to elapse",
                yes=yes,
                no=no,
            )

        if self.require_tradable_quotes and (yes is None or no is None):
            return self._skip(
                estimate,
                edge=0.0,
                reason="no tradable YES/NO ask yet (skipping empty/0¢ book)",
                yes=yes,
                no=no,
            )

        assert yes is not None and no is not None
        edge_above = self._net_edge(estimate.prob_above, yes)
        edge_below = self._net_edge(estimate.prob_below, no)

        ranked: list[tuple[float, Side, float, float]] = []
        if "ABOVE" in self.allowed_sides:
            ranked.append((edge_above, "ABOVE", estimate.prob_above, yes))
        if "BELOW" in self.allowed_sides:
            ranked.append((edge_below, "BELOW", estimate.prob_below, no))
        ranked.sort(key=lambda row: row[0], reverse=True)

        disabled_note = ""
        if "ABOVE" not in self.allowed_sides and edge_above >= self.min_edge:
            disabled_note = f"ABOVE disabled ({edge_above * 100:.1f}¢ edge). "
        elif "BELOW" not in self.allowed_sides and edge_below >= self.min_edge:
            disabled_note = f"BELOW disabled ({edge_below * 100:.1f}¢ edge). "

        blocked_reasons: list[str] = []
        for edge, action, model_prob, entry in ranked:
            if edge < self.min_edge:
                continue
            blocked = self._filter_skip_reason(model_prob=model_prob, entry=entry)
            if blocked is not None:
                blocked_reasons.append(f"{action}: {blocked}")
                continue
            if action == "ABOVE":
                fee_cents = self._fee_drag(yes) * 100.0
                reason = (
                    f"model {estimate.prob_above_pct:.1f}% ABOVE vs YES ask "
                    f"{yes * 100:.1f}¢ (net edge {edge_above * 100:.1f}¢"
                    f"{f', fee ~{fee_cents:.1f}¢' if fee_cents > 0 else ''})"
                )
            else:
                fee_cents = self._fee_drag(no) * 100.0
                reason = (
                    f"model {estimate.prob_below_pct:.1f}% BELOW vs NO ask "
                    f"{no * 100:.1f}¢ (net edge {edge_below * 100:.1f}¢"
                    f"{f', fee ~{fee_cents:.1f}¢' if fee_cents > 0 else ''})"
                )
            return Advice(
                action=action,
                prob_above=estimate.prob_above,
                prob_below=estimate.prob_below,
                edge=edge,
                fair_yes_cents=estimate.prob_above * 100.0,
                fair_no_cents=estimate.prob_below * 100.0,
                reason=reason,
                estimate=estimate,
                yes_ask=yes,
                no_ask=no,
            )

        if blocked_reasons:
            return self._skip(
                estimate,
                edge=max((row[0] for row in ranked), default=0.0),
                reason=f"{disabled_note}skip {'; '.join(blocked_reasons)}".strip(),
                yes=yes,
                no=no,
            )

        best_edge = max((row[0] for row in ranked), default=max(edge_above, edge_below))
        return self._skip(
            estimate,
            edge=best_edge,
            reason=(
                f"{disabled_note}no net edge (best {best_edge * 100:+.1f}¢ after fees, need "
                f"{self.min_edge * 100:.0f}¢)"
            ).strip(),
            yes=yes,
            no=no,
        )
