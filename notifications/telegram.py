"""Telegram notifications for paper-bot events."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Send short trade alerts via the Telegram Bot API."""

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self.token = (token if token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_id = (chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")).strip()
        if enabled is None:
            flag = os.getenv("TELEGRAM_ALERTS", "true").strip().lower()
            enabled = flag in {"1", "true", "yes", "on"}
        self.enabled = bool(enabled) and bool(self.token) and bool(self.chat_id)
        quiet_flag = os.getenv("TELEGRAM_QUIET", "true").strip().lower()
        self.quiet = quiet_flag in {"1", "true", "yes", "on"}
        # Set to "LIVE" once real orders are armed so alerts are unmistakable
        self.prefix = ""
        if enabled and not self.enabled:
            logger.warning(
                "Telegram alerts requested but TELEGRAM_BOT_TOKEN / "
                "TELEGRAM_CHAT_ID not set — alerts disabled"
            )

    @property
    def active(self) -> bool:
        return self.enabled

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        if self.prefix:
            text = f"[{self.prefix}] {text}"
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": text[:4000],
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.load(resp)
            if not body.get("ok"):
                logger.warning("Telegram API not ok: %s", body)
                return False
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            logger.warning("Telegram send failed: %s", exc)
            return False

    def bet_placed(self, bet: Any, *, reason: str = "") -> None:
        if self.quiet:
            return
        px = float(bet.contract_price) * 100
        cost = float(bet.contract_cost)
        payout = float(bet.payout)
        profit = payout - cost
        msg = (
            f"BET {bet.side}\n"
            f"Window {bet.window_id}\n"
            f"Strike ${float(bet.strike):,.2f} | Spot ${float(bet.entry_price):,.2f}\n"
            f"Model {float(bet.model_prob)*100:.1f}% @ {px:.1f}¢\n"
            f"Pay ${cost:.2f} → win ${payout:.2f} (+${profit:.2f}) / lose ${cost:.2f}\n"
            f"Edge {float(bet.edge)*100:.1f}¢ | Bank ${float(bet.usd_balance_after):,.2f}"
        )
        if reason:
            msg += f"\n{reason}"
        self.send(msg)

    def bet_settled(self, bet: Any) -> None:
        if self.quiet:
            return
        pnl = float(bet.pnl or 0.0)
        sign = "+" if pnl >= 0 else "-"
        msg = (
            f"SETTLED {bet.status} ({bet.side})\n"
            f"Window {bet.window_id}\n"
            f"Final ${float(bet.settlement_price):,.2f} → {bet.outcome}\n"
            f"P/L {sign}${abs(pnl):,.2f} | Bank ${float(bet.usd_balance_after):,.2f}"
        )
        self.send(msg)

    def window_stats(self, stats: dict[str, Any], *, skips: str = "") -> None:
        if self.quiet:
            return
        pnl = float(stats.get("total_pnl") or 0.0)
        sign = "+" if pnl >= 0 else "-"
        vaulted = float(stats.get("vaulted_usd") or 0.0)
        msg = (
            f"WINDOW STATS\n"
            f"Bank ${float(stats.get('usd_balance') or 0):,.2f} | "
            f"Vault ${vaulted:,.2f} | "
            f"All-in ${float(stats.get('equity') or 0):,.2f}\n"
            f"Total P/L {sign}${abs(pnl):,.2f}\n"
            f"W/L {stats.get('win_count', 0)}W/"
            f"{stats.get('loss_count', 0)}L "
            f"({float(stats.get('win_rate_pct') or 0):.1f}%) | "
            f"Settled {stats.get('settled_count', 0)}"
        )
        if skips:
            msg += f"\nNo bet: {skips}"
        self.send(msg)

    def vault_withdrawal(self, event: Any) -> None:
        goal = float(getattr(event, "vault_goal", 0.0) or 0.0)
        aside = float(getattr(event, "vaulted_after", 0.0) or 0.0)
        msg = (
            f"PAPER VAULT\n"
            f"Withdrew ${float(event.amount):,.2f}\n"
            f"Bank ${float(event.balance_before):,.2f} → "
            f"${float(event.balance_after):,.2f}\n"
            f"Put aside ${aside:,.2f} / ${goal:,.2f}"
        )
        if getattr(event, "goal_reached", False):
            msg += "\nVault goal reached — auto-vault pauses."
        # The vault is bookkeeping only; the cash is still on Kalshi and still
        # at risk until it's actually withdrawn.
        msg += (
            f"\nBookkeeping only — no money left Kalshi. To actually bank it, "
            f"withdraw ${float(event.amount):,.2f} to your bank."
        )
        self.send(msg)

    def live_order_plan(self, plan: Any, *, note: str = "DRY-RUN") -> None:
        msg = (
            f"LIVE {note}\n"
            f"{plan.advice_side} via YES-{plan.book_side} @ "
            f"{float(plan.yes_book_price)*100:.1f}¢\n"
            f"Pay side ~{float(plan.share_price)*100:.1f}¢ × "
            f"{float(plan.contracts):.2f} contracts\n"
            f"Ticker {plan.market_ticker}\n"
            f"client_order_id {plan.client_order_id}"
        )
        if getattr(plan, "order_id", None):
            msg += f"\norder_id {plan.order_id} fill={float(plan.fill_count):.2f}"
        if getattr(plan, "error", None):
            msg += f"\nERROR: {plan.error}"
        self.send(msg)

    def info(self, text: str, *, important: bool = False) -> None:
        if self.quiet and not important:
            return
        self.send(text)

    def daily_digest(self, text: str) -> None:
        self.send(text)

    def previous_run_recap(self) -> None:
        """Send a fixed recap of the pre-restart paper run (cloud wipe)."""
        if self.quiet:
            return
        self.send(
            "PREVIOUS RUN RECAP (before cloud wipe / restart)\n"
            "----------------------------------------------\n"
            "Days active : ~Thu Jul 24 → Sat Jul 25 (paper)\n"
            "Started     : $100.00\n"
            "Peak        : ~$172.35\n"
            "Near crash  : ~$153–155\n"
            "Total P/L   : ~+$55\n"
            "Record      : ~67W / 82L (~45% WR)\n"
            "Notes       : Mostly BELOW-heavy soft tape; phone Kalshi\n"
            "              outcomes matched bot settles.\n"
            "----------------------------------------------\n"
            "New run starting fresh at $100 / $5 face."
        )
