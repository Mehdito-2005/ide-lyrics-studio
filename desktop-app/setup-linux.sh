#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ_FILE="${SCRIPT_DIR}/requirements.txt"

echo "=== IDE Lyrics Studio — Linux Setup ==="

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "[ERROR] python3 not found. Install Python 3.10+ using your package manager." >&2
  exit 1
fi

echo "[OK] Found $("${PYTHON_BIN}" --version)"

# Check GTK3 / WebKitGTK / AppIndicator & playerctl prerequisites
MISSING_HINTS=()
if ! "${PYTHON_BIN}" -c "import gi; gi.require_version('Gtk', '3.0')" >/dev/null 2>&1; then
  MISSING_HINTS+=("GTK3 + PyGObject + WebKitGTK + Ayatana AppIndicator")
fi
if ! command -v playerctl >/dev/null 2>&1 && ! command -v gdbus >/dev/null 2>&1; then
  MISSING_HINTS+=("playerctl (MPRIS2 media sync)")
fi

if [[ "${#MISSING_HINTS[@]}" -gt 0 ]]; then
  echo "[WARN] Missing recommended Linux desktop packages: ${MISSING_HINTS[*]}"
  echo "  Ubuntu / Debian:"
  echo "    sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1 gir1.2-ayatanaappindicator3-0.1 playerctl x11-xserver-utils"
  echo "  Fedora:"
  echo "    sudo dnf install -y python3-gobject gtk3 webkit2gtk4.1 libayatana-appindicator-gtk3 playerctl xrandr"
  echo "  Arch Linux:"
  echo "    sudo pacman -S --needed python-gobject gtk3 webkit2gtk-4.1 libayatana-appindicator playerctl xorg-xrandr"
  echo ""
fi

echo "[..] Installing Python dependencies from ${REQ_FILE}..."
"${PYTHON_BIN}" -m pip install --user -r "${REQ_FILE}" || "${PYTHON_BIN}" -m pip install --break-system-packages --user -r "${REQ_FILE}"

echo "[..] Verifying Python imports..."
"${PYTHON_BIN}" - <<'PY'
for mod in ("webview", "pystray", "PIL"):
    try:
        __import__(mod)
        print(f"  [OK] {mod}")
    except Exception as exc:
        print(f"  [WARN] {mod}: {exc}")
PY

chmod +x "${SCRIPT_DIR}/launch-linux.sh" "${SCRIPT_DIR}/build-linux.sh" 2>/dev/null || true
echo "=== Linux Setup Complete! Run ./launch-linux.sh to start in your System Tray. ==="
