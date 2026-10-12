#!/usr/bin/env python3
"""ALLOWED_SIDES drops the live ABOVE bleed without flipping the other knobs."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from prediction.advisor import (
    PredictionAdvisor,
    allowed_sides_label,
    parse_allowed_sides,
)
from prediction.probability import ProbabilityEstimate
from prediction.window import PredictionWindow


def _window() -> PredictionWindow:
    start = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)
    return PredictionWindow(
        start=start,
        end=start + timedelta(minutes=15),
        strike=100_000.0,
    )


def _estimate(p_above: float) -> ProbabilityEstimate:
    p = float(p_above)
    return ProbabilityEstimate(
        spot=100_000.0,
        strike=100_000.0,
        seconds_remaining=400.0,
        sigma_per_sqrt_second=1e-6,
        annualized_vol=0.4,
        prob_above=p,
        prob_below=1.0 - p,
        distance_pct=0.0,
        moneyness="ATM",
    )


def _advise(
    *,
    p_above: float,
    yes: float,
    no: float,
    allowed: str,
    min_edge: float = 0.08,
) -> object:
    advisor = PredictionAdvisor(
        min_edge=min_edge,
        min_seconds_to_bet=20.0,
        require_tradable_quotes=True,
        stake_notional=5.0,
        fee_mode="none",
        allowed_sides=allowed,
    )
    with patch(
        "prediction.advisor.estimate_prob_above",
        return_value=_estimate(p_above),
    ):
        return advisor.advise(_window(), spot=100_000.0, candles=None, yes_ask=yes, no_ask=no)


def test_parse_allowed_sides() -> None:
    assert parse_allowed_sides("BELOW") == frozenset({"BELOW"})
    assert parse_allowed_sides("no") == frozenset({"BELOW"})
    assert parse_allowed_sides("YES,BELOW") == frozenset({"ABOVE", "BELOW"})
    assert parse_allowed_sides("ALL") == frozenset({"ABOVE", "BELOW"})
    assert parse_allowed_sides("") == frozenset({"BELOW"})
    assert parse_allowed_sides("garbage") == frozenset({"BELOW"})
    assert allowed_sides_label(parse_allowed_sides("BELOW")) == "BELOW"
    assert allowed_sides_label(parse_allowed_sides("ABOVE,BELOW")) == "ABOVE,BELOW"
    old = os.environ.pop("ALLOWED_SIDES", None)
    try:
        assert parse_allowed_sides() == frozenset({"BELOW"})
    finally:
        if old is None:
            os.environ.pop("ALLOWED_SIDES", None)
        else:
            os.environ["ALLOWED_SIDES"] = old


def test_below_only_skips_above_edge_without_flipping_the_other_leg() -> None:
    # Model likes ABOVE by 12¢; BELOW is -12¢. Disabling ABOVE must SKIP,
    # not invent a BELOW ticket.
    both = _advise(p_above=0.75, yes=0.63, no=0.37, allowed="ABOVE,BELOW")
    assert both.action == "ABOVE"
    assert abs(both.edge - 0.12) < 1e-9

    below_only = _advise(p_above=0.75, yes=0.63, no=0.37, allowed="BELOW")
    assert below_only.action == "SKIP"
    assert "ABOVE disabled" in below_only.reason
    assert "no net edge" in below_only.reason


def test_unset_env_defaults_advisor_to_below_only() -> None:
    old = os.environ.pop("ALLOWED_SIDES", None)
    try:
        advisor = PredictionAdvisor(
            min_edge=0.08,
            min_seconds_to_bet=20.0,
            require_tradable_quotes=True,
            stake_notional=5.0,
            fee_mode="none",
        )
        with patch(
            "prediction.advisor.estimate_prob_above",
            return_value=_estimate(0.75),
        ):
            advice = advisor.advise(
                _window(), spot=100_000.0, candles=None, yes_ask=0.63, no_ask=0.37
            )
        assert advisor.allowed_sides == frozenset({"BELOW"})
        assert advice.action == "SKIP"
        assert "ABOVE disabled" in advice.reason
    finally:
        if old is None:
            os.environ.pop("ALLOWED_SIDES", None)
        else:
            os.environ["ALLOWED_SIDES"] = old


def test_below_only_still_takes_a_real_below_edge() -> None:
    advice = _advise(p_above=0.28, yes=0.40, no=0.55, allowed="BELOW")
    assert advice.action == "BELOW"
    assert abs(advice.edge - 0.17) < 1e-9


def test_disabled_side_does_not_steal_a_weaker_allowed_leg() -> None:
    # Crossed/wide book: ABOVE +15¢, BELOW +10¢. BELOW-only still takes BELOW.
    advice = _advise(p_above=0.55, yes=0.40, no=0.35, allowed="BELOW")
    assert advice.action == "BELOW"
    assert abs(advice.edge - 0.10) < 1e-9
    above_only = _advise(p_above=0.55, yes=0.40, no=0.35, allowed="ABOVE")
    assert above_only.action == "ABOVE"
    assert abs(above_only.edge - 0.15) < 1e-9


if __name__ == "__main__":
    test_parse_allowed_sides()
    test_below_only_skips_above_edge_without_flipping_the_other_leg()
    test_unset_env_defaults_advisor_to_below_only()
    test_below_only_still_takes_a_real_below_edge()
    test_disabled_side_does_not_steal_a_weaker_allowed_leg()
    print("allowed-sides tests ok")
