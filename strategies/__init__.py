"""Trading strategy implementations."""

from .base import BaseStrategy, Signal
from .momentum_strategy import EMACrossoverStrategy

__all__ = ["BaseStrategy", "EMACrossoverStrategy", "Signal"]
