"""Free disk space for Render workers without touching the live paper DB."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Never delete these — live paper state
_PROTECTED_NAMES = {
    "paper_trading.db",
    "paper_trading.db-wal",
    "paper_trading.db-shm",
    "paper_trading.latest.db",
    ".paper_reset_done",
}


def _unlink(path: Path) -> int:
    try:
        size = path.stat().st_size if path.exists() else 0
        path.unlink(missing_ok=True)
        return size
    except OSError as exc:
        logger.warning("Could not delete %s: %s", path, exc)
        return 0


def _keep_newest(paths: Iterable[Path], keep: int) -> int:
    files = sorted((p for p in paths if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    freed = 0
    for stale in files[max(0, keep) :]:
        if stale.name in _PROTECTED_NAMES:
            continue
        freed += _unlink(stale)
    return freed


def _truncate_file(path: Path, *, keep_bytes: int = 0) -> int:
    if not path.is_file():
        return 0
    try:
        size = path.stat().st_size
        if size <= keep_bytes:
            return 0
        with path.open("wb") as fh:
            if keep_bytes > 0:
                # keep only the tail
                with path.open("rb") as src:
                    src.seek(max(0, size - keep_bytes))
                    fh.write(src.read())
            else:
                fh.truncate(0)
        return size - keep_bytes
    except OSError as exc:
        logger.warning("Could not truncate %s: %s", path, exc)
        return 0


def disk_usage(path: Path) -> tuple[int, int, int]:
    """Return (total, used, free) bytes for the filesystem containing path."""
    usage = shutil.disk_usage(path)
    return usage.total, usage.used, usage.free


def prune_runtime_disk(
    *,
    data_dir: Optional[Path] = None,
    backup_dir: Optional[Path] = None,
    log_dir: Optional[Path] = None,
    backup_keep: Optional[int] = None,
    min_free_mb: float = 100.0,
) -> dict[str, int]:
    """
    Delete old stamped backups + oversized logs.

    Never deletes the live ``paper_trading.db``. Safe to run on every boot.
    """
    data = Path(data_dir or os.getenv("DATA_DIR", "/var/data"))
    backups = Path(backup_dir or os.getenv("BACKUP_DIR", str(data / "backups")))
    logs = Path(log_dir or os.getenv("LOG_DIR", str(data / "logs")))
    keep = int(backup_keep if backup_keep is not None else os.getenv("BACKUP_KEEP", "6"))
    keep = max(1, keep)

    freed = 0
    deleted = 0

    if backups.is_dir():
        for pattern in (
            "paper_trading_*.db",
            "bot_*.log",
            "calibration_*.csv",
            "bets_*.csv",
        ):
            before = list(backups.glob(pattern))
            # Always keep .latest.* companions; stamped only
            stamped = [p for p in before if ".latest." not in p.name]
            got = _keep_newest(stamped, keep)
            if got:
                deleted += 1
            freed += got
        # Stamped DBs already handled; also drop huge latest logs if needed
        for name in ("bot.latest.log",):
            p = backups / name
            if p.exists() and p.stat().st_size > 2 * 1024 * 1024:
                freed += _truncate_file(p, keep_bytes=512 * 1024)

    if logs.is_dir():
        # Rotated logs: bot.log.1 ... bot.log.N
        for p in logs.glob("bot.log.*"):
            freed += _unlink(p)
            deleted += 1
        bot_log = logs / os.getenv("LOG_FILE", "bot.log")
        max_log = int(os.getenv("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
        if bot_log.exists() and bot_log.stat().st_size > max_log:
            freed += _truncate_file(bot_log, keep_bytes=max_log // 2)
        # Calibration can grow; keep file but trim if gigantic
        cal = Path(os.getenv("CALIBRATION_LOG", str(logs / "calibration.csv")))
        if cal.exists() and cal.stat().st_size > 5 * 1024 * 1024:
            freed += _truncate_file(cal, keep_bytes=1 * 1024 * 1024)

    # If still critically low, nuke all stamped backups except newest DB + latest copies
    try:
        _total, _used, free = disk_usage(data if data.exists() else Path("/"))
    except OSError:
        free = 0
    min_free = int(min_free_mb * 1024 * 1024)
    if free < min_free and backups.is_dir():
        stamped_dbs = sorted(
            (p for p in backups.glob("paper_trading_*.db") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in stamped_dbs[1:]:
            freed += _unlink(stale)
        for pattern in ("bot_*.log", "calibration_*.csv", "bets_*.csv"):
            for p in backups.glob(pattern):
                if ".latest." in p.name:
                    continue
                freed += _unlink(p)
        # Drop latest log copies entirely — DB is what matters
        for name in ("bot.latest.log",):
            p = backups / name
            if p.exists():
                freed += _unlink(p)

    return {"freed_bytes": freed, "free_bytes": free, "backup_keep": keep}
