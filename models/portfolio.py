"""Portfolio / account state model."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from models.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Portfolio(Base):
    """Single-row paper account balances."""

    __tablename__ = "portfolio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, default="BTC/USDT")
    usd_balance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    btc_balance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_entry_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )
