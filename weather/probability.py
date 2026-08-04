"""Map a temperature forecast distribution onto Kalshi strike contracts."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from markets import WeatherContract


@dataclass(frozen=True)
class ContractProb:
    ticker: str
    model_yes: float
    market_yes: float
    edge: float
    label: str


def _integer_pmf_from_members(members_f: Iterable[float]) -> dict[int, float]:
    """
    Kalshi settles on integer °F from NWS CLI.
    Round ensemble floats to nearest int and use the empirical frequencies.
    """
    rounded = [int(round(x)) for x in members_f]
    counts = Counter(rounded)
    total = float(len(rounded))
    return {temp: n / total for temp, n in sorted(counts.items())}


def _normal_pmf(mean: float, sigma: float, temps: range) -> dict[int, float]:
    """Discrete Normal PMF over integer temps (continuity-corrected-ish)."""
    if sigma <= 0:
        sigma = 1.0
    raw: dict[int, float] = {}
    for t in temps:
        # P(t - 0.5 < X <= t + 0.5)
        lo = (t - 0.5 - mean) / sigma
        hi = (t + 0.5 - mean) / sigma
        raw[t] = 0.5 * (math.erf(hi / math.sqrt(2)) - math.erf(lo / math.sqrt(2)))
    s = sum(raw.values())
    if s <= 0:
        return {int(round(mean)): 1.0}
    return {t: p / s for t, p in raw.items()}


def build_temp_pmf(
    members_f: tuple[float, ...],
    blend_sigma_floor: float = 1.5,
) -> dict[int, float]:
    """
    Empirical ensemble PMF, blended with a Normal to avoid zero-prob bins
    when the ensemble is tight.
    """
    emp = _integer_pmf_from_members(members_f)
    mean = sum(members_f) / len(members_f)
    var = sum((x - mean) ** 2 for x in members_f) / max(len(members_f) - 1, 1)
    sigma = max(math.sqrt(var), blend_sigma_floor)
    low = min(emp) - 6
    high = max(emp) + 6
    norm = _normal_pmf(mean, sigma, range(low, high + 1))

    # 70% ensemble / 30% smooth Normal
    keys = sorted(set(emp) | set(norm))
    blended = {t: 0.7 * emp.get(t, 0.0) + 0.3 * norm.get(t, 0.0) for t in keys}
    s = sum(blended.values())
    return {t: p / s for t, p in blended.items()}


def contract_yes_prob(pmf: dict[int, float], contract: WeatherContract) -> float:
    """P(YES) for a Kalshi weather contract under integer settlement."""
    st = contract.strike_type
    if st == "between":
        lo = int(contract.floor_strike)  # type: ignore[arg-type]
        hi = int(contract.cap_strike)  # type: ignore[arg-type]
        return sum(p for t, p in pmf.items() if lo <= t <= hi)
    if st == "greater":
        # YES if temp > floor_strike  (e.g. floor 87 => 88+)
        thr = float(contract.floor_strike)  # type: ignore[arg-type]
        return sum(p for t, p in pmf.items() if t > thr)
    if st == "less":
        # YES if temp < cap_strike  (e.g. cap 80 => 79-)
        thr = float(contract.cap_strike)  # type: ignore[arg-type]
        return sum(p for t, p in pmf.items() if t < thr)
    raise ValueError(f"Unsupported strike_type={st!r} on {contract.ticker}")


def score_contracts(
    pmf: dict[int, float],
    contracts: list[WeatherContract],
) -> list[ContractProb]:
    scored: list[ContractProb] = []
    for c in contracts:
        model = contract_yes_prob(pmf, c)
        market = c.market_yes
        scored.append(
            ContractProb(
                ticker=c.ticker,
                model_yes=model,
                market_yes=market,
                edge=model - market,
                label=c.label,
            )
        )
    scored.sort(key=lambda x: x.edge, reverse=True)
    return scored
