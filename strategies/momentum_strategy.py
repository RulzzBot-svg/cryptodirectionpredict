"""EMA crossover momentum strategy."""

from __future__ import annotations

import logging

import pandas as pd

from .base import BaseStrategy, Signal

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("ema_9", "ema_21", "rsi_14")


class EMACrossoverStrategy(BaseStrategy):
    """
    Short-term EMA crossover with an RSI filter.

    BUY  — 9 EMA crosses above 21 EMA and RSI is under ``rsi_buy_max``
    SELL — 9 EMA crosses below 21 EMA
    HOLD — otherwise
    """

    name = "ema_crossover"

    def __init__(self, rsi_buy_max: float = 60.0) -> None:
        self.rsi_buy_max = rsi_buy_max

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        if df is None or df.empty:
            logger.debug("%s: empty DataFrame → HOLD", self.name)
            return "HOLD"

        missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
        if missing:
            raise ValueError(
                f"{self.name} requires columns {REQUIRED_COLUMNS}; missing {missing}"
            )

        # Need two valid bars to detect a crossover
        indicators = df.loc[:, list(REQUIRED_COLUMNS)].dropna()
        if len(indicators) < 2:
            logger.debug("%s: insufficient indicator history → HOLD", self.name)
            return "HOLD"

        prev = indicators.iloc[-2]
        curr = indicators.iloc[-1]

        prev_ema_9 = float(prev["ema_9"])
        prev_ema_21 = float(prev["ema_21"])
        curr_ema_9 = float(curr["ema_9"])
        curr_ema_21 = float(curr["ema_21"])
        curr_rsi = float(curr["rsi_14"])

        crossed_above = prev_ema_9 <= prev_ema_21 and curr_ema_9 > curr_ema_21
        crossed_below = prev_ema_9 >= prev_ema_21 and curr_ema_9 < curr_ema_21

        if crossed_above and curr_rsi < self.rsi_buy_max:
            logger.info(
                "%s: BUY (ema_9 crossed above ema_21, rsi_14=%.2f < %.2f)",
                self.name,
                curr_rsi,
                self.rsi_buy_max,
            )
            return "BUY"

        if crossed_below:
            logger.info(
                "%s: SELL (ema_9 crossed below ema_21, rsi_14=%.2f)",
                self.name,
                curr_rsi,
            )
            return "SELL"

        return "HOLD"
