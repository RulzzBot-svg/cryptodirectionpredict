"""Paper prediction-market book for above/below 15m contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from config.settings import load_settings
from models.db import create_db_engine, create_session_factory, init_db
from models.prediction import PredictionBankroll, PredictionBet
from prediction.advisor import Advice
from prediction.window import PredictionWindow


class PredictionBook:
    """
    Simulates buying ABOVE/BELOW shares like Robinhood/Kalshi.

    Economics for face ``N`` contracts (each pays $1 if correct):
      - YES ask 34¢ / NO ask 66¢, face $20 →
          ABOVE: pay $6.80 now; win → receive $20 total
                 (= $6.80 stake back + $13.20 profit); lose → lose $6.80
          BELOW: pay $13.20 now; win → receive $20 total
                 (= $13.20 stake back + $6.80 profit); lose → lose $13.20
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        initial_balance: Optional[float] = None,
        symbol: Optional[str] = None,
        stake_notional: float = 5.0,
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
        self.stake_notional = max(0.01, float(stake_notional))
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

    def reset_paper_history(self, *, balance: Optional[float] = None) -> None:
        """Clear all paper bets and set bankroll (default: initial_balance)."""
        target = float(self.initial_balance if balance is None else balance)
        with self.session_factory() as session:
            session.execute(delete(PredictionBet))
            row = session.scalars(
                select(PredictionBankroll).order_by(PredictionBankroll.id.asc())
            ).first()
            if row is None:
                session.add(PredictionBankroll(usd_balance=target))
            else:
                row.usd_balance = target
                row.updated_at = datetime.now(timezone.utc)
            session.commit()
        self._log(
            f"Paper history reset | W/L 0-0 | bankroll ${target:,.2f}"
        )

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

    @staticmethod
    def _tradable_share_price(price: Optional[float]) -> Optional[float]:
        if price is None:
            return None
        value = float(price)
        if value < 0.02 or value > 0.98:
            return None
        return value

    def place_bet(
        self,
        window: PredictionWindow,
        advice: Advice,
        *,
        market_prob_above: Optional[float] = None,
        contract_price: Optional[float] = None,
        stake_notional: Optional[float] = None,
    ) -> Optional[PredictionBet]:
        if not advice.should_bet:
            return None
        if window.strike is None:
            return None

        # Prefer explicit ask for the chosen side; never invent a 1¢ fill
        if contract_price is not None:
            share_price = self._tradable_share_price(contract_price)
        elif advice.entry_share_price is not None:
            share_price = self._tradable_share_price(advice.entry_share_price)
        elif market_prob_above is not None:
            raw = (
                float(market_prob_above)
                if advice.action == "ABOVE"
                else (1.0 - float(market_prob_above))
            )
            share_price = self._tradable_share_price(raw)
        else:
            share_price = None

        if share_price is None:
            self._log(
                "Bet skipped | no tradable share ask "
                f"for {advice.action} (refusing empty/0¢/100¢ book)"
            )
            return None

        notional = float(stake_notional) if stake_notional is not None else self.stake_notional
        notional = max(0.01, notional)
        # Each contract pays $1 face → quantity equals notional dollars
        quantity = notional
        total_cost = quantity * share_price
        total_payout = quantity * 1.0  # cash returned if correct (includes stake)

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
            if float(bankroll.usd_balance) < total_cost:
                self._log(
                    f"Bet skipped | need ${total_cost:,.2f}, "
                    f"have ${float(bankroll.usd_balance):,.2f}"
                )
                return None

            bankroll.usd_balance = float(bankroll.usd_balance) - total_cost
            bankroll.updated_at = datetime.now(timezone.utc)

            model_prob = (
                advice.prob_above if advice.action == "ABOVE" else advice.prob_below
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
                quantity=quantity,
                contract_price=share_price,
                contract_cost=total_cost,
                payout=total_payout,
                model_prob=float(model_prob),
                market_prob=float(share_price),
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

            # Robinhood/Kalshi: YES if settlement >= strike (at or above)
            if final_price >= float(bet.strike):
                outcome = "ABOVE"
            else:
                outcome = "BELOW"

            bankroll = self._get_bankroll(session)
            if outcome == bet.side:
                # Win: receive full $1 face value per contract (stake + profit)
                pnl = float(bet.payout) - float(bet.contract_cost)
                bankroll.usd_balance = float(bankroll.usd_balance) + float(bet.payout)
                status = "WON"
            else:
                # Lose: premium already debited at entry — you lose what you paid
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
            # Mark open premiums back into equity (capital at risk)
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
        qty = float(bet.quantity)
        px = float(bet.contract_price)
        cost = float(bet.contract_cost)
        payout = float(bet.payout)
        profit_if_win = payout - cost
        print(
            "\n"
            + "=" * 60
            + f"\n  PREDICTION BET  |  {bet.side}"
            + f"\n  Window         : {bet.window_id}"
            + f"\n  Strike         : ${bet.strike:,.2f}"
            + f"\n  BTC spot       : ${bet.entry_price:,.2f}"
            + f"\n  Model prob     : {bet.model_prob * 100:.2f}%"
            + f"\n  Share price    : {px * 100:.1f}¢"
            + f"\n  Contracts      : {qty:.2f}  (face ${payout:,.2f})"
            + f"\n  You pay now    : ${cost:,.2f}  (your stake)"
            + f"\n  If correct     : receive ${payout:,.2f} total "
            + f"(= ${cost:,.2f} stake back + ${profit_if_win:,.2f} profit)"
            + f"\n  If wrong       : lose your ${cost:,.2f} stake"
            + f"\n  Edge           : {bet.edge * 100:.1f}¢"
            + f"\n  Bankroll       : ${bet.usd_balance_after:,.2f}"
            + f"\n  Reason         : {advice.reason}"
            + "\n"
            + "=" * 60
            + "\n"
        )

    def _log_bet_settled(self, bet: PredictionBet) -> None:
        pnl = float(bet.pnl or 0.0)
        cost = float(bet.contract_cost)
        payout = float(bet.payout)
        if bet.status == "WON":
            pnl_txt = (
                f"+${pnl:,.2f}  (received ${payout:,.2f} total = "
                f"${cost:,.2f} stake + ${pnl:,.2f} profit)"
            )
        else:
            pnl_txt = f"-${abs(pnl):,.2f}  (lost your ${cost:,.2f} stake)"
        print(
            "\n"
            + "=" * 60
            + f"\n  SETTLEMENT  |  {bet.status}  |  bet {bet.side}"
            + f"\n  Window         : {bet.window_id}"
            + f"\n  Strike         : ${bet.strike:,.2f}"
            + f"\n  Final price    : ${float(bet.settlement_price):,.2f}"
            + f"\n  Outcome        : {bet.outcome}"
            + f"\n  Contracts      : {float(bet.quantity):.2f} @ "
            + f"{float(bet.contract_price) * 100:.1f}¢"
            + f"\n  Paid / Face    : ${cost:,.2f} / ${payout:,.2f}"
            + f"\n  P/L            : {pnl_txt}"
            + f"\n  Bankroll       : ${float(bet.usd_balance_after):,.2f}"
            + "\n"
            + "=" * 60
            + "\n"
        )
