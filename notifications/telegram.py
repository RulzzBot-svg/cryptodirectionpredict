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
        pnl = float(bet.pnl or 0.0)
        sign = "+" if pnl >= 0 else "-"
        msg = (
            f"SETTLED {bet.status} ({bet.side})\n"
            f"Window {bet.window_id}\n"
            f"Final ${float(bet.settlement_price):,.2f} → {bet.outcome}\n"
            f"P/L {sign}${abs(pnl):,.2f} | Bank ${float(bet.usd_balance_after):,.2f}"
        )
        self.send(msg)

    def window_stats(self, stats: dict[str, Any]) -> None:
        pnl = float(stats.get("total_pnl") or 0.0)
        sign = "+" if pnl >= 0 else "-"
        msg = (
            f"WINDOW STATS\n"
            f"Bank ${float(stats.get('usd_balance') or 0):,.2f} | "
            f"Equity ${float(stats.get('equity') or 0):,.2f}\n"
            f"Total P/L {sign}${abs(pnl):,.2f}\n"
            f"W/L {stats.get('win_count', 0)}W/"
            f"{stats.get('loss_count', 0)}L "
            f"({float(stats.get('win_rate_pct') or 0):.1f}%) | "
            f"Settled {stats.get('settled_count', 0)}"
        )
        self.send(msg)

    def info(self, text: str) -> None:
        self.send(text)
