#!/usr/bin/env bash
# Start the paper bot on Render with disk-backed paths + auto-restart.
set -uo pipefail

DATA_DIR="${DATA_DIR:-/var/data}"
mkdir -p "${DATA_DIR}/logs" "${DATA_DIR}/backups"

export DATABASE_URL="${DATABASE_URL:-sqlite:///${DATA_DIR}/paper_trading.db}"
export LOG_DIR="${LOG_DIR:-${DATA_DIR}/logs}"
export LOG_FILE="${LOG_FILE:-bot.log}"
export BACKUP_DIR="${BACKUP_DIR:-${DATA_DIR}/backups}"
export CALIBRATION_LOG="${CALIBRATION_LOG:-${DATA_DIR}/logs/calibration.csv}"
# Keep stamped DB backups small on the 1GB disk
export BACKUP_KEEP="${BACKUP_KEEP:-6}"
export LOG_MAX_BYTES="${LOG_MAX_BYTES:-2097152}"
export LOG_BACKUP_COUNT="${LOG_BACKUP_COUNT:-2}"

cd "$(dirname "$0")/.."

RESET_FLAG="${DATA_DIR}/.paper_reset_done"
want_reset=false
if [[ "${RESET_PAPER_HISTORY:-false}" =~ ^(1|true|yes|on)$ ]]; then
  want_reset=true
fi

fail_streak=0

prune_disk() {
  echo "[start_render] pruning disk (keeping paper_trading.db)…"
  python scripts/prune_disk.py || true
  df -h "${DATA_DIR}" 2>/dev/null || df -h / || true
}

# Free space once before the loop (bot is currently crash-looping on ENOSPC)
prune_disk

# Keep the worker alive across crashes / brief Render blips.
while true; do
  prune_disk

  if [[ "${want_reset}" == "true" && ! -f "${RESET_FLAG}" ]]; then
    echo "[start_render] first boot reset → $100 bank"
    python main.py --reset-paper
    exit_code=$?
    if [[ -f "${DATA_DIR}/paper_trading.db" ]]; then
      touch "${RESET_FLAG}"
      echo "[start_render] reset complete; future restarts will NOT wipe bank"
    fi
  else
    echo "[start_render] starting bot (no reset)"
    python main.py
    exit_code=$?
  fi

  if [[ "${exit_code:-1}" -eq 0 ]]; then
    fail_streak=0
  else
    fail_streak=$((fail_streak + 1))
  fi

  # Back off when crash-looping (e.g. disk full) so we don't thrash
  sleep_for=5
  if [[ "${fail_streak}" -ge 3 ]]; then
    sleep_for=30
  fi
  if [[ "${fail_streak}" -ge 10 ]]; then
    sleep_for=60
  fi

  echo "[start_render] bot exited code=${exit_code:-?} — restarting in ${sleep_for}s"
  sleep "${sleep_for}"
done
