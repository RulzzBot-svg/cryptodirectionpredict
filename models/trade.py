"""Trade history model."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from models.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Trade(Base):
    """Recorded paper trade."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # BUY | SELL
    price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)  # BTC amount
    usd_amount: Mapped[float] = mapped_column(Float, nullable=False)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    usd_balance_after: Mapped[float] = mapped_column(Float, nullable=False)
    btc_balance_after: Mapped[float] = mapped_column(Float, nullable=False)
    position_size_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
