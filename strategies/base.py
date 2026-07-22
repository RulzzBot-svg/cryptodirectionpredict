"""Base interface for trading strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

import pandas as pd

Signal = Literal["BUY", "SELL", "HOLD"]


class BaseStrategy(ABC):
    """Parent class for swappable trading strategies."""

    name: str = "base"

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Evaluate the latest market state and return a trading signal.

        Parameters
        ----------
        df:
            Pandas DataFrame of OHLCV candles plus strategy indicators
            (e.g. ema_9, ema_21, rsi_14), ordered oldest → newest.
        """

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
