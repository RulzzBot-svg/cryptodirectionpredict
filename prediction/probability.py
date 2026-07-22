"""Binary above/below probability from spot, strike, time, and volatility."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True)
class ProbabilityEstimate:
    spot: float
    strike: float
    seconds_remaining: float
    sigma_per_sqrt_second: float
    annualized_vol: float
    prob_above: float
    prob_below: float
    distance_pct: float
    moneyness: str  # ITM_ABOVE | ITM_BELOW | ATM

    @property
    def prob_above_pct(self) -> float:
        return self.prob_above * 100.0

    @property
    def prob_below_pct(self) -> float:
        return self.prob_below * 100.0


def realized_vol_per_sqrt_second(
    candles: pd.DataFrame,
    *,
    min_bars: int = 20,
    fallback_annual_vol: float = 0.60,
) -> float:
    """
    Estimate σ such that variance over ``t`` seconds ≈ (σ_per_sqrt_second ** 2) * t.

    Uses log-returns of candle closes and scales by median bar duration.
    """
    seconds_per_year = 365.25 * 24 * 3600
    fallback = fallback_annual_vol / math.sqrt(seconds_per_year)

    if candles is None or candles.empty or "close" not in candles.columns:
        return fallback

    closes = pd.to_numeric(candles["close"], errors="coerce").dropna()
    if len(closes) < min_bars + 1:
        return fallback

    log_returns = np.log(closes / closes.shift(1)).dropna()
    if log_returns.empty:
        return fallback

    # Infer bar length from index when possible; default 15m
    bar_seconds = 15 * 60
    if isinstance(closes.index, pd.DatetimeIndex) and len(closes.index) >= 2:
        deltas = closes.index.to_series().diff().dt.total_seconds().dropna()
        if not deltas.empty:
            median = float(deltas.median())
            if median > 0:
                bar_seconds = median

    sigma_bar = float(log_returns.tail(100).std(ddof=1))
    if not math.isfinite(sigma_bar) or sigma_bar <= 0:
        return fallback

    return sigma_bar / math.sqrt(bar_seconds)


def estimate_prob_above(
    spot: float,
    strike: float,
    seconds_remaining: float,
    candles: Optional[pd.DataFrame] = None,
    *,
    sigma_per_sqrt_second: Optional[float] = None,
) -> ProbabilityEstimate:
    """
    Probability that spot finishes above strike at expiry.

    Uses a driftless lognormal model:
        P(S_T > K) = N(d2),  d2 = (ln(S/K) - 0.5 σ² τ) / (σ √τ)
    """
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")

    sigma = (
        float(sigma_per_sqrt_second)
        if sigma_per_sqrt_second is not None
        else realized_vol_per_sqrt_second(candles if candles is not None else pd.DataFrame())
    )
    sigma = max(sigma, 1e-12)
    tau = max(float(seconds_remaining), 0.0)
    distance_pct = (spot - strike) / strike * 100.0

    if tau <= 1e-9:
        if spot > strike:
            p_above = 1.0
        elif spot < strike:
            p_above = 0.0
        else:
            p_above = 0.5
    else:
        vol_term = sigma * math.sqrt(tau)
        d2 = (math.log(spot / strike) - 0.5 * (sigma**2) * tau) / vol_term
        p_above = _norm_cdf(d2)

    p_above = min(1.0, max(0.0, float(p_above)))
    p_below = 1.0 - p_above

    if spot > strike * 1.0005:
        moneyness = "ITM_ABOVE"
    elif spot < strike * 0.9995:
        moneyness = "ITM_BELOW"
    else:
        moneyness = "ATM"

    seconds_per_year = 365.25 * 24 * 3600
    annualized = sigma * math.sqrt(seconds_per_year)

    return ProbabilityEstimate(
        spot=float(spot),
        strike=float(strike),
        seconds_remaining=tau,
        sigma_per_sqrt_second=sigma,
        annualized_vol=annualized,
        prob_above=p_above,
        prob_below=p_below,
        distance_pct=distance_pct,
        moneyness=moneyness,
    )
