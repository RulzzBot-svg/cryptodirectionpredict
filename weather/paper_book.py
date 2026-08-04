"""Simple paper ledger for weather YES contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class PaperFill:
    ticker: str
    label: str
    side: str  # YES
    contracts: float
    price: float
    model_yes: float
    edge: float
    ts: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )


@dataclass
class PaperBook:
    bankroll: float
    fills: list[PaperFill] = field(default_factory=list)

    def can_buy(self, notional: float) -> bool:
        return notional > 0 and self.bankroll >= notional

    def buy_yes(
        self,
        *,
        ticker: str,
        label: str,
        price: float,
        notional: float,
        model_yes: float,
        edge: float,
    ) -> PaperFill | None:
        if price <= 0 or price >= 1:
            return None
        if not self.can_buy(notional):
            return None
        contracts = notional / price
        self.bankroll -= notional
        fill = PaperFill(
            ticker=ticker,
            label=label,
            side="YES",
            contracts=contracts,
            price=price,
            model_yes=model_yes,
            edge=edge,
        )
        self.fills.append(fill)
        return fill
