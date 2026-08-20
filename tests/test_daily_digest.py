#!/usr/bin/env python3
"""Daily Telegram scorecard helpers."""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.digest import (
    DailyDigest,
    Slice,
    digest_due,
    digest_stamp_path,
    format_digest,
)


def _digest(**overrides) -> DailyDigest:
    base = dict(
        local_day=date(2026, 8, 20),
        tz_name="America/Los_Angeles",
        live=False,
        auto_bet=True,
        haircut=0.55,
        bank=57.46,
        vaulted=50.0,
        full=Slice(n=552, wins=375, pnl=65.16),
        since_haircut=Slice(n=31, wins=23, pnl=15.35),
        today=Slice(n=3, wins=3, pnl=5.58),
        last50=Slice(n=50, wins=37, pnl=22.59),
        halt_detail=None,
        blackout_label="19:00–23:00 America/Los_Angeles (no new bets)",
    )
    base.update(overrides)
    return DailyDigest(**base)


def test_stay_paper_under_40() -> None:
    text = format_digest(_digest())
    assert "STAY PAPER — haircut sample 31" in text
    assert "Bank $57.46" in text
    assert "SINCE_HAIRCUT: n=31 wr=74.2% pnl=$+15.35" in text


def test_sample_ok_at_40() -> None:
    d = _digest(since_haircut=Slice(n=40, wins=28, pnl=12.0))
    assert "PAPER sample OK" in d.call_line()


def test_halt_and_kill_day() -> None:
    halted = _digest(halt_detail="day loss -$18.00")
    assert halted.call_line().startswith("HALTED")
    knife = _digest(
        since_haircut=Slice(n=50, wins=30, pnl=5.0),
        today=Slice(n=40, wins=22, pnl=-16.0),
    )
    assert "kill day" in knife.call_line()


def test_digest_due_once_per_local_morning() -> None:
    tz = "America/Los_Angeles"
    # 6:59 AM LA = 13:59 UTC in PDT
    early = datetime(2026, 8, 20, 13, 59, tzinfo=timezone.utc)
    assert early.astimezone(ZoneInfo(tz)).hour == 6
    assert digest_due(tz_name=tz, hour=7, last_sent=None, now=early) is False
    after = datetime(2026, 8, 20, 14, 5, tzinfo=timezone.utc)
    assert after.astimezone(ZoneInfo(tz)).hour == 7
    assert digest_due(tz_name=tz, hour=7, last_sent=None, now=after) is True
    assert digest_due(tz_name=tz, hour=7, last_sent=date(2026, 8, 20), now=after) is False


def test_stamp_path_render_sqlite() -> None:
    path = digest_stamp_path("sqlite:////var/data/live_mad_v2.db")
    assert path.name == "live_mad_v2.db.digest_date"
    assert str(path.parent) == "/var/data"


if __name__ == "__main__":
    test_stay_paper_under_40()
    test_sample_ok_at_40()
    test_halt_and_kill_day()
    test_digest_due_once_per_local_morning()
    test_stamp_path_render_sqlite()
    print("daily digest tests ok")
