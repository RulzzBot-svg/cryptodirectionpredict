"""File + console logging for the prediction bot."""

from __future__ import annotations

import atexit
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TextIO


DEFAULT_LOG_DIR = "logs"
DEFAULT_LOG_FILE = "bot.log"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 2


class _TeeTextIO:
    """
    Mirror writes to the real console and a log file.

    Carriage-return status updates (``\\r``) stay in-place on the console but
    become normal newlines in the log file so every ~10s tick is preserved.
    """

    def __init__(self, console: TextIO, log_file: TextIO) -> None:
        self._console = console
        self._log_file = log_file
        self._file_buf = ""

    @property
    def encoding(self) -> str:
        return getattr(self._console, "encoding", None) or "utf-8"

    def write(self, data: str) -> int:
        if not data:
            return 0
        self._console.write(data)
        normalized = data.replace("\r\n", "\n").replace("\r", "\n")
        if normalized:
            self._file_buf += normalized
            while "\n" in self._file_buf:
                line, self._file_buf = self._file_buf.split("\n", 1)
                if line.strip():
                    try:
                        self._log_file.write(line.rstrip() + "\n")
                        self._log_file.flush()
                    except OSError:
                        # Disk full mid-run — keep console output alive
                        self._file_buf = ""
                        break
        return len(data)

    def flush(self) -> None:
        self._console.flush()
        if self._file_buf.strip():
            self._log_file.write(self._file_buf.rstrip() + "\n")
            self._file_buf = ""
        self._log_file.flush()

    def isatty(self) -> bool:
        return bool(getattr(self._console, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self._console.fileno()

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def close(self) -> None:
        self.flush()


_tee_stdout: Optional[_TeeTextIO] = None
_tee_stderr: Optional[_TeeTextIO] = None
_log_fp: Optional[TextIO] = None
_original_stdout: Optional[TextIO] = None
_original_stderr: Optional[TextIO] = None
_log_path: Optional[Path] = None
_configured = False


def log_path_from_env() -> Path:
    log_dir = Path(os.getenv("LOG_DIR", DEFAULT_LOG_DIR))
    log_name = os.getenv("LOG_FILE", DEFAULT_LOG_FILE)
    return log_dir / log_name


def _rotate_if_needed(path: Path, *, max_bytes: int, backup_count: int) -> None:
    if not path.exists() or path.stat().st_size < max_bytes or backup_count <= 0:
        return
    # bot.log -> bot.log.1 ... bot.log.N (drop oldest)
    for idx in range(backup_count, 0, -1):
        src = path if idx == 1 else Path(f"{path}.{idx - 1}")
        dst = Path(f"{path}.{idx}")
        if not src.exists():
            continue
        if idx == backup_count:
            src.unlink(missing_ok=True)
        else:
            src.replace(dst)


def _emergency_clear_logs(path: Path) -> None:
    """Last-resort free space so the bot can start after ENOSPC."""
    parent = path.parent
    for p in parent.glob(f"{path.name}*"):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        path.write_text("", encoding="utf-8")
    except OSError:
        pass


def setup_bot_logging(
    *,
    log_path: Optional[Path] = None,
    level: int = logging.INFO,
) -> Path:
    """
    Tee stdout/stderr into ``logs/bot.log`` and route the logging module there too.

    Safe to call once at process start. Returns the log file path.
    If the disk is full, truncates/rotates aggressively and falls back to
    console-only logging rather than crash-looping.
    """
    global _configured, _tee_stdout, _tee_stderr, _log_fp
    global _original_stdout, _original_stderr, _log_path

    if _configured and _log_path is not None:
        return _log_path

    path = Path(log_path) if log_path is not None else log_path_from_env()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Fall through to console-only below
        path = Path(os.getenv("LOG_DIR", DEFAULT_LOG_DIR)) / os.getenv(
            "LOG_FILE", DEFAULT_LOG_FILE
        )

    max_bytes = int(os.getenv("LOG_MAX_BYTES", str(DEFAULT_MAX_BYTES)))
    backup_count = int(os.getenv("LOG_BACKUP_COUNT", str(DEFAULT_BACKUP_COUNT)))
    try:
        _rotate_if_needed(path, max_bytes=max_bytes, backup_count=backup_count)
    except OSError:
        _emergency_clear_logs(path)

    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        _log_fp = open(path, "a", encoding="utf-8", buffering=1)
        _log_fp.write(f"\n===== bot session start {started} =====\n")
        _log_fp.flush()
    except OSError as exc:
        # Disk full — wipe rotated logs / truncate and retry once
        sys.__stderr__.write(
            f"[bot.logging] log write failed ({exc}); clearing old logs and retrying\n"
        )
        _emergency_clear_logs(path)
        try:
            _log_fp = open(path, "w", encoding="utf-8", buffering=1)
            _log_fp.write(f"\n===== bot session start {started} (after disk prune) =====\n")
            _log_fp.flush()
        except OSError as exc2:
            sys.__stderr__.write(
                f"[bot.logging] console-only mode (cannot write {path}: {exc2})\n"
            )
            _log_fp = None

    _original_stdout = sys.stdout
    _original_stderr = sys.stderr
    if _log_fp is not None:
        _tee_stdout = _TeeTextIO(sys.__stdout__, _log_fp)
        _tee_stderr = _TeeTextIO(sys.__stderr__, _log_fp)
        sys.stdout = _tee_stdout
        sys.stderr = _tee_stderr
    else:
        _tee_stdout = None
        _tee_stderr = None

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(level)
    root.addHandler(stream_handler)

    atexit.register(shutdown_bot_logging)
    _configured = True
    _log_path = path

    logging.getLogger("bot.logging").info(
        "Writing bot log to %s", path.resolve() if _log_fp else "(console only)"
    )
    return path


def shutdown_bot_logging() -> None:
    """Flush and restore stdout/stderr (idempotent)."""
    global _configured, _tee_stdout, _tee_stderr, _log_fp
    global _original_stdout, _original_stderr, _log_path

    if not _configured and _log_fp is None:
        return

    if _tee_stdout is not None:
        try:
            _tee_stdout.flush()
        except Exception:
            pass
    if _tee_stderr is not None:
        try:
            _tee_stderr.flush()
        except Exception:
            pass

    if _original_stdout is not None:
        sys.stdout = _original_stdout
    if _original_stderr is not None:
        sys.stderr = _original_stderr

    if _log_fp is not None:
        try:
            ended = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            _log_fp.write(f"===== bot session end {ended} =====\n")
            _log_fp.flush()
            _log_fp.close()
        except Exception:
            pass

    _tee_stdout = None
    _tee_stderr = None
    _log_fp = None
    _original_stdout = None
    _original_stderr = None
    _log_path = None
    _configured = False
