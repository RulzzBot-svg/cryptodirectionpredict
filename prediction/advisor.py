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

    @property
    def should_bet(self) -> bool:
        return self.action in ("ABOVE", "BELOW")


class PredictionAdvisor:
    """
    Recommend a side when model edge vs a reference market price is large enough.

    By default the reference market is an inefficient 50/50 book (``market_prob_above=0.5``),
    which matches many retail UIs before prices move. Override when you have live odds.
    """

    def __init__(
        self,
        *,
        min_edge: float = 0.08,
        market_prob_above: float = 0.50,
        max_seconds_to_bet: Optional[float] = None,
        min_seconds_to_bet: float = 20.0,
    ) -> None:
        self.min_edge = min_edge
        self.market_prob_above = market_prob_above
        self.max_seconds_to_bet = max_seconds_to_bet
        self.min_seconds_to_bet = min_seconds_to_bet

    def advise(
        self,
        window: PredictionWindow,
        spot: float,
        candles,
        *,
        market_prob_above: Optional[float] = None,
    ) -> Advice:
        if window.strike is None:
            raise ValueError("window strike is not locked yet")

        estimate = estimate_prob_above(
            spot=spot,
            strike=window.strike,
            seconds_remaining=window.seconds_remaining(),
            candles=candles,
        )
        mkt = (
            self.market_prob_above
            if market_prob_above is None
            else float(market_prob_above)
        )
        edge_above = estimate.prob_above - mkt
        edge_below = estimate.prob_below - (1.0 - mkt)

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
            )

        if edge_above >= self.min_edge and edge_above >= edge_below:
            return Advice(
                action="ABOVE",
                prob_above=estimate.prob_above,
                prob_below=estimate.prob_below,
                edge=edge_above,
                fair_yes_cents=estimate.prob_above * 100.0,
                fair_no_cents=estimate.prob_below * 100.0,
                reason=(
                    f"model {estimate.prob_above_pct:.1f}% ABOVE vs market "
                    f"{mkt * 100:.1f}% (edge {edge_above * 100:.1f}¢)"
                ),
                estimate=estimate,
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
                    f"model {estimate.prob_below_pct:.1f}% BELOW vs market "
                    f"{(1.0 - mkt) * 100:.1f}% (edge {edge_below * 100:.1f}¢)"
                ),
                estimate=estimate,
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
        )
