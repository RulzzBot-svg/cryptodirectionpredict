#!/usr/bin/env python3
"""Is the model honest? Compare predicted probabilities to what actually happened.

The bot bets when its probability exceeds the market price. That edge is only
real if the probabilities are calibrated — when it says 27%, those bets should
win about 27% of the time. If they win materially less, the "edge" is
arithmetic on a wrong number and no execution fix will rescue it.

Read-only. Safe to run against a live database.

Usage:
  python scripts/calibration_report.py --db /var/data/paper_trading.db
  python scripts/calibration_report.py --db /var/data/live_v4.db --buckets 5
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class Bucket:
    label: str
    predicted: list[float] = field(default_factory=list)
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    staked: float = 0.0

    @property
    def n(self) -> int:
        return self.wins + self.losses

    @property
    def avg_predicted(self) -> Optional[float]:
        return sum(self.predicted) / len(self.predicted) if self.predicted else None

    @property
    def actual(self) -> Optional[float]:
        return self.wins / self.n if self.n else None


def _resolve_db(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit)
    url = os.getenv("DATABASE_URL", "")
    if url.startswith("sqlite:///"):
        return Path(url[len("sqlite:///") :])
    return ROOT / "paper_trading.db"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Model calibration report")
    parser.add_argument("--db", help="Path to the bot database")
    parser.add_argument("--buckets", type=int, default=7)
    parser.add_argument(
        "--min-n",
        type=int,
        default=5,
        help="Hide buckets with fewer settled bets than this",
    )
    args = parser.parse_args(argv)

    db_path = _resolve_db(args.db)
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT side, status, model_prob, contract_price, contract_cost, "
            "pnl, edge FROM prediction_bets "
            "WHERE status IN ('WON','LOST') ORDER BY id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise SystemExit(f"Could not read prediction_bets: {exc}") from exc
    finally:
        conn.close()

    if not rows:
        print(f"No settled bets in {db_path}")
        return 1

    n_buckets = max(2, args.buckets)
    buckets: list[Bucket] = []
    for i in range(n_buckets):
        lo = i / n_buckets
        hi = (i + 1) / n_buckets
        buckets.append(Bucket(label=f"{lo*100:.0f}-{hi*100:.0f}%"))

    total_pred = 0.0
    total_win = 0
    total_n = 0
    brier = 0.0
    edge_pred = 0.0
    price_paid = 0.0
    realized = 0.0
    staked = 0.0

    for r in rows:
        try:
            prob = float(r["model_prob"])
            price = float(r["contract_price"])
            cost = float(r["contract_cost"])
            pnl = float(r["pnl"] or 0.0)
        except (TypeError, ValueError):
            continue
        won = r["status"] == "WON"
        idx = min(n_buckets - 1, int(prob * n_buckets))
        b = buckets[idx]
        b.predicted.append(prob)
        b.pnl += pnl
        b.staked += cost
        if won:
            b.wins += 1
        else:
            b.losses += 1

        total_pred += prob
        total_win += 1 if won else 0
        total_n += 1
        brier += (prob - (1.0 if won else 0.0)) ** 2
        edge_pred += prob - price
        price_paid += price
        realized += pnl
        staked += cost

    avg_pred = total_pred / total_n
    avg_actual = total_win / total_n
    avg_price = price_paid / total_n

    print("=" * 74)
    print("  MODEL CALIBRATION")
    print("=" * 74)
    print(f"  Database        : {db_path}")
    print(f"  Settled bets    : {total_n}")
    print(f"  Avg predicted   : {avg_pred*100:.1f}%   (model's own confidence)")
    print(f"  Avg actual      : {avg_actual*100:.1f}%   (what really happened)")
    gap = (avg_actual - avg_pred) * 100
    verdict = (
        "well calibrated"
        if abs(gap) < 3
        else ("OVERCONFIDENT — predicts more than it delivers" if gap < 0 else "underconfident")
    )
    print(f"  Gap             : {gap:+.1f} pts  → {verdict}")
    print(f"  Brier score     : {brier/total_n:.4f}   (lower is better; 0.25 = coin flip)")
    print()
    print(f"  Avg price paid  : {avg_price*100:.1f}¢")
    print(f"  Edge claimed    : {edge_pred/total_n*100:+.1f}¢ per bet")
    print(f"  Edge realized   : {(avg_actual - avg_price)*100:+.1f}¢ per bet")
    print(f"  Realized P/L    : ${realized:,.2f} on ${staked:,.2f} staked")

    print()
    print("  Predicted vs actual, by confidence bucket")
    print("-" * 74)
    print(f"{'Model says':<14}{'Bets':>6}{'Predicted':>12}{'Actual':>10}{'Gap':>9}{'P/L':>12}")
    print("-" * 74)
    for b in buckets:
        if b.n < args.min_n:
            continue
        pred = b.avg_predicted or 0.0
        act = b.actual or 0.0
        pnl_txt = f"+${b.pnl:,.2f}" if b.pnl >= 0 else f"-${abs(b.pnl):,.2f}"
        print(
            f"{b.label:<14}{b.n:>6}{pred*100:>11.1f}%{act*100:>9.1f}%"
            f"{(act-pred)*100:>+8.1f}{pnl_txt:>12}"
        )
    print("-" * 74)

    print()
    if avg_actual - avg_price > 0.02:
        print("  Read: bets win more often than they cost. The edge is real —")
        print("  what's left is an execution problem (fills, fees, latency).")
    elif avg_actual - avg_price > 0:
        print("  Read: barely ahead of the price paid. Thin, and fees matter a lot.")
    else:
        print("  Read: bets win LESS often than their price implies. The model is")
        print("  overconfident and no execution fix will make this profitable.")
        print("  Raising MIN_EDGE or recalibrating the model is the priority.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
