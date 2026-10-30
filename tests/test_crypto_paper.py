#!/usr/bin/env python3
"""Paper crypto spot bot: fees, one position, stop, day buy-halt, live locked."""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.crypto_risk import day_buy_halted, decide_crypto_action, stop_hit
from execution.paper_engine import PaperBroker
from models.db import create_db_engine, create_session_factory, init_db


def test_stop_and_decide() -> None:
    assert stop_hit(100.0, 96.9, 0.03) is True
    assert stop_hit(100.0, 97.1, 0.03) is False
    assert decide_crypto_action(
        signal="HOLD",
        in_position=True,
        entry=100.0,
        price=96.0,
        stop_pct=0.03,
        buy_halted=False,
    ) == "SELL"
    assert decide_crypto_action(
        signal="BUY",
        in_position=True,
        entry=100.0,
        price=110.0,
        stop_pct=0.03,
        buy_halted=False,
    ) == "HOLD"
    assert decide_crypto_action(
        signal="BUY",
        in_position=False,
        entry=0.0,
        price=110.0,
        stop_pct=0.03,
        buy_halted=True,
    ) == "HOLD"
    assert decide_crypto_action(
        signal="BUY",
        in_position=False,
        entry=0.0,
        price=110.0,
        stop_pct=0.03,
        buy_halted=False,
    ) == "BUY"
    assert decide_crypto_action(
        signal="SELL",
        in_position=True,
        entry=100.0,
        price=105.0,
        stop_pct=0.03,
        buy_halted=True,
    ) == "SELL"


def test_day_buy_halted_local_day() -> None:
    now = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)  # 1pm LA
    sells = [
        SimpleNamespace(
            side="SELL",
            realized_pnl=-10.5,
            timestamp=datetime(2026, 10, 30, 18, 0, tzinfo=timezone.utc),
        )
    ]
    assert day_buy_halted(sells, day_loss=10, now=now) is True
    yesterday = [
        SimpleNamespace(
            side="SELL",
            realized_pnl=-50.0,
            timestamp=datetime(2026, 10, 29, 23, 0, tzinfo=timezone.utc),
        )
    ]
    assert day_buy_halted(yesterday, day_loss=10, now=now) is False


def _broker(tmp: str, *, fee_rate: float) -> PaperBroker:
    url = f"sqlite:///{Path(tmp) / 'crypto_paper.db'}"
    engine = create_db_engine(url)
    init_db(engine)
    return PaperBroker(
        create_session_factory(engine),
        initial_balance=100.0,
        symbol="BTC/USD",
        engine=engine,
        fee_rate=fee_rate,
    )


def test_fees_drag_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp, fee_rate=0.006)
        buy = broker.execute_order("BUY", 100_000.0, position_size_pct=0.25)
        assert buy is not None
        pf = broker.get_portfolio()
        assert abs(pf["usd_balance"] - 75.0) < 1e-9
        # $25 spent, 0.6% fee → $24.85 of BTC at $100k
        assert abs(pf["btc_balance"] - (24.85 / 100_000.0)) < 1e-12
        assert pf["avg_entry_price"] > 100_000.0
        sell = broker.execute_order("SELL", 100_000.0)
        assert sell is not None
        assert float(sell.realized_pnl) < 0
        end = broker.get_portfolio()
        assert end["btc_balance"] == 0.0
        assert end["usd_balance"] < 100.0


def test_zero_fee_unchanged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp, fee_rate=0.0)
        broker.execute_order("BUY", 50_000.0, position_size_pct=1.0)
        sell = broker.execute_order("SELL", 55_000.0)
        assert sell is not None
        assert abs(float(sell.realized_pnl) - 10.0) < 1e-9


def test_live_crypto_flag_is_rejected() -> None:
    import crypto_bot

    old = os.environ.get("LIVE_CRYPTO")
    os.environ["LIVE_CRYPTO"] = "true"
    try:
        raised = False
        try:
            import asyncio

            asyncio.run(
                crypto_bot.run_crypto_paper(
                    database_url="sqlite:///:memory:",
                    symbol="BTC/USD",
                    provider="coinbase",
                    timeframe="1h",
                    loop_seconds=1,
                    fee_rate=0.006,
                    stop_pct=0.03,
                    position_pct=0.25,
                    day_loss=10,
                    tz_name="America/Los_Angeles",
                    initial_balance=100,
                    ohlcv_limit=20,
                )
            )
        except SystemExit as exc:
            raised = True
            assert "paper only" in str(exc).lower() or "LIVE_CRYPTO" in str(exc)
        assert raised
    finally:
        if old is None:
            os.environ.pop("LIVE_CRYPTO", None)
        else:
            os.environ["LIVE_CRYPTO"] = old


if __name__ == "__main__":
    test_stop_and_decide()
    test_day_buy_halted_local_day()
    test_fees_drag_round_trip()
    test_zero_fee_unchanged()
    test_live_crypto_flag_is_rejected()
    print("crypto paper tests ok")
