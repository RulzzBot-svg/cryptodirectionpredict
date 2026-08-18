#!/usr/bin/env python3
"""CLI: free Render disk space (never deletes live paper_trading.db)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.disk_prune import disk_usage, prune_runtime_disk  # noqa: E402


def main() -> int:
    data = Path(os.getenv("DATA_DIR", "/var/data"))
    result = prune_runtime_disk(data_dir=data)
    try:
        total, used, free = disk_usage(data if data.exists() else Path("/"))
    except OSError as exc:
        print(f"[prune_disk] usage check failed: {exc}")
        total = used = free = 0
    print(
        f"[prune_disk] freed {result['freed_bytes'] / (1024*1024):.1f} MiB | "
        f"disk free {free / (1024*1024):.1f} MiB / "
        f"{total / (1024*1024):.1f} MiB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
