#!/usr/bin/env python3
"""Haircut + 8¢ vs the bid is why live maker sat at 0 fills."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.kalshi_fees import net_edge
from prediction.probability import apply_prob_haircut


def test_typical_haircut_maker_misses_min_edge() -> None:
    # Live MAD raw ~78% → haircut 0.55 → ~65.4%.
    fair = apply_prob_haircut(0.78, factor=0.55)
    assert abs(fair - 0.654) < 1e-9
    # Resting on a 60–63¢ bid cannot clear 8¢ after maker fees.
    assert net_edge(fair, 0.60, 5.0, maker=True) < 0.08
    assert net_edge(fair, 0.63, 5.0, maker=True) < 0.08
    # Crossing a softer ask (the paper path) still can.
    assert net_edge(fair, 0.55, 5.0, maker=False) >= 0.08


if __name__ == "__main__":
    test_typical_haircut_maker_misses_min_edge()
    print("test_maker_starvation ok")
