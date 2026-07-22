"""Paper prediction-market book for above/below 15m contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from config.settings import load_settings
from models.db import create_db_engine, create_session_factory, init_db
from models.prediction import PredictionBankroll, PredictionBet
from prediction.advisor import Advice
from prediction.window import PredictionWindow


class PredictionBook:
    """
    Simulates buying ABOVE/BELOW contracts.

    Default economics: pay ``contract_cost`` (e.g. $0.50) for a $1 payout if correct.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        initial_balance: Optional[float] = None,
        symbol: Optional[str] = None,
        contract_cost: float = 0.50,
        payout: float = 1.0,
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
        self.contract_cost = float(contract_cost)
        self.payout = float(payout)
        bind = getattr(session_factory, "kw", {}).get("bind")
        init_db(bind if bind is not None else engine)
        self._ensure_bankroll()

    @classmethod
    def from_database_url(
        cls,
        database_url: Optional[str] = None,
        **kwargs,
    ) -> "PredictionBook":
        engine = create_db_engine(database_url)
        init_db(engine)
        return cls(create_session_factory(engine), engine=engine, **kwargs)

    def _ensure_bankroll(self) -> PredictionBankroll:
        with self.session_factory() as session:
            row = session.scalars(
                select(PredictionBankroll).order_by(PredictionBankroll.id.asc())
            ).first()
            if row is None:
                row = PredictionBankroll(usd_balance=self.initial_balance)
                session.add(row)
                session.commit()
                session.refresh(row)
                self._log(
                    f"Initialized prediction bankroll | USD {row.usd_balance:,.2f}"
                )
            return row

    def _get_bankroll(self, session: Session) -> PredictionBankroll:
        row = session.scalars(
            select(PredictionBankroll).order_by(PredictionBankroll.id.asc())
        ).first()
        if row is None:
            row = PredictionBankroll(usd_balance=self.initial_balance)
            session.add(row)
            session.flush()
        return row

    def get_balance(self) -> float:
        with self.session_factory() as session:
            return float(self._get_bankroll(session).usd_balance)

    def get_open_bet(self, window_id: str) -> Optional[PredictionBet]:
        with self.session_factory() as session:
            return session.scalars(
                select(PredictionBet).where(
                    PredictionBet.window_id == window_id,
                    PredictionBet.status == "OPEN",
                )
            ).first()

    def place_bet(
        self,
        window: PredictionWindow,
        advice: Advice,
        *,
        market_prob_above: float = 0.50,
    ) -> Optional[PredictionBet]:
        if not advice.should_bet:
            return None
        if window.strike is None:
            return None

        with self.session_factory() as session:
            existing = session.scalars(
                select(PredictionBet).where(
                    PredictionBet.window_id == window.window_id,
                    PredictionBet.status.in_(("OPEN", "WON", "LOST", "PUSH")),
                )
            ).first()
            if existing is not None:
                self._log(
                    f"Bet skipped | already have a {existing.status} contract "
                    f"for window {window.window_id}"
                )
                return None

            bankroll = self._get_bankroll(session)
            if float(bankroll.usd_balance) < self.contract_cost:
                self._log("Bet skipped | insufficient bankroll")
                return None

            bankroll.usd_balance = float(bankroll.usd_balance) - self.contract_cost
            bankroll.updated_at = datetime.now(timezone.utc)

            model_prob = (
                advice.prob_above if advice.action == "ABOVE" else advice.prob_below
            )
            market_prob = (
                market_prob_above
                if advice.action == "ABOVE"
                else (1.0 - market_prob_above)
            )

            bet = PredictionBet(
                placed_at=datetime.now(timezone.utc),
                window_id=window.window_id,
                window_start=window.start,
                window_end=window.end,
                symbol=self.symbol,
                side=advice.action,
                strike=float(window.strike),
                entry_price=float(advice.estimate.spot),
                contract_cost=self.contract_cost,
                payout=self.payout,
                model_prob=float(model_prob),
                market_prob=float(market_prob),
                edge=float(advice.edge),
                status="OPEN",
                usd_balance_after=float(bankroll.usd_balance),
            )
            session.add(bet)
            session.commit()
            session.refresh(bet)
            self._log_bet_placed(bet, advice)
            return bet

    def settle_window(
        self,
        window: PredictionWindow,
        final_price: float,
    ) -> Optional[PredictionBet]:
        if window.strike is None:
            return None

        with self.session_factory() as session:
            bet = session.scalars(
                select(PredictionBet).where(
                    PredictionBet.window_id == window.window_id,
                    PredictionBet.status == "OPEN",
                )
            ).first()
            if bet is None:
                return None

            if final_price > float(bet.strike):
                outcome = "ABOVE"
            elif final_price < float(bet.strike):
                outcome = "BELOW"
            else:
                outcome = "PUSH"

            bankroll = self._get_bankroll(session)
            if outcome == "PUSH":
                pnl = 0.0
                bankroll.usd_balance = float(bankroll.usd_balance) + float(bet.contract_cost)
                status = "PUSH"
            elif outcome == bet.side:
                pnl = float(bet.payout) - float(bet.contract_cost)
                bankroll.usd_balance = float(bankroll.usd_balance) + float(bet.payout)
                status = "WON"
            else:
                pnl = -float(bet.contract_cost)
                status = "LOST"

            bankroll.updated_at = datetime.now(timezone.utc)
            bet.status = status
            bet.outcome = outcome
            bet.settlement_price = float(final_price)
            bet.pnl = pnl
            bet.settled_at = datetime.now(timezone.utc)
            bet.usd_balance_after = float(bankroll.usd_balance)
            session.commit()
            session.refresh(bet)
            self._log_bet_settled(bet)
            return bet

    def get_performance_stats(self) -> dict:
        with self.session_factory() as session:
            bankroll = self._get_bankroll(session)
            bets = list(session.scalars(select(PredictionBet).order_by(PredictionBet.id)))
            settled = [b for b in bets if b.status in ("WON", "LOST", "PUSH")]
            wins = [b for b in settled if b.status == "WON"]
            losses = [b for b in settled if b.status == "LOST"]
            realized = sum(float(b.pnl or 0.0) for b in settled)
            open_bets = [b for b in bets if b.status == "OPEN"]
            balance = float(bankroll.usd_balance)
            # Open contracts still have capital at risk (premium already deducted)
            equity = balance + sum(float(b.contract_cost) for b in open_bets)
            starting = float(self.initial_balance)
            return {
                "starting_balance": starting,
                "usd_balance": balance,
                "equity": equity,
                "open_bets": len(open_bets),
                "bet_count": len(bets),
                "settled_count": len(settled),
                "win_count": len(wins),
                "loss_count": len(losses),
                "push_count": len([b for b in settled if b.status == "PUSH"]),
                "win_rate_pct": (len(wins) / (len(wins) + len(losses)) * 100.0)
                if (wins or losses)
                else 0.0,
                "realized_pnl": realized,
                "total_pnl": equity - starting,
                "total_return_pct": ((equity - starting) / starting * 100.0)
                if starting
                else 0.0,
            }

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def _log(self, message: str) -> None:
        print(f"\n[{self._timestamp()}] {message}")

    def _log_bet_placed(self, bet: PredictionBet, advice: Advice) -> None:
        print(
            "\n"
            + "=" * 60
            + f"\n  PREDICTION BET  |  {bet.side}"
            + f"\n  Window       : {bet.window_id}"
            + f"\n  Strike       : ${bet.strike:,.2f}"
            + f"\n  Spot         : ${bet.entry_price:,.2f}"
            + f"\n  Model prob   : {bet.model_prob * 100:.2f}%"
            + f"\n  Fair cents   : YES {advice.fair_yes_cents:.1f}¢ / "
            + f"NO {advice.fair_no_cents:.1f}¢"
            + f"\n  Edge         : {bet.edge * 100:.1f}¢"
            + f"\n  Cost / Pay   : ${bet.contract_cost:.2f} → ${bet.payout:.2f}"
            + f"\n  Bankroll     : ${bet.usd_balance_after:,.2f}"
            + f"\n  Reason       : {advice.reason}"
            + "\n"
            + "=" * 60
            + "\n"
        )

    def _log_bet_settled(self, bet: PredictionBet) -> None:
        pnl = float(bet.pnl or 0.0)
        pnl_txt = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
        print(
            "\n"
            + "=" * 60
            + f"\n  SETTLEMENT  |  {bet.status}  |  bet {bet.side}"
            + f"\n  Window       : {bet.window_id}"
            + f"\n  Strike       : ${bet.strike:,.2f}"
            + f"\n  Final price  : ${float(bet.settlement_price):,.2f}"
            + f"\n  Outcome      : {bet.outcome}"
            + f"\n  P/L          : {pnl_txt}"
            + f"\n  Bankroll     : ${float(bet.usd_balance_after):,.2f}"
            + "\n"
            + "=" * 60
            + "\n"
        )
