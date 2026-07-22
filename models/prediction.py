"""Prediction-market paper portfolio and settled contracts."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from models.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PredictionBankroll(Base):
    """Cash bankroll used for paper prediction contracts."""

    __tablename__ = "prediction_bankroll"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    usd_balance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class PredictionBet(Base):
    """One paper above/below contract for a 15m window."""

    __tablename__ = "prediction_bets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )
    window_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # ABOVE | BELOW
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)  # spot at entry
    contract_cost: Mapped[float] = mapped_column(Float, nullable=False)
    payout: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    model_prob: Mapped[float] = mapped_column(Float, nullable=False)
    market_prob: Mapped[float] = mapped_column(Float, nullable=False)
    edge: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    settlement_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(8), nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    usd_balance_after: Mapped[float | None] = mapped_column(Float, nullable=True)
