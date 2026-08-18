#!/usr/bin/env python3
"""Guards for unattended paper: haircut + day/bank halt."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.auto_halt import (
    RiskSnapshot,
    load_auto_halt,
    snapshot_from_rows,
)
from prediction.probability import apply_prob_haircut, haircut_factor


def test_haircut_maps_live_mad_overconfidence() -> None:
    # 78% raw → ~65% with default 0.55, matching ~67% realized.
    fair = apply_prob_haircut(0.78, factor=0.55)
    assert abs(fair - 0.654) < 1e-9, fair
    assert abs(apply_prob_haircut(0.22, factor=0.55) - 0.346) < 1e-9
    assert apply_prob_haircut(0.78, factor=1.0) == 0.78
    assert apply_prob_haircut(0.90, factor=0.0) == 0.5


def test_haircut_factor_reads_env() -> None:
    import os

    old = os.environ.get("PROB_HAIRCUT")
    os.environ["PROB_HAIRCUT"] = "0.55"
    try:
        assert abs(haircut_factor() - 0.55) < 1e-12
    finally:
        if old is None:
            os.environ.pop("PROB_HAIRCUT", None)
        else:
            os.environ["PROB_HAIRCUT"] = old


def test_day_loss_halts_and_clears_next_morning() -> None:
    halt = load_auto_halt(
        enabled=True,
        tz_name="America/Los_Angeles",
        bank_floor=30,
        day_loss=15,
        day_min_bets=25,
        day_max_wr=0.56,
    )
    # 16:00 UTC 17 Aug 2026 = 09:00 LA — still Aug 17 locally.
    now = datetime(2026, 8, 17, 16, 0, tzinfo=timezone.utc)
    la = ZoneInfo("America/Los_Angeles")
    rows = []
    for i in range(58):
        win = i < 33  # 57% WR
        pnl = 1.75 if win else -3.25
        when = datetime(2026, 8, 17, 8 + (i % 8), 0, tzinfo=timezone.utc)
        rows.append((pnl, when, "WON" if win else "LOST"))
    snap = snapshot_from_rows(
        bank=62.0, rows=rows, tz_name="America/Los_Angeles", now=now
    )
    assert snap.today == now.astimezone(la).date()
    assert snap.today_n == 58
    decision = halt.evaluate(snap)
    assert decision is not None
    assert decision.reason == "day loss"
    # Next LA morning (Aug 18 08:00 local = 15:00 UTC) with no new bets → clear.
    morning = datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc)
    later = snapshot_from_rows(
        bank=62.0, rows=rows, tz_name="America/Los_Angeles", now=morning
    )
    assert later.today_n == 0
    assert halt.evaluate(later) is None


def test_day_wr_halt_without_hitting_dollar_cap() -> None:
    halt = load_auto_halt(
        enabled=True,
        tz_name="America/Los_Angeles",
        bank_floor=30,
        day_loss=15,
        day_min_bets=25,
        day_max_wr=0.56,
    )
    now = datetime(2026, 8, 17, 16, 0, tzinfo=timezone.utc)
    rows = []
    # 25 bets, 56% WR, small red day (−$8) — WR rule, not dollar cap.
    for i in range(25):
        win = i < 14
        pnl = 1.0 if win else -2.0  # 14*1 + 11*(-2) = -8
        when = datetime(2026, 8, 17, 12, i % 50, tzinfo=timezone.utc)
        rows.append((pnl, when, "WON" if win else "LOST"))
    snap = snapshot_from_rows(
        bank=70.0, rows=rows, tz_name="America/Los_Angeles", now=now
    )
    assert snap.today_pnl == -8.0
    decision = halt.evaluate(snap)
    assert decision is not None
    assert decision.reason == "day wr"


def test_bank_floor_and_disabled() -> None:
    halt = load_auto_halt(
        enabled=True,
        tz_name="America/Los_Angeles",
        bank_floor=30,
        day_loss=15,
        day_min_bets=25,
        day_max_wr=0.56,
    )
    floor_hit = RiskSnapshot(
        bank=29.99,
        today=__import__("datetime").date(2026, 8, 18),
        today_n=0,
        today_wins=0,
        today_pnl=0.0,
    )
    assert halt.evaluate(floor_hit) is not None
    ok = RiskSnapshot(
        bank=30.01,
        today=floor_hit.today,
        today_n=0,
        today_wins=0,
        today_pnl=0.0,
    )
    assert halt.evaluate(ok) is None
    off = load_auto_halt(
        enabled=False,
        tz_name="America/Los_Angeles",
        bank_floor=30,
        day_loss=15,
        day_min_bets=25,
        day_max_wr=0.56,
    )
    assert off.evaluate(floor_hit) is None


if __name__ == "__main__":
    test_haircut_maps_live_mad_overconfidence()
    test_haircut_factor_reads_env()
    test_day_loss_halts_and_clears_next_morning()
    test_day_wr_halt_without_hitting_dollar_cap()
    test_bank_floor_and_disabled()
    print("auto-guard tests ok")
