"""Configuration helpers for the prediction-market bot."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    paper_initial_balance: float
    symbol: str
    data_provider: str
    database_url: str
    min_edge: float
    market_prob_above: float
    contract_cost: float
    auto_bet: bool


def load_settings() -> Settings:
    return Settings(
        paper_initial_balance=float(os.getenv("PAPER_INITIAL_BALANCE", "10000")),
        symbol=os.getenv("SYMBOL", "BTC/USD"),
        data_provider=os.getenv("DATA_PROVIDER", "coinbase").strip().lower(),
        database_url=os.getenv("DATABASE_URL", "sqlite:///./paper_trading.db"),
        min_edge=float(os.getenv("MIN_EDGE", "0.08")),
        market_prob_above=float(os.getenv("MARKET_PROB_ABOVE", "0.50")),
        contract_cost=float(os.getenv("CONTRACT_COST", "0.50")),
        auto_bet=os.getenv("AUTO_BET", "true").strip().lower()
        in {"1", "true", "yes", "on"},
    )
