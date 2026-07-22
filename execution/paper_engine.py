"""Paper trading execution engine."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from config.settings import load_settings
from models.db import create_db_engine, create_session_factory, init_db
from models.portfolio import Portfolio
from models.trade import Trade


class PaperBroker:
    """
    Simulated broker that persists balances and trades via SQLAlchemy.

    Parameters
    ----------
    session_factory:
        SQLAlchemy ``sessionmaker`` used for database access.
    initial_balance:
        Starting USD balance when no portfolio row exists yet.
    symbol:
        Trading pair label stored on trades (default from settings).
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        initial_balance: Optional[float] = None,
        symbol: Optional[str] = None,
        engine=None,
    ) -> None:
        settings = load_settings()
        self.session_factory = session_factory
        self.initial_balance = (
            float(initial_balance)
            if initial_balance is not None
            else settings.paper_initial_balance
        )
        self.symbol = symbol or settings.symbol
        bind = getattr(session_factory, "kw", {}).get("bind")
        init_db(bind if bind is not None else engine)
        self._ensure_portfolio()

    @classmethod
    def from_database_url(
        cls,
        database_url: Optional[str] = None,
        *,
        initial_balance: Optional[float] = None,
        symbol: Optional[str] = None,
    ) -> "PaperBroker":
        """Convenience constructor that creates engine + session factory."""
        engine = create_db_engine(database_url)
        init_db(engine)
        factory = create_session_factory(engine)
        return cls(
            factory,
            initial_balance=initial_balance,
            symbol=symbol,
            engine=engine,
        )

    def _ensure_portfolio(self) -> Portfolio:
        with self.session_factory() as session:
            portfolio = session.scalars(
                select(Portfolio).order_by(Portfolio.id.asc())
            ).first()
            if portfolio is None:
                portfolio = Portfolio(
                    symbol=self.symbol,
                    usd_balance=self.initial_balance,
                    btc_balance=0.0,
                    avg_entry_price=0.0,
                )
                session.add(portfolio)
                session.commit()
                session.refresh(portfolio)
                self._log(
                    f"Initialized paper portfolio | USD {portfolio.usd_balance:,.2f} | "
                    f"BTC {portfolio.btc_balance:.8f}"
                )
            return portfolio

    def get_portfolio(self) -> dict:
        with self.session_factory() as session:
            portfolio = self._get_portfolio_row(session)
            return {
                "symbol": portfolio.symbol,
                "usd_balance": float(portfolio.usd_balance),
                "btc_balance": float(portfolio.btc_balance),
                "avg_entry_price": float(portfolio.avg_entry_price),
                "updated_at": portfolio.updated_at,
            }

    def _get_portfolio_row(self, session: Session) -> Portfolio:
        portfolio = session.scalars(
            select(Portfolio).order_by(Portfolio.id.asc())
        ).first()
        if portfolio is None:
            portfolio = Portfolio(
                symbol=self.symbol,
                usd_balance=self.initial_balance,
                btc_balance=0.0,
                avg_entry_price=0.0,
            )
            session.add(portfolio)
            session.flush()
        return portfolio

    def execute_order(
        self,
        signal: str,
        current_price: float,
        position_size_pct: float = 0.25,
    ) -> Optional[Trade]:
        """
        Execute a paper order from a strategy signal.

        BUY  — spend ``position_size_pct`` of USD balance on BTC at ``current_price``
        SELL — liquidate the full BTC position and realize P/L vs entry
        HOLD — no-op
        """
        normalized = (signal or "HOLD").strip().upper()
        if normalized == "HOLD":
            return None
        if current_price <= 0:
            raise ValueError(f"current_price must be positive, got {current_price}")
        if not 0 < position_size_pct <= 1:
            raise ValueError(
                f"position_size_pct must be in (0, 1], got {position_size_pct}"
            )

        if normalized == "BUY":
            return self._execute_buy(current_price, position_size_pct)
        if normalized == "SELL":
            return self._execute_sell(current_price)
        raise ValueError(f"Unsupported signal '{signal}'. Expected BUY, SELL, or HOLD.")

    def _execute_buy(self, current_price: float, position_size_pct: float) -> Optional[Trade]:
        with self.session_factory() as session:
            portfolio = self._get_portfolio_row(session)
            usd_to_spend = float(portfolio.usd_balance) * position_size_pct

            if usd_to_spend <= 0:
                self._log("BUY skipped | insufficient USD balance")
                return None

            btc_qty = usd_to_spend / current_price
            if btc_qty <= 0:
                self._log("BUY skipped | calculated BTC quantity is zero")
                return None

            prev_btc = float(portfolio.btc_balance)
            prev_entry = float(portfolio.avg_entry_price)
            new_btc = prev_btc + btc_qty
            # Volume-weighted average entry across open inventory
            if new_btc > 0:
                portfolio.avg_entry_price = (
                    (prev_btc * prev_entry) + (btc_qty * current_price)
                ) / new_btc
            else:
                portfolio.avg_entry_price = 0.0

            portfolio.usd_balance = float(portfolio.usd_balance) - usd_to_spend
            portfolio.btc_balance = new_btc
            portfolio.symbol = self.symbol
            portfolio.updated_at = datetime.now(timezone.utc)

            trade = Trade(
                timestamp=datetime.now(timezone.utc),
                symbol=self.symbol,
                side="BUY",
                price=current_price,
                quantity=btc_qty,
                usd_amount=usd_to_spend,
                realized_pnl=None,
                usd_balance_after=float(portfolio.usd_balance),
                btc_balance_after=float(portfolio.btc_balance),
                position_size_pct=position_size_pct,
            )
            session.add(trade)
            session.commit()
            session.refresh(trade)

            self._log_trade(
                side="BUY",
                price=current_price,
                quantity=btc_qty,
                usd_amount=usd_to_spend,
                realized_pnl=None,
                usd_balance=float(portfolio.usd_balance),
                btc_balance=float(portfolio.btc_balance),
                avg_entry=float(portfolio.avg_entry_price),
                position_size_pct=position_size_pct,
            )
            return trade

    def _execute_sell(self, current_price: float) -> Optional[Trade]:
        with self.session_factory() as session:
            portfolio = self._get_portfolio_row(session)
            btc_qty = float(portfolio.btc_balance)

            if btc_qty <= 0:
                self._log("SELL skipped | no open BTC position")
                return None

            entry = float(portfolio.avg_entry_price)
            usd_proceeds = btc_qty * current_price
            realized_pnl = (current_price - entry) * btc_qty if entry > 0 else 0.0

            portfolio.usd_balance = float(portfolio.usd_balance) + usd_proceeds
            portfolio.btc_balance = 0.0
            portfolio.avg_entry_price = 0.0
            portfolio.symbol = self.symbol
            portfolio.updated_at = datetime.now(timezone.utc)

            trade = Trade(
                timestamp=datetime.now(timezone.utc),
                symbol=self.symbol,
                side="SELL",
                price=current_price,
                quantity=btc_qty,
                usd_amount=usd_proceeds,
                realized_pnl=realized_pnl,
                usd_balance_after=float(portfolio.usd_balance),
                btc_balance_after=float(portfolio.btc_balance),
                position_size_pct=None,
            )
            session.add(trade)
            session.commit()
            session.refresh(trade)

            self._log_trade(
                side="SELL",
                price=current_price,
                quantity=btc_qty,
                usd_amount=usd_proceeds,
                realized_pnl=realized_pnl,
                usd_balance=float(portfolio.usd_balance),
                btc_balance=float(portfolio.btc_balance),
                avg_entry=entry,
                position_size_pct=None,
            )
            return trade

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def _log(self, message: str) -> None:
        print(f"[{self._timestamp()}] {message}")

    def _log_trade(
        self,
        *,
        side: str,
        price: float,
        quantity: float,
        usd_amount: float,
        realized_pnl: Optional[float],
        usd_balance: float,
        btc_balance: float,
        avg_entry: float,
        position_size_pct: Optional[float],
    ) -> None:
        lines = [
            "",
            "=" * 60,
            f"  PAPER TRADE  |  {side}  |  {self.symbol}",
            f"  Time         : {self._timestamp()}",
            f"  Price        : ${price:,.2f}",
            f"  Quantity     : {quantity:.8f} BTC",
            f"  USD amount   : ${usd_amount:,.2f}",
        ]
        if position_size_pct is not None:
            lines.append(f"  Size         : {position_size_pct * 100:.0f}% of USD balance")
        if side == "SELL":
            pnl_sign = "+" if (realized_pnl or 0) >= 0 else ""
            lines.append(f"  Entry price  : ${avg_entry:,.2f}")
            lines.append(f"  Realized P/L : {pnl_sign}${realized_pnl:,.2f}")
        else:
            lines.append(f"  Avg entry    : ${avg_entry:,.2f}")
        lines.extend(
            [
                f"  USD balance  : ${usd_balance:,.2f}",
                f"  BTC balance  : {btc_balance:.8f} BTC",
                "=" * 60,
                "",
            ]
        )
        print("\n".join(lines))
