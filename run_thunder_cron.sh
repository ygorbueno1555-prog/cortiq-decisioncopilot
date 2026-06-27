#!/usr/bin/env bash
# run_thunder_cron.sh — Shell wrapper for thunder_cron.py
#
# Put in crontab. Example schedules:
#   every 30 min:  */30 * * * * /path/to/run_thunder_cron.sh
#   every hour:    0    * * * * /path/to/run_thunder_cron.sh
#   every 15 min:  */15 * * * * /path/to/run_thunder_cron.sh
#
# Redirects output to the rotating log so cron email is suppressed.
# Exits non-zero if the monitor found errors (useful for cron alerts).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env if present (development / non-systemd environments)
if [[ -f "${SCRIPT_DIR}/.env" ]]; then
    set -o allexport
    # shellcheck source=/dev/null
    source "${SCRIPT_DIR}/.env"
    set +o allexport
fi

# Resolve Python: prefer venv, then system python3
if [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
    PYTHON="${SCRIPT_DIR}/.venv/bin/python"
elif [[ -x "${SCRIPT_DIR}/venv/bin/python" ]]; then
    PYTHON="${SCRIPT_DIR}/venv/bin/python"
else
    PYTHON="$(command -v python3)"
fi

LOG_DIR="${PERSISTENT_DATA_DIR:-${SCRIPT_DIR}/data}/logs"
mkdir -p "${LOG_DIR}"

# Run and append stdout+stderr to log (RotatingFileHandler handles rotation
# for the Python log; this captures anything that leaks before logging init).
exec "${PYTHON}" "${SCRIPT_DIR}/thunder_cron.py" "$@" \
    >> "${LOG_DIR}/thunder_cron_shell.log" 2>&1
