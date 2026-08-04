"""Choose weather contracts with model edge over Kalshi mid price."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from probability import ContractProb


@dataclass(frozen=True)
class Advice:
    action: str  # BUY_YES | SKIP
    pick: Optional[ContractProb]
    reason: str


def advise(
    scored: list[ContractProb],
    *,
    min_edge: float,
) -> Advice:
    if not scored:
        return Advice("SKIP", None, "no open contracts")

    best = scored[0]
    if best.edge < min_edge:
        return Advice(
            "SKIP",
            best,
            f"best edge {best.edge:+.1%} < min {min_edge:.1%} "
            f"({best.label}: model {best.model_yes:.1%} vs mkt {best.market_yes:.1%})",
        )

    # Avoid buying near-zero liquidity / absurd asks proxied as mid≈0/1
    if best.market_yes <= 0.02 or best.market_yes >= 0.98:
        return Advice(
            "SKIP",
            best,
            f"market price extreme ({best.market_yes:.1%}) on {best.label}",
        )

    return Advice(
        "BUY_YES",
        best,
        f"edge {best.edge:+.1%} on {best.label} "
        f"(model {best.model_yes:.1%} vs mkt {best.market_yes:.1%})",
    )
