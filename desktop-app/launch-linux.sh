#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_PY="${SCRIPT_DIR}/app.py"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "[ERROR] python3 not found. Run ./setup-linux.sh first." >&2
  exit 1
fi

if ! "${PYTHON_BIN}" -c "import webview" >/dev/null 2>&1; then
  echo "[IDE Lyrics] Dependencies missing; running setup-linux.sh..."
  bash "${SCRIPT_DIR}/setup-linux.sh"
fi

FOREGROUND=0
EXTRA_ARGS=()
for arg in "$@"; do
  if [[ "${arg}" == "--console" || "${arg}" == "--foreground" ]]; then
    FOREGROUND=1
  else
    EXTRA_ARGS+=("${arg}")
  fi
done

export WEBKIT_DISABLE_DMABUF_RENDERER="${WEBKIT_DISABLE_DMABUF_RENDERER:-1}"
export GDK_BACKEND="${GDK_BACKEND:-x11,wayland}"

if [[ "${FOREGROUND}" -eq 1 ]]; then
  exec "${PYTHON_BIN}" "${APP_PY}" "${EXTRA_ARGS[@]}"
fi

nohup "${PYTHON_BIN}" "${APP_PY}" "${EXTRA_ARGS[@]}" >/dev/null 2>&1 &
disown || true
echo "[IDE Lyrics] Running silently in your Linux System Tray."
