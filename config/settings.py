"""Configuration helpers for the paper-trading bot."""

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


def load_settings() -> Settings:
    return Settings(
        paper_initial_balance=float(os.getenv("PAPER_INITIAL_BALANCE", "10000")),
        symbol=os.getenv("SYMBOL", "BTC/USDT"),
        data_provider=os.getenv("DATA_PROVIDER", "binance").strip().lower(),
    )
