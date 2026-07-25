"""Append-only calibration log for every advice tick / window outcome."""

from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


DEFAULT_PATH = Path("logs/calibration.csv")

_FIELDS = [
    "ts_utc",
    "event",  # advice | settle
    "window_id",
    "symbol",
    "spot",
    "strike",
    "seconds_remaining",
    "prob_above",
    "prob_below",
    "yes_ask",
    "no_ask",
    "action",
    "edge",
    "outcome",
    "settlement_price",
    "settlement_source",
]


class CalibrationLog:
    def __init__(self, path: Optional[Path] = None) -> None:
        raw = os.getenv("CALIBRATION_LOG", str(DEFAULT_PATH))
        self.path = Path(path) if path is not None else Path(raw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", encoding="utf-8", newline="") as fh:
                csv.DictWriter(fh, fieldnames=_FIELDS).writeheader()

    def _write(self, row: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=_FIELDS)
            writer.writerow({k: row.get(k, "") for k in _FIELDS})

    def log_advice(
        self,
        *,
        window_id: str,
        symbol: str,
        spot: float,
        strike: float,
        seconds_remaining: float,
        prob_above: float,
        prob_below: float,
        yes_ask: Optional[float],
        no_ask: Optional[float],
        action: str,
        edge: float,
    ) -> None:
        self._write(
            {
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "event": "advice",
                "window_id": window_id,
                "symbol": symbol,
                "spot": f"{spot:.4f}",
                "strike": f"{strike:.4f}",
                "seconds_remaining": f"{seconds_remaining:.1f}",
                "prob_above": f"{prob_above:.6f}",
                "prob_below": f"{prob_below:.6f}",
                "yes_ask": "" if yes_ask is None else f"{yes_ask:.4f}",
                "no_ask": "" if no_ask is None else f"{no_ask:.4f}",
                "action": action,
                "edge": f"{edge:.6f}",
            }
        )

    def log_settle(
        self,
        *,
        window_id: str,
        symbol: str,
        strike: Optional[float],
        outcome: Optional[str],
        settlement_price: Optional[float],
        settlement_source: str,
    ) -> None:
        self._write(
            {
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "event": "settle",
                "window_id": window_id,
                "symbol": symbol,
                "strike": "" if strike is None else f"{float(strike):.4f}",
                "outcome": outcome or "",
                "settlement_price": (
                    "" if settlement_price is None else f"{float(settlement_price):.4f}"
                ),
                "settlement_source": settlement_source,
            }
        )
