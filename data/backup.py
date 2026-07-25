"""Durable backups so paper history survives cloud environment rebuilds."""

from __future__ import annotations

import csv
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_BACKUP_DIR = "/opt/cursor/artifacts/paper-bot-backups"
DEFAULT_DB_PATH = Path("paper_trading.db")
DEFAULT_LOG_PATH = Path("logs/bot.log")
DEFAULT_CALIBRATION_PATH = Path("logs/calibration.csv")


def backup_dir() -> Path:
    return Path(os.getenv("BACKUP_DIR", DEFAULT_BACKUP_DIR))


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ensure_backup_dir() -> Path:
    path = backup_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_db_path(database_url: Optional[str] = None) -> Path:
    url = database_url or os.getenv("DATABASE_URL", "sqlite:///./paper_trading.db")
    if url.startswith("sqlite:///"):
        raw = url[len("sqlite:///"):]
        return Path(raw)
    return DEFAULT_DB_PATH


def restore_latest_backup(*, database_url: Optional[str] = None) -> bool:
    """
    If the local SQLite DB is missing, restore the newest backup copy.

    Returns True if a restore happened.
    """
    db_path = resolve_db_path(database_url)
    if db_path.exists() and db_path.stat().st_size > 0:
        return False

    root = backup_dir()
    if not root.exists():
        return False

    candidates = sorted(root.glob("paper_trading_*.db"), reverse=True)
    # Also accept a stable latest symlink/copy
    latest = root / "paper_trading.latest.db"
    if latest.exists():
        candidates = [latest] + candidates

    for src in candidates:
        if not src.exists() or src.stat().st_size <= 0:
            continue
        db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, db_path)
        logger.info("Restored paper DB from backup %s → %s", src, db_path)
        # Best-effort restore companion files
        for name, dest in (
            ("bot.latest.log", Path(os.getenv("LOG_DIR", "logs")) / os.getenv("LOG_FILE", "bot.log")),
            ("calibration.latest.csv", DEFAULT_CALIBRATION_PATH),
        ):
            buddy = root / name
            if buddy.exists() and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(buddy, dest)
        return True
    return False


def backup_now(
    *,
    database_url: Optional[str] = None,
    log_path: Optional[Path] = None,
    calibration_path: Optional[Path] = None,
) -> Optional[Path]:
    """Copy DB (+ log/calibration if present) into the durable backup directory."""
    root = ensure_backup_dir()
    db_path = resolve_db_path(database_url)
    if not db_path.exists():
        logger.warning("Backup skipped — DB missing at %s", db_path)
        return None

    stamp = _stamp()
    dest = root / f"paper_trading_{stamp}.db"
    shutil.copy2(db_path, dest)
    shutil.copy2(db_path, root / "paper_trading.latest.db")

    log_file = Path(log_path) if log_path else Path(os.getenv("LOG_DIR", "logs")) / os.getenv(
        "LOG_FILE", "bot.log"
    )
    if log_file.exists():
        shutil.copy2(log_file, root / f"bot_{stamp}.log")
        shutil.copy2(log_file, root / "bot.latest.log")

    cal = Path(calibration_path) if calibration_path else DEFAULT_CALIBRATION_PATH
    if cal.exists():
        shutil.copy2(cal, root / f"calibration_{stamp}.csv")
        shutil.copy2(cal, root / "calibration.latest.csv")

    # Export a human-readable bets CSV snapshot too
    _export_bets_csv(db_path, root / "bets.latest.csv")
    _export_bets_csv(db_path, root / f"bets_{stamp}.csv")

    # Keep last N stamped DB backups
    keep = int(os.getenv("BACKUP_KEEP", "48"))
    old = sorted(root.glob("paper_trading_*.db"), reverse=True)
    for stale in old[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass

    logger.info("Backed up paper DB to %s", dest)
    return dest


def _export_bets_csv(db_path: Path, dest: Path) -> None:
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "prediction_bets" not in tables:
            conn.close()
            return
        cols = [r[1] for r in conn.execute("PRAGMA table_info(prediction_bets)").fetchall()]
        if not cols:
            conn.close()
            return
        sql = f"SELECT {', '.join(cols)} FROM prediction_bets ORDER BY id"
        rows = conn.execute(sql).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Bet CSV export failed: %s", exc)
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        with dest.open("w", encoding="utf-8", newline="") as fh:
            csv.DictWriter(fh, fieldnames=cols).writeheader()
        return
    with dest.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
