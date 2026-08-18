#!/usr/bin/env python3
"""Compare the volatility the model assumes against what BTC actually did.

A binary's probability is driven almost entirely by sigma. Too high a sigma
pulls probabilities toward 50%, which systematically overstates the unlikely
side — and the unlikely side is exactly what an edge-seeking bot buys. So a
persistent calibration gap usually traces back to sigma.

Three numbers come out of this:

  realized  — sigma implied by how far BTC actually moved each window, taken
              from settlement price versus strike (the strike is the window's
              opening price, so that ratio *is* the window's return)
  model     — sigma the model used, recovered from the probability it published
  market    — sigma implied by the price Kalshi was charging

If model > realized, the model thinks the market is jumpier than it is, and
every probability it publishes is too close to a coin flip.

Reads the calibration CSV, which is written continuously. Read-only.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import statistics
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WINDOW_SECONDS = 15 * 60


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _prob_above(spot: float, strike: float, tau: float, sigma: float) -> float:
    if tau <= 0 or sigma <= 0:
        return 1.0 if spot > strike else 0.0
    vol = sigma * math.sqrt(tau)
    d2 = (math.log(spot / strike) - 0.5 * sigma * sigma * tau) / vol
    return _norm_cdf(d2)


def implied_sigma(
    spot: float, strike: float, tau: float, target_prob_above: float
) -> Optional[float]:
    """Recover sigma from a published probability, when it's well posed.

    For spot above strike the probability falls monotonically as sigma rises,
    so a bisection is safe. Below the strike it isn't monotonic, so those are
    skipped rather than guessed at.
    """
    if not 0.001 < target_prob_above < 0.999 or tau <= 0:
        return None
    if spot <= strike:
        return None
    lo, hi = 1e-9, 1e-2  # per sqrt-second; 1e-2 is absurdly high, a safe bound
    if _prob_above(spot, strike, tau, hi) > target_prob_above:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _prob_above(spot, strike, tau, mid) > target_prob_above:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _f(row: dict, key: str) -> Optional[float]:
    raw = (row.get(key) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def robust_sigma(values: list[float]) -> tuple[float, float, float]:
    """Three views of 'how wide' a return distribution is.

    Standard deviation is dominated by the rare large moves. The median
    absolute deviation and interquartile range describe the ordinary middle of
    the distribution instead. For a true normal all three agree; the more they
    diverge, the more the distribution is peaked-with-fat-tails — and the more
    misleading standard deviation is for pricing a nearby strike.
    """
    n = len(values)
    sd = statistics.stdev(values) if n > 1 else 0.0
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    sigma_mad = 1.4826 * mad  # scaled so it equals sd for a normal
    ordered = sorted(values)
    q1 = ordered[int(0.25 * (n - 1))]
    q3 = ordered[int(0.75 * (n - 1))]
    sigma_iqr = (q3 - q1) / 1.349  # same normalization
    return sd, sigma_mad, sigma_iqr


def excess_kurtosis(values: list[float]) -> Optional[float]:
    """0 for a normal distribution; large positive means fat tails."""
    n = len(values)
    if n < 4:
        return None
    mean = statistics.fmean(values)
    sd = statistics.stdev(values)
    if sd <= 0:
        return None
    m4 = sum((v - mean) ** 4 for v in values) / n
    return m4 / (sd**4) - 3.0


def annualize(sigma_per_sqrt_second: float) -> float:
    return sigma_per_sqrt_second * math.sqrt(365.25 * 24 * 3600)


def per_window(sigma_per_sqrt_second: float) -> float:
    """Sigma over a full 15-minute window, as a fraction of price."""
    return sigma_per_sqrt_second * math.sqrt(WINDOW_SECONDS)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Model vs realized volatility")
    parser.add_argument(
        "--csv",
        default=os.getenv("CALIBRATION_LOG", "logs/calibration.csv"),
        help="Path to calibration.csv",
    )
    args = parser.parse_args(argv)

    path = Path(args.csv)
    if not path.exists():
        raise SystemExit(f"Calibration log not found: {path}")

    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    # Realized: how far price actually travelled from the strike each window.
    # Split by source — Kalshi's own settlement value measures the index that
    # actually decides these contracts, while a Coinbase reading measures spot.
    # If Kalshi settles on a time-averaged index those two differ, and only the
    # first one is the volatility the contract cares about.
    realized_returns: list[float] = []
    by_source: dict[str, list[float]] = {}
    for r in rows:
        if (r.get("event") or "").strip() != "settle":
            continue
        strike = _f(r, "strike")
        settle = _f(r, "settlement_price")
        if not (strike and settle and strike > 0 and settle > 0):
            continue
        ret = math.log(settle / strike)
        realized_returns.append(ret)
        src = (r.get("settlement_source") or "unknown").strip()
        bucket = "kalshi_official" if src.startswith("kalshi") else "spot_reading"
        by_source.setdefault(bucket, []).append(ret)

    # Model and market sigma, recovered from published probability and price
    model_sigmas: list[float] = []
    market_sigmas: list[float] = []
    pairs: list[tuple[float, float]] = []
    for r in rows:
        if (r.get("event") or "").strip() != "advice":
            continue
        spot, strike = _f(r, "spot"), _f(r, "strike")
        tau = _f(r, "seconds_remaining")
        prob = _f(r, "prob_above")
        ask = _f(r, "yes_ask")
        if not (spot and strike and tau and prob):
            continue
        m = implied_sigma(spot, strike, tau, prob)
        if m is None:
            continue
        model_sigmas.append(m)
        # Invert the mid, not the ask. The ask carries the spread, which biases
        # the implied sigma low and makes the market look far calmer than it is.
        # Kalshi quotes NO from the other side of the book, so the YES bid is
        # 1 - no_ask.
        no_ask = _f(r, "no_ask")
        mid = None
        if ask is not None and no_ask is not None:
            mid = (ask + (1.0 - no_ask)) / 2.0
        elif ask is not None:
            mid = ask
        if mid is not None:
            k = implied_sigma(spot, strike, tau, mid)
            if k is not None:
                market_sigmas.append(k)
                pairs.append((m, k))

    print("=" * 70)
    print("  VOLATILITY CHECK")
    print("=" * 70)
    print(f"  Source : {path}")
    print(f"  Windows settled     : {len(realized_returns)}")
    print(f"  Advice ticks usable : {len(model_sigmas)}")

    realized_pss = None
    if len(realized_returns) >= 10:
        realized_15m = statistics.stdev(realized_returns)
        print()
        print("  What BTC actually did (all sources pooled)")
        print(f"    15m move (1 sigma) : {realized_15m*100:.3f}% of price")
        print(
            f"    annualized         : "
            f"{annualize(realized_15m / math.sqrt(WINDOW_SECONDS))*100:.0f}%"
        )
    else:
        print("\n  Not enough settled windows yet for a realized estimate")

    # Prefer Kalshi's official value: that index is what settles the contract
    for bucket in ("kalshi_official", "spot_reading"):
        vals = by_source.get(bucket) or []
        if len(vals) < 10:
            continue
        sd = statistics.stdev(vals)
        pss = sd / math.sqrt(WINDOW_SECONDS)
        label = (
            "Kalshi's own settlement index"
            if bucket == "kalshi_official"
            else "our spot reading at close"
        )
        print()
        print(f"  {label}  ({len(vals)} windows)")
        print(f"    15m move (1 sigma) : {sd*100:.3f}% of price")
        print(f"    annualized         : {annualize(pss)*100:.0f}%")
        if bucket == "kalshi_official":
            realized_pss = pss
            sd, s_mad, s_iqr = robust_sigma(vals)
            kurt = excess_kurtosis(vals)
            print(f"    from std dev       : {sd*100:.3f}%  <- what the model uses")
            print(f"    from MAD           : {s_mad*100:.3f}%  <- the ordinary middle")
            print(f"    from IQR           : {s_iqr*100:.3f}%")
            if kurt is not None:
                shape = (
                    "fat tails: std dev overstates the typical move"
                    if kurt > 1.0
                    else "roughly normal"
                )
                print(f"    excess kurtosis    : {kurt:+.1f}  ({shape})")
    if realized_pss is None and len(realized_returns) >= 10:
        realized_pss = statistics.stdev(realized_returns) / math.sqrt(WINDOW_SECONDS)

    if model_sigmas:
        med_model = statistics.median(model_sigmas)
        print()
        print("  What the model assumed")
        print(f"    15m move (1 sigma) : {per_window(med_model)*100:.3f}% of price")
        print(f"    annualized         : {annualize(med_model)*100:.0f}%")

    if market_sigmas:
        med_market = statistics.median(market_sigmas)
        print()
        print("  What Kalshi's price implied")
        print(f"    15m move (1 sigma) : {per_window(med_market)*100:.3f}% of price")
        print(f"    annualized         : {annualize(med_market)*100:.0f}%")

    print()
    print("-" * 70)
    if model_sigmas and realized_pss:
        ratio = statistics.median(model_sigmas) / realized_pss
        print(f"  model / realized : {ratio:.2f}x   (realized = Kalshi's index where available)")
        if ratio > 1.15:
            print("  → The model thinks BTC is jumpier than it is. Every")
            print("    probability it publishes sits too close to 50%, which")
            print("    overstates the unlikely side — the side it buys.")
        elif ratio < 0.85:
            print("  → The model underestimates movement, pushing probabilities")
            print("    too far toward the extremes.")
        else:
            print("  → Volatility looks about right; the calibration gap is")
            print("    coming from somewhere else.")
    if pairs:
        ratios = [m / k for m, k in pairs if k > 0]
        if ratios:
            print(f"  model / market   : {statistics.median(ratios):.2f}x")
            print("    (>1 means we assume more movement than Kalshi does)")
    print("-" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
