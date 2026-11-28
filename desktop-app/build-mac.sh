#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LIVE_WALLPAPER="${REPO_ROOT}/live-wallpaper.html"
APP_PY="${SCRIPT_DIR}/app.py"
DIST_DIR="${SCRIPT_DIR}/dist"
BUILD_DIR="${SCRIPT_DIR}/build"
ICON_PNG="${BUILD_DIR}/ide-lyrics-1024.png"
ICONSET_DIR="${BUILD_DIR}/ide-lyrics.iconset"
ICON_ICNS="${BUILD_DIR}/ide-lyrics.icns"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "=== Building IDE-Lyrics-Studio.app (macOS Apple Silicon arm64) ==="
mkdir -p "${BUILD_DIR}" "${DIST_DIR}"

if [[ ! -f "${LIVE_WALLPAPER}" ]]; then
  echo "[ERROR] Missing ${LIVE_WALLPAPER}" >&2
  exit 1
fi

echo "[1/5] Generating Retina macOS .icns app icon..."
"${PYTHON_BIN}" - <<PY
import sys
sys.path.insert(0, "${SCRIPT_DIR}")
import app
img = app.create_tray_icon_image(1024)
img.save("${ICON_PNG}", format="PNG")
PY

ICON_FLAG=()
if command -v iconutil >/dev/null 2>&1 && command -v sips >/dev/null 2>&1; then
  rm -rf "${ICONSET_DIR}"
  mkdir -p "${ICONSET_DIR}"
  for sz in 16 32 128 256 512; do
    sz2=$((sz * 2))
    sips -z "${sz}" "${sz}" "${ICON_PNG}" --out "${ICONSET_DIR}/icon_${sz}x${sz}.png" >/dev/null
    sips -z "${sz2}" "${sz2}" "${ICON_PNG}" --out "${ICONSET_DIR}/icon_${sz}x${sz}@2x.png" >/dev/null
  done
  iconutil -c icns "${ICONSET_DIR}" -o "${ICON_ICNS}"
  ICON_FLAG=(--icon "${ICON_ICNS}")
fi

if ! "${PYTHON_BIN}" -m PyInstaller --version >/dev/null 2>&1; then
  echo "[..] Installing PyInstaller..."
  "${PYTHON_BIN}" -m pip install pyinstaller
fi

echo "[2/5] Running PyInstaller (arm64)..."
"${PYTHON_BIN}" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "IDE-Lyrics-Studio" \
  --osx-bundle-identifier "com.idelyrics.studio" \
  "${ICON_FLAG[@]}" \
  --distpath "${DIST_DIR}" \
  --workpath "${BUILD_DIR}" \
  --specpath "${BUILD_DIR}" \
  --paths "${SCRIPT_DIR}" \
  --add-data "${LIVE_WALLPAPER}:." \
  --hidden-import "smtc_bridge" \
  --hidden-import "wallpaper_host" \
  --hidden-import "integrations" \
  --hidden-import "pystray._darwin" \
  --collect-submodules "webview" \
  --collect-data "webview" \
  "${APP_PY}"

APP_BUNDLE="${DIST_DIR}/IDE-Lyrics-Studio.app"
PLIST="${APP_BUNDLE}/Contents/Info.plist"

echo "[3/5] Setting Info.plist metadata (LSUIElement, version, Retina)..."
APP_VERSION="$("${PYTHON_BIN}" -c "import sys; sys.path.insert(0, '${SCRIPT_DIR}'); import app; print(app.APP_VERSION)")"
if [[ -f "${PLIST}" ]] && command -v /usr/libexec/PlistBuddy >/dev/null 2>&1; then
  /usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "${PLIST}" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Set :LSUIElement true" "${PLIST}"
  /usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "${PLIST}" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Set :NSHighResolutionCapable true" "${PLIST}"
  /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string ${APP_VERSION}" "${PLIST}" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString ${APP_VERSION}" "${PLIST}"
  /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string ${APP_VERSION}" "${PLIST}" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Set :CFBundleVersion ${APP_VERSION}" "${PLIST}"
fi

echo "[4/5] Re-signing ad-hoc bundle signature after Info.plist edits..."
if command -v codesign >/dev/null 2>&1; then
  codesign --force --deep --sign - "${APP_BUNDLE}"
  codesign --verify --deep --strict --verbose=2 "${APP_BUNDLE}"
fi

echo "[5/5] Packaging macOS .dmg and .zip..."
DMG_OUT="${DIST_DIR}/IDE-Lyrics-Studio-macOS-arm64.dmg"
ZIP_OUT="${DIST_DIR}/IDE-Lyrics-Studio-macOS.zip"
DMG_STAGE="${BUILD_DIR}/dmg-stage"

rm -rf "${DMG_STAGE}" "${DMG_OUT}" "${ZIP_OUT}"
if command -v hdiutil >/dev/null 2>&1; then
  mkdir -p "${DMG_STAGE}"
  cp -R "${APP_BUNDLE}" "${DMG_STAGE}/"
  ln -s /Applications "${DMG_STAGE}/Applications"
  hdiutil create -volname "IDE Lyrics Studio" -srcfolder "${DMG_STAGE}" -ov -format UDZO "${DMG_OUT}"
fi

(cd "${DIST_DIR}" && ditto -c -k --sequesterRsrc --keepParent "IDE-Lyrics-Studio.app" "${ZIP_OUT}")

echo "=== Build Complete ==="
echo "  App Bundle : ${APP_BUNDLE}"
echo "  DMG Image  : ${DMG_OUT}"
echo "  Zip Archive: ${ZIP_OUT}"
