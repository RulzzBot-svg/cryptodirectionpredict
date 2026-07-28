#!/usr/bin/env python3
"""Break paper results down by hour of day, side, and entry price.

Read-only: never writes to the DB. Safe to run while the bot is live.

Usage:
  python scripts/hourly_pnl.py                      # uses DATABASE_URL / DATA_DIR
  python scripts/hourly_pnl.py --db /var/data/paper_trading.db
  python scripts/hourly_pnl.py --tz America/Los_Angeles
  python scripts/hourly_pnl.py --csv backups/bets.latest.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SETTLED = ("WON", "LOST", "PUSH")


@dataclass
class Bucket:
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    staked: float = 0.0
    entry_prices: list[float] = field(default_factory=list)

    @property
    def settled(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> Optional[float]:
        return (self.wins / self.settled * 100.0) if self.settled else None

    @property
    def avg_entry(self) -> Optional[float]:
        return (sum(self.entry_prices) / len(self.entry_prices)) if self.entry_prices else None

    @property
    def roi_pct(self) -> Optional[float]:
        return (self.pnl / self.staked * 100.0) if self.staked else None


@dataclass
class Row:
    placed_at: datetime
    side: str
    status: str
    contract_price: float
    contract_cost: float
    pnl: float


def _resolve_db(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit)
    url = os.getenv("DATABASE_URL", "")
    if url.startswith("sqlite:///"):
        return Path(url[len("sqlite:///") :])
    data_dir = Path(os.getenv("DATA_DIR", "/var/data"))
    candidate = data_dir / "paper_trading.db"
    if candidate.exists():
        return candidate
    return ROOT / "paper_trading.db"


def _parse_dt(raw: object) -> Optional[datetime]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        # SQLite stores e.g. "2026-07-28 03:45:00.123456"
        text = str(raw).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_from_db(db_path: Path) -> list[Row]:
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        raw = conn.execute(
            "SELECT placed_at, side, status, contract_price, contract_cost, pnl "
            "FROM prediction_bets ORDER BY id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise SystemExit(f"Could not read prediction_bets: {exc}") from exc
    finally:
        conn.close()
    return _to_rows(raw)


def load_from_csv(csv_path: Path) -> list[Row]:
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")
    with csv_path.open(newline="", encoding="utf-8") as fh:
        return _to_rows(list(csv.DictReader(fh)))


def _to_rows(raw_rows: Iterable[dict]) -> list[Row]:
    rows: list[Row] = []
    for r in raw_rows:
        status = str(r["status"] or "").upper()
        if status not in SETTLED:
            continue
        placed = _parse_dt(r["placed_at"])
        if placed is None:
            continue
        try:
            rows.append(
                Row(
                    placed_at=placed,
                    side=str(r["side"] or "").upper(),
                    status=status,
                    contract_price=float(r["contract_price"] or 0),
                    contract_cost=float(r["contract_cost"] or 0),
                    pnl=float(r["pnl"] or 0),
                )
            )
        except (TypeError, ValueError):
            continue
    return rows


def _add(bucket: Bucket, row: Row) -> None:
    if row.status == "WON":
        bucket.wins += 1
    elif row.status == "LOST":
        bucket.losses += 1
    bucket.pnl += row.pnl
    bucket.staked += row.contract_cost
    bucket.entry_prices.append(row.contract_price)


def _fmt_money(value: float) -> str:
    return f"+${value:,.2f}" if value >= 0 else f"-${abs(value):,.2f}"


def _print_table(title: str, buckets: dict[str, Bucket], *, label: str) -> None:
    print()
    print(title)
    print("-" * 72)
    print(f"{label:<12}{'Bets':>6}{'W/L':>10}{'WR':>8}{'Avg buy':>10}{'P/L':>12}{'ROI':>8}")
    print("-" * 72)
    for key in buckets:
        b = buckets[key]
        if not b.settled:
            continue
        wr = f"{b.win_rate:.0f}%" if b.win_rate is not None else "—"
        avg = f"{b.avg_entry*100:.0f}¢" if b.avg_entry is not None else "—"
        roi = f"{b.roi_pct:+.0f}%" if b.roi_pct is not None else "—"
        print(
            f"{key:<12}{b.settled:>6}{f'{b.wins}W/{b.losses}L':>10}{wr:>8}"
            f"{avg:>10}{_fmt_money(b.pnl):>12}{roi:>8}"
        )
    print("-" * 72)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Paper results by hour / side / price")
    parser.add_argument("--db", help="Path to paper_trading.db")
    parser.add_argument("--csv", help="Read a bets CSV backup instead of the DB")
    parser.add_argument(
        "--tz",
        default=os.getenv("REPORT_TZ", "UTC"),
        help="Timezone for hour labels (e.g. America/Los_Angeles)",
    )
    parser.add_argument(
        "--block",
        type=int,
        default=1,
        help="Group hours into blocks of N (e.g. 3 for 3-hour blocks)",
    )
    args = parser.parse_args(argv)

    try:
        tz = ZoneInfo(args.tz)
    except (ZoneInfoNotFoundError, ValueError):
        print(f"Unknown timezone {args.tz!r}; using UTC")
        tz = timezone.utc

    if args.csv:
        rows = load_from_csv(Path(args.csv))
        source = args.csv
    else:
        db_path = _resolve_db(args.db)
        rows = load_from_db(db_path)
        source = str(db_path)

    if not rows:
        print(f"No settled bets found in {source}")
        return 1

    overall = Bucket()
    by_hour: dict[str, Bucket] = defaultdict(Bucket)
    by_side: dict[str, Bucket] = defaultdict(Bucket)
    by_price: dict[str, Bucket] = defaultdict(Bucket)

    block = max(1, args.block)
    for row in rows:
        _add(overall, row)
        local_hour = row.placed_at.astimezone(tz).hour
        start = (local_hour // block) * block
        label = f"{start:02d}:00" if block == 1 else f"{start:02d}-{(start + block) % 24:02d}"
        _add(by_hour[label], row)
        _add(by_side[row.side or "?"], row)
        cents = row.contract_price * 100
        if cents < 30:
            price_label = "<30¢"
        elif cents < 45:
            price_label = "30-44¢"
        elif cents < 60:
            price_label = "45-59¢"
        elif cents < 75:
            price_label = "60-74¢"
        else:
            price_label = "75¢+"
        _add(by_price[price_label], row)

    first = min(r.placed_at for r in rows).astimezone(tz)
    last = max(r.placed_at for r in rows).astimezone(tz)
    print("=" * 72)
    print("  PAPER RESULTS BREAKDOWN")
    print("=" * 72)
    print(f"  Source     : {source}")
    print(f"  Timezone   : {args.tz}")
    print(f"  Range      : {first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M}")
    if overall.win_rate is not None:
        print(
            f"  Settled    : {overall.settled} "
            f"({overall.wins}W/{overall.losses}L, {overall.win_rate:.1f}% WR)"
        )
    else:
        print(f"  Settled    : {overall.settled}")
    print(f"  Realized   : {_fmt_money(overall.pnl)} on {_fmt_money(overall.staked)} staked")
    if overall.roi_pct is not None:
        print(f"  ROI        : {overall.roi_pct:+.1f}% of money risked")

    hour_order = dict(sorted(by_hour.items(), key=lambda kv: kv[0]))
    _print_table(f"  By hour placed ({args.tz})", hour_order, label="Hour")
    _print_table("  By side", dict(sorted(by_side.items())), label="Side")
    price_order = ["<30¢", "30-44¢", "45-59¢", "60-74¢", "75¢+"]
    _print_table(
        "  By entry price",
        {k: by_price[k] for k in price_order if k in by_price},
        label="Paid",
    )

    print()
    print("  Note: single-hour buckets are noisy. Use --block 3 or --block 6")
    print("  before concluding a time-of-day effect is real.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
