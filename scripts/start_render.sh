#!/usr/bin/env bash
# Start the paper bot on Render (or any host) with disk-backed paths.
set -euo pipefail

DATA_DIR="${DATA_DIR:-/var/data}"
mkdir -p "${DATA_DIR}/logs" "${DATA_DIR}/backups"

export DATABASE_URL="${DATABASE_URL:-sqlite:///${DATA_DIR}/paper_trading.db}"
export LOG_DIR="${LOG_DIR:-${DATA_DIR}/logs}"
export LOG_FILE="${LOG_FILE:-bot.log}"
export BACKUP_DIR="${BACKUP_DIR:-${DATA_DIR}/backups}"
export CALIBRATION_LOG="${CALIBRATION_LOG:-${DATA_DIR}/logs/calibration.csv}"

cd "$(dirname "$0")/.."

# Optional one-shot reset: set RESET_PAPER_HISTORY=true in Render env, then remove it.
if [[ "${RESET_PAPER_HISTORY:-false}" =~ ^(1|true|yes|on)$ ]]; then
  exec python main.py --reset-paper
fi
exec python main.py
