"""Paper prediction-market book for above/below 15m contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from config.settings import load_settings
from config.kalshi_fees import order_fee
from config.auto_halt import RiskSnapshot, snapshot_from_rows
from models.db import create_db_engine, create_session_factory, init_db
from models.prediction import PredictionBankroll, PredictionBet
from prediction.advisor import Advice
from prediction.window import PredictionWindow


@dataclass(frozen=True)
class VaultWithdrawal:
    """One (or stacked) paper withdrawal into the vault."""

    amount: float
    balance_before: float
    balance_after: float
    vaulted_after: float
    working_bank: float
    trigger_profit: float
    vault_goal: float
    goal_reached: bool


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
        vault_enabled: bool = True,
        vault_working_bank: Optional[float] = None,
        vault_trigger_profit: float = 55.0,
        vault_withdraw_amount: float = 50.0,
        vault_goal: float = 300.0,
        engine=None,
        # Paper used to ignore exchange fees, which made it look more
        # profitable than live. Default taker matches crossing the ask.
        fee_mode: str = "taker",
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
        self.fee_mode = (fee_mode or "none").strip().lower()
        self.vault_enabled = bool(vault_enabled)
        self.vault_working_bank = float(
            self.initial_balance if vault_working_bank is None else vault_working_bank
        )
        self.vault_trigger_profit = max(0.0, float(vault_trigger_profit))
        self.vault_withdraw_amount = max(0.01, float(vault_withdraw_amount))
        self.vault_goal = max(0.0, float(vault_goal))
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
                row = PredictionBankroll(usd_balance=self.initial_balance, vaulted_usd=0.0)
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
                session.add(PredictionBankroll(usd_balance=target, vaulted_usd=0.0))
            else:
                row.usd_balance = target
                row.vaulted_usd = 0.0
                row.updated_at = datetime.now(timezone.utc)
            session.commit()
        self._log(
            f"Paper history reset | W/L 0-0 | bankroll ${target:,.2f} | vault $0.00"
        )

    def _get_bankroll(self, session: Session) -> PredictionBankroll:
        row = session.scalars(
            select(PredictionBankroll).order_by(PredictionBankroll.id.asc())
        ).first()
        if row is None:
            row = PredictionBankroll(usd_balance=self.initial_balance, vaulted_usd=0.0)
            session.add(row)
            session.flush()
        return row

    def get_balance(self) -> float:
        with self.session_factory() as session:
            return float(self._get_bankroll(session).usd_balance)

    def risk_snapshot(self, *, tz_name: str = "America/Los_Angeles") -> RiskSnapshot:
        """Settled P/L for the current local day + cash bank. Read-only."""
        with self.session_factory() as session:
            bank = float(self._get_bankroll(session).usd_balance)
            rows = session.execute(
                select(
                    PredictionBet.pnl,
                    PredictionBet.settled_at,
                    PredictionBet.status,
                ).where(PredictionBet.status.in_(("WON", "LOST")))
            ).all()
        return snapshot_from_rows(bank=bank, rows=list(rows), tz_name=tz_name)

    def set_bank(self, amount: float, *, reason: str = "reconciled") -> float:
        """Force the cash balance to a known figure, keeping all bet history.

        Used after depositing or withdrawing on the exchange, where the book has
        no way to learn that cash moved. Returns the previous balance.
        """
        target = float(amount)
        with self.session_factory() as session:
            row = self._get_bankroll(session)
            previous = float(row.usd_balance)
            row.usd_balance = target
            row.updated_at = datetime.now(timezone.utc)
            session.commit()
        self._log(
            f"Bank {reason} | ${previous:,.2f} → ${target:,.2f} "
            f"({target - previous:+,.2f}) | bet history kept"
        )
        return previous

    def get_vaulted(self) -> float:
        with self.session_factory() as session:
            return float(getattr(self._get_bankroll(session), "vaulted_usd", 0.0) or 0.0)

    def maybe_vault_profits(self) -> Optional[VaultWithdrawal]:
        """Withdraw paper profits into the vault when cash is far enough above working bank.

        Rule (defaults): working bank $100, trigger +$55 → cash ≥ $155,
        withdraw $50 (leave ~$105 so the next loss doesn't start red under $100).
        Repeat while still above the trigger. Stop auto-vault once vaulted ≥ goal ($300).
        """
        if not self.vault_enabled or self.vault_withdraw_amount <= 0:
            return None

        threshold = self.vault_working_bank + self.vault_trigger_profit
        with self.session_factory() as session:
            bankroll = self._get_bankroll(session)
            balance = float(bankroll.usd_balance)
            vaulted = float(getattr(bankroll, "vaulted_usd", 0.0) or 0.0)
            if vaulted >= self.vault_goal:
                return None
            if balance < threshold:
                return None

            balance_before = balance
            withdrawn = 0.0
            while (
                balance >= threshold
                and vaulted < self.vault_goal
                and balance >= self.vault_withdraw_amount
            ):
                # Don't overshoot the vault goal on the last slice
                room = self.vault_goal - vaulted
                chunk = min(self.vault_withdraw_amount, room, balance)
                if chunk <= 0:
                    break
                balance -= chunk
                vaulted += chunk
                withdrawn += chunk

            if withdrawn <= 0:
                return None

            bankroll.usd_balance = balance
            bankroll.vaulted_usd = vaulted
            bankroll.updated_at = datetime.now(timezone.utc)
            session.commit()

            result = VaultWithdrawal(
                amount=withdrawn,
                balance_before=balance_before,
                balance_after=balance,
                vaulted_after=vaulted,
                working_bank=self.vault_working_bank,
                trigger_profit=self.vault_trigger_profit,
                vault_goal=self.vault_goal,
                goal_reached=vaulted >= self.vault_goal,
            )
            self._log_vault(result)
            return result

    def _log_vault(self, withdrawal: VaultWithdrawal) -> None:
        goal_note = (
            " | VAULT GOAL REACHED — auto-vault pauses"
            if withdrawal.goal_reached
            else ""
        )
        self._log(
            f"PAPER VAULT | withdrew ${withdrawal.amount:,.2f} "
            f"(${withdrawal.balance_before:,.2f} → ${withdrawal.balance_after:,.2f}) | "
            f"put aside ${withdrawal.vaulted_after:,.2f} / "
            f"${withdrawal.vault_goal:,.2f}{goal_note}"
        )

    def get_open_bet(self, window_id: str) -> Optional[PredictionBet]:
        with self.session_factory() as session:
            return session.scalars(
                select(PredictionBet).where(
                    PredictionBet.window_id == window_id,
                    PredictionBet.status == "OPEN",
                )
            ).first()

    @staticmethod
    @staticmethod
    def _tradable_share_price(
        price: Optional[float], *, all_in: bool = False
    ) -> Optional[float]:
        if price is None:
            return None
        value = float(price)
        # All-in (fee-inclusive) live fills can sit a hair above 98¢.
        hi = 1.10 if all_in else 0.98
        if value < 0.02 or value > hi:
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
        price_includes_fees: bool = False,
    ) -> Optional[PredictionBet]:
        if not advice.should_bet:
            return None
        if window.strike is None:
            return None

        # Prefer explicit ask for the chosen side; never invent a 1¢ fill
        if contract_price is not None:
            share_price = self._tradable_share_price(
                contract_price, all_in=price_includes_fees
            )
            quote_price = float(contract_price) if share_price is not None else None
        elif advice.entry_share_price is not None:
            share_price = self._tradable_share_price(advice.entry_share_price)
            quote_price = share_price
        elif market_prob_above is not None:
            raw = (
                float(market_prob_above)
                if advice.action == "ABOVE"
                else (1.0 - float(market_prob_above))
            )
            share_price = self._tradable_share_price(raw)
            quote_price = share_price
        else:
            share_price = None
            quote_price = None

        if share_price is None or quote_price is None:
            self._log(
                "Bet skipped | no tradable share ask "
                f"for {advice.action} (refusing empty/0¢/100¢ book)"
            )
            return None

        notional = float(stake_notional) if stake_notional is not None else self.stake_notional
        notional = max(0.01, notional)
        # Each contract pays $1 face → quantity equals notional dollars
        quantity = notional
        fee_total = 0.0
        all_in_price = quote_price
        if not price_includes_fees and self.fee_mode not in {"", "none", "off", "0"}:
            fee_total = order_fee(
                quote_price,
                quantity,
                maker=self.fee_mode == "maker",
            )
            all_in_price = quote_price + (fee_total / quantity)
        total_cost = quantity * quote_price + fee_total
        # Store all-in price so calibration's (WR - avg_price) matches cash P/L.
        share_price = all_in_price
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
        *,
        outcome_side: Optional[str] = None,
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

            # Prefer official Kalshi/RH result when provided; else price vs strike
            if outcome_side in ("ABOVE", "BELOW"):
                outcome = outcome_side
            elif final_price >= float(bet.strike):
                # Robinhood/Kalshi: YES if settlement >= strike (at or above)
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
            vaulted = float(getattr(bankroll, "vaulted_usd", 0.0) or 0.0)
            # Mark open premiums back into equity (capital at risk)
            working_equity = balance + sum(float(b.contract_cost) for b in open_bets)
            # All-in wealth includes paper withdrawals put aside
            equity = working_equity + vaulted
            starting = float(self.initial_balance)
            total_pnl = equity - starting
            return {
                "starting_balance": starting,
                "usd_balance": balance,
                "vaulted_usd": vaulted,
                "working_equity": working_equity,
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
                "total_pnl": total_pnl,
                "total_return_pct": (total_pnl / starting * 100.0) if starting else 0.0,
                "vault_working_bank": self.vault_working_bank,
                "vault_goal": self.vault_goal,
                "vault_enabled": self.vault_enabled,
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
