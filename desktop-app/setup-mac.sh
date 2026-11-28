#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ_FILE="${SCRIPT_DIR}/requirements.txt"

echo "=== IDE Lyrics Studio - macOS Setup ==="

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "[ERROR] python3 not found. Install Python 3.10+ (e.g. 'brew install python')." >&2
  exit 1
fi

echo "[OK] Found $("${PYTHON_BIN}" --version)"
echo "[..] Installing Python dependencies from ${REQ_FILE}..."
"${PYTHON_BIN}" -m pip install --upgrade pip >/dev/null 2>&1 || true
"${PYTHON_BIN}" -m pip install -r "${REQ_FILE}"

if ! command -v media-control >/dev/null 2>&1 && ! command -v nowplaying-cli >/dev/null 2>&1; then
  echo ""
  echo "[TIP] Spotify & Apple Music work out of the box via AppleScript."
  echo "      For universal macOS media sync (YouTube, Chrome, Arc, Cider, Tidal, etc.),"
  echo "      install media-control (macOS 15.4+) with Homebrew:"
  echo "        brew tap ungive/media-control && brew install media-control"
fi

echo ""
echo "[..] Verifying Python imports..."
"${PYTHON_BIN}" - <<'PY'
for mod in ("webview", "pystray", "PIL", "AppKit"):
    try:
        __import__(mod)
        print(f"  [OK] {mod}")
    except Exception as exc:
        print(f"  [WARN] {mod}: {exc}")
PY

chmod +x "${SCRIPT_DIR}/launch-mac.sh" "${SCRIPT_DIR}/build-mac.sh" 2>/dev/null || true
echo "=== macOS Setup Complete! Run ./launch-mac.sh to start in your Menu Bar. ==="
