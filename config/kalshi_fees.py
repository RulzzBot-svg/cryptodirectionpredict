"""Kalshi fee helpers used by paper, live, and the edge gate.

Formulas match Kalshi's published schedule (rounded up to the cent per order):
  taker: 0.07  * C * P * (1-P)
  maker: 0.0175 * C * P * (1-P)

Edge decisions must clear MIN_EDGE *after* this drag, or paper/live will keep
buying trades that look +8¢ and realize ~+6¢ (or less once the model lies).
"""

from __future__ import annotations

import math
from typing import Any, Optional


def taker_fee(price: float, contracts: float) -> float:
    """Total taker fee in dollars for an order, rounded up to the cent."""
    p = min(max(float(price), 0.0), 1.0)
    raw = 0.07 * float(contracts) * p * (1.0 - p)
    return math.ceil(raw * 100.0 - 1e-12) / 100.0


def maker_fee(price: float, contracts: float) -> float:
    """Total maker fee in dollars for an order, rounded up to the cent."""
    p = min(max(float(price), 0.0), 1.0)
    raw = 0.0175 * float(contracts) * p * (1.0 - p)
    return math.ceil(raw * 100.0 - 1e-12) / 100.0


def order_fee(price: float, contracts: float, *, maker: bool) -> float:
    return maker_fee(price, contracts) if maker else taker_fee(price, contracts)


def fee_per_contract(price: float, contracts: float, *, maker: bool) -> float:
    qty = max(float(contracts), 1e-9)
    return order_fee(price, qty, maker=maker) / qty


def net_edge(
    model_prob: float,
    price: float,
    contracts: float,
    *,
    maker: bool,
) -> float:
    """Model edge in probability points after estimated exchange fees."""
    return float(model_prob) - float(price) - fee_per_contract(
        price, contracts, maker=maker
    )


def fee_amount_to_dollars(raw: Any) -> Optional[float]:
    """Normalize a Kalshi fee field to dollars.

    Fees arrive as dollar strings (``\"0.0200\"``), dollar floats, or rarely
    whole cents. Unlike contract prices, a fee can exceed $1 on large size, so
    values above 1 are only treated as cents when they look like cent integers
    (``<= 100`` and close to an integer).
    """
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value >= 1.0:
        # 2 → 2¢; 1.5 dollars stays dollars (not a cent encoding).
        if value <= 100.0 and abs(value - round(value)) < 1e-6:
            return value / 100.0
        return value
    return value
