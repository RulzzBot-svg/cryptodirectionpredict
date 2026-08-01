"""Probability from the actual distribution of past returns, not a bell curve.

The lognormal model needs one number, sigma, to describe how BTC moves. Live
data says that can't work: 15-minute returns have excess kurtosis around +16,
meaning a sharp peak with rare violent jumps. Fit sigma to the standard
deviation and the middle is too wide, which overstates the chance of reaching a
nearby strike. Fit it to a robust scale and the middle is right but the tails
vanish, which understates jumps. There is no sigma that gets both.

Using the empirical distribution sidesteps the choice. Past returns already have
the right peak and the right tails, so the only assumption left is how they
scale with time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
import pandas as pd

# Never return a certainty from a finite sample
MIN_PROB = 0.005
MAX_PROB = 0.995
DEFAULT_MIN_SAMPLES = 200


@dataclass(frozen=True)
class EmpiricalFit:
    """Standardized past returns, ready to be rescaled to any horizon."""

    returns: np.ndarray  # log returns, one per bar
    bar_seconds: float
    sample_size: int

    @property
    def scale_15m(self) -> float:
        """Robust width over a 15-minute window, for reporting."""
        if self.returns.size == 0:
            return 0.0
        med = float(np.median(self.returns))
        mad = float(np.median(np.abs(self.returns - med)))
        per_bar = 1.4826 * mad
        return per_bar * math.sqrt((15 * 60) / self.bar_seconds)

    @property
    def excess_kurtosis(self) -> Optional[float]:
        if self.returns.size < 4:
            return None
        sd = float(self.returns.std(ddof=1))
        if sd <= 0:
            return None
        centered = self.returns - float(self.returns.mean())
        return float((centered**4).mean() / sd**4) - 3.0


def fit_returns(
    candles: pd.DataFrame,
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> Optional[EmpiricalFit]:
    """Collect log returns from candle closes, or None if there aren't enough."""
    if candles is None or candles.empty or "close" not in candles.columns:
        return None
    closes = pd.to_numeric(candles["close"], errors="coerce").dropna()
    if len(closes) < min_samples + 1:
        return None

    returns = np.log(closes.to_numpy()[1:] / closes.to_numpy()[:-1])
    returns = returns[np.isfinite(returns)]
    if returns.size < min_samples:
        return None

    bar_seconds = 15.0 * 60.0
    if isinstance(closes.index, pd.DatetimeIndex) and len(closes.index) >= 2:
        deltas = closes.index.to_series().diff().dt.total_seconds().dropna()
        if not deltas.empty:
            median = float(deltas.median())
            if median > 0:
                bar_seconds = median

    # Center on zero: a drift fitted from a few hundred bars is noise, and
    # betting on it would be betting on the recent past repeating.
    returns = returns - float(np.median(returns))
    return EmpiricalFit(
        returns=returns, bar_seconds=bar_seconds, sample_size=int(returns.size)
    )


def prob_above_empirical(
    *,
    spot: float,
    strike: float,
    seconds_remaining: float,
    fit: EmpiricalFit,
    smoothing: float = 0.25,
) -> float:
    """Fraction of past moves that would have finished above the strike.

    Past returns are rescaled by sqrt(time) to the remaining horizon — the same
    assumption the lognormal makes — but their *shape* is kept, so jumps stay as
    likely as they really are.

    ``smoothing`` widens each historical return into a small kernel so a finite
    sample doesn't produce hard 0% or 100% answers near the edges.
    """
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    tau = max(float(seconds_remaining), 0.0)
    if tau <= 1e-9:
        return 1.0 if spot > strike else 0.0 if spot < strike else 0.5

    # The return needed to land exactly on the strike
    needed = math.log(strike / spot)
    scale = math.sqrt(tau / fit.bar_seconds)
    scaled = fit.returns * scale

    if smoothing > 0:
        # Each sample contributes a normal bump rather than a step, so the
        # estimate stays smooth in the tails where samples are sparse.
        band = smoothing * float(np.std(scaled))
        if band > 0:
            z = (scaled - needed) / band
            # P(sample + noise > needed) averaged over samples
            probs = 0.5 * (1.0 + np_erf(z / math.sqrt(2.0)))
            p_above = float(np.mean(probs))
        else:
            p_above = float(np.mean(scaled > needed))
    else:
        p_above = float(np.mean(scaled > needed))

    return min(MAX_PROB, max(MIN_PROB, p_above))


def np_erf(x: np.ndarray) -> np.ndarray:
    """Vectorized erf without pulling in scipy."""
    return np.vectorize(math.erf, otypes=[float])(x)


def describe(fit: EmpiricalFit) -> str:
    kurt = fit.excess_kurtosis
    kurt_txt = f"{kurt:+.1f}" if kurt is not None else "n/a"
    return (
        f"empirical: {fit.sample_size} bars of {fit.bar_seconds:.0f}s, "
        f"15m scale {fit.scale_15m*100:.3f}%, excess kurtosis {kurt_txt}"
    )
