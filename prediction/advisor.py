"""Turn model probabilities into ABOVE / BELOW / SKIP advice."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from .probability import ProbabilityEstimate, estimate_prob_above
from .window import PredictionWindow

Side = Literal["ABOVE", "BELOW", "SKIP"]


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

    Edge is measured against the price you would pay:
      ABOVE edge = P(above) - yes_ask
      BELOW edge = P(below) - no_ask
    """

    def __init__(
        self,
        *,
        min_edge: float = 0.08,
        market_prob_above: float = 0.50,
        max_seconds_to_bet: Optional[float] = None,
        min_seconds_to_bet: float = 20.0,
        require_tradable_quotes: bool = True,
    ) -> None:
        self.min_edge = min_edge
        self.market_prob_above = market_prob_above
        self.max_seconds_to_bet = max_seconds_to_bet
        self.min_seconds_to_bet = min_seconds_to_bet
        self.require_tradable_quotes = require_tradable_quotes

    @staticmethod
    def _valid_ask(value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        price = float(value)
        # Refuse empty books / extremes that produce fake 1¢ edges
        if price < 0.02 or price > 0.98:
            return None
        return price

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
            return Advice(
                action="SKIP",
                prob_above=estimate.prob_above,
                prob_below=estimate.prob_below,
                edge=0.0,
                fair_yes_cents=estimate.prob_above * 100.0,
                fair_no_cents=estimate.prob_below * 100.0,
                reason=f"too close to expiry ({remaining:.0f}s left)",
                estimate=estimate,
                yes_ask=yes,
                no_ask=no,
            )
        if self.max_seconds_to_bet is not None and remaining > self.max_seconds_to_bet:
            return Advice(
                action="SKIP",
                prob_above=estimate.prob_above,
                prob_below=estimate.prob_below,
                edge=0.0,
                fair_yes_cents=estimate.prob_above * 100.0,
                fair_no_cents=estimate.prob_below * 100.0,
                reason="waiting for more of the window to elapse",
                estimate=estimate,
                yes_ask=yes,
                no_ask=no,
            )

        if self.require_tradable_quotes and (yes is None or no is None):
            return Advice(
                action="SKIP",
                prob_above=estimate.prob_above,
                prob_below=estimate.prob_below,
                edge=0.0,
                fair_yes_cents=estimate.prob_above * 100.0,
                fair_no_cents=estimate.prob_below * 100.0,
                reason="no tradable YES/NO ask yet (skipping empty/0¢ book)",
                estimate=estimate,
                yes_ask=yes,
                no_ask=no,
            )

        assert yes is not None and no is not None
        edge_above = estimate.prob_above - yes
        edge_below = estimate.prob_below - no

        if edge_above >= self.min_edge and edge_above >= edge_below:
            return Advice(
                action="ABOVE",
                prob_above=estimate.prob_above,
                prob_below=estimate.prob_below,
                edge=edge_above,
                fair_yes_cents=estimate.prob_above * 100.0,
                fair_no_cents=estimate.prob_below * 100.0,
                reason=(
                    f"model {estimate.prob_above_pct:.1f}% ABOVE vs YES ask "
                    f"{yes * 100:.1f}¢ (edge {edge_above * 100:.1f}¢)"
                ),
                estimate=estimate,
                yes_ask=yes,
                no_ask=no,
            )

        if edge_below >= self.min_edge:
            return Advice(
                action="BELOW",
                prob_above=estimate.prob_above,
                prob_below=estimate.prob_below,
                edge=edge_below,
                fair_yes_cents=estimate.prob_above * 100.0,
                fair_no_cents=estimate.prob_below * 100.0,
                reason=(
                    f"model {estimate.prob_below_pct:.1f}% BELOW vs NO ask "
                    f"{no * 100:.1f}¢ (edge {edge_below * 100:.1f}¢)"
                ),
                estimate=estimate,
                yes_ask=yes,
                no_ask=no,
            )

        best_edge = max(edge_above, edge_below)
        return Advice(
            action="SKIP",
            prob_above=estimate.prob_above,
            prob_below=estimate.prob_below,
            edge=best_edge,
            fair_yes_cents=estimate.prob_above * 100.0,
            fair_no_cents=estimate.prob_below * 100.0,
            reason=(
                f"no edge (best {best_edge * 100:+.1f}¢, need "
                f"{self.min_edge * 100:.0f}¢)"
            ),
            estimate=estimate,
            yes_ask=yes,
            no_ask=no,
        )
