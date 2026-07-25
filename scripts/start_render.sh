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

cd "$(dirname "$0")/.."

RESET_FLAG="${DATA_DIR}/.paper_reset_done"
want_reset=false
if [[ "${RESET_PAPER_HISTORY:-false}" =~ ^(1|true|yes|on)$ ]]; then
  want_reset=true
fi

# Keep the worker alive across crashes / brief Render blips.
while true; do
  if [[ "${want_reset}" == "true" && ! -f "${RESET_FLAG}" ]]; then
    echo "[start_render] first boot reset → $100 bank"
    python main.py --reset-paper
    exit_code=$?
    # Only mark reset done if the process ran long enough to init DB,
    # or if it exited cleanly after a short intentional stop.
    if [[ -f "${DATA_DIR}/paper_trading.db" ]]; then
      touch "${RESET_FLAG}"
      echo "[start_render] reset complete; future restarts will NOT wipe bank"
    fi
  else
    echo "[start_render] starting bot (no reset)"
    python main.py
    exit_code=$?
  fi

  echo "[start_render] bot exited code=${exit_code:-?} — restarting in 5s"
  sleep 5
done
