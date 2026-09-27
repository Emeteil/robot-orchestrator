#!/usr/bin/env bash
set -euo pipefail

SELF_VENV_POINTER="/var/lib/robot-orchestrator/self/current-venv"
FALLBACK_VENV_BIN="/opt/robot-orchestrator/.venv/bin"

if [ -e "${SELF_VENV_POINTER}" ]; then
    VENV_BIN="$(readlink -f "${SELF_VENV_POINTER}")/bin"
else
    VENV_BIN="${FALLBACK_VENV_BIN}"
fi

exec "${VENV_BIN}/robot-orchestrator" run
