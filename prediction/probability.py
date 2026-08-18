"""Binary above/below probability from spot, strike, time, and volatility."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def haircut_factor() -> float:
    """How far to trust the raw model. 1.0 = no haircut; 0.55 ≈ live MAD honesty."""
    raw = os.getenv("PROB_HAIRCUT", "0.55").strip()
    try:
        factor = float(raw)
    except ValueError:
        factor = 0.55
    return min(1.0, max(0.0, factor))


def apply_prob_haircut(p: float, factor: Optional[float] = None) -> float:
    """Shrink a probability toward 50¢.

    Live MAD claimed ~78% and delivered ~67%. A factor of 0.55 maps 78% → 65%,
    so MIN_EDGE sees honest cents instead of fake +12¢. Set PROB_HAIRCUT=1 to
    disable. Does not learn online — one knob, slow to change.
    """
    scale = haircut_factor() if factor is None else min(1.0, max(0.0, float(factor)))
    p = min(1.0, max(0.0, float(p)))
    return 0.5 + (p - 0.5) * scale


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


def _scale_from_returns(log_returns: pd.Series, estimator: str) -> float:
    """Width of a return distribution, by one of two definitions.

    ``std`` is the textbook choice but is dominated by rare large moves. Whether
    price crosses a strike a short distance away is decided by ordinary windows,
    not violent ones, so ``mad`` measures the middle of the distribution instead
    (scaled to match std for a true normal). For BTC's peaked, jump-prone
    15-minute returns the two differ by well over 50%, and using std makes every
    probability sit too close to a coin flip.
    """
    if estimator == "std":
        return float(log_returns.std(ddof=1))
    median = float(log_returns.median())
    mad = float((log_returns - median).abs().median())
    return 1.4826 * mad


def realized_vol_per_sqrt_second(
    candles: pd.DataFrame,
    *,
    min_bars: int = 20,
    fallback_annual_vol: float = 0.60,
    estimator: Optional[str] = None,
) -> float:
    """
    Estimate σ such that variance over ``t`` seconds ≈ (σ_per_sqrt_second ** 2) * t.

    Uses log-returns of candle closes and scales by median bar duration.
    """
    estimator = (
        estimator if estimator is not None else os.getenv("VOL_ESTIMATOR", "std")
    ).strip().lower()
    if estimator not in {"std", "mad"}:
        estimator = "std"
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

    sigma_bar = _scale_from_returns(log_returns.tail(100), estimator)
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

    # BTC's 15m returns are far from normal (excess kurtosis ~+16), so no single
    # sigma describes both the peak and the jumps. When asked, and when there's
    # enough history, use the shape of past returns directly instead.
    model = os.getenv("PROB_MODEL", "lognormal").strip().lower()
    if model == "empirical" and candles is not None and sigma_per_sqrt_second is None:
        from prediction.empirical import fit_returns, prob_above_empirical

        fit = fit_returns(candles)
        if fit is not None:
            p_above = apply_prob_haircut(
                prob_above_empirical(
                    spot=spot, strike=strike, seconds_remaining=tau, fit=fit
                )
            )
            p_below = 1.0 - p_above
            seconds_per_year = 365.25 * 24 * 3600
            # Report the distribution's own robust width so the status line and
            # diagnostics stay comparable across models.
            sigma_report = fit.scale_15m / math.sqrt(15 * 60) if fit.scale_15m else sigma
            return ProbabilityEstimate(
                spot=float(spot),
                strike=float(strike),
                seconds_remaining=tau,
                sigma_per_sqrt_second=sigma_report,
                annualized_vol=sigma_report * math.sqrt(seconds_per_year),
                prob_above=p_above,
                prob_below=p_below,
                distance_pct=distance_pct,
                moneyness=(
                    "ITM_ABOVE"
                    if spot > strike * 1.0005
                    else "ITM_BELOW"
                    if spot < strike * 0.9995
                    else "ATM"
                ),
            )

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

    p_above = apply_prob_haircut(min(1.0, max(0.0, float(p_above))))
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
