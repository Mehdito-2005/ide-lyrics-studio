#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LIVE_WALLPAPER="${REPO_ROOT}/live-wallpaper.html"
DIST_DIR="${SCRIPT_DIR}/dist"
BUILD_DIR="${SCRIPT_DIR}/build"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "=== Building IDE Lyrics Studio (Linux native .deb & portable tarball) ==="
mkdir -p "${BUILD_DIR}" "${DIST_DIR}"

if [[ ! -f "${LIVE_WALLPAPER}" ]]; then
  echo "[ERROR] Missing ${LIVE_WALLPAPER}" >&2
  exit 1
fi

APP_VERSION="$("${PYTHON_BIN}" -c "import sys; sys.path.insert(0, '${SCRIPT_DIR}'); import app; print(app.APP_VERSION)")"
ICON_PNG="${DIST_DIR}/ide-lyrics-studio.png"

echo "[1/4] Generating 256x256 PNG app icon..."
"${PYTHON_BIN}" - <<PY
import sys
sys.path.insert(0, "${SCRIPT_DIR}")
import app
img = app.create_tray_icon_image(256)
img.save("${ICON_PNG}", format="PNG")
PY

echo "[2/4] Staging application payload & pure-Python wheels..."
PAYLOAD_DIR="${BUILD_DIR}/payload"
VENDOR_DIR="${PAYLOAD_DIR}/vendor"
rm -rf "${PAYLOAD_DIR}"
mkdir -p "${PAYLOAD_DIR}/desktop-app" "${VENDOR_DIR}"

cp "${LIVE_WALLPAPER}" "${PAYLOAD_DIR}/live-wallpaper.html"
if [[ -f "${REPO_ROOT}/LICENSE" ]]; then
  cp "${REPO_ROOT}/LICENSE" "${PAYLOAD_DIR}/LICENSE"
fi
for pyfile in app.py smtc_bridge.py wallpaper_host.py integrations.py; do
  cp "${SCRIPT_DIR}/${pyfile}" "${PAYLOAD_DIR}/desktop-app/${pyfile}"
done

# Vendor ONLY pure-Python dependencies so the package has zero glibc lock-in
# and uses the system distro's GTK3, WebKitGTK, AppIndicator, and Pillow.
"${PYTHON_BIN}" -m pip install \
  --no-deps \
  --upgrade \
  --target "${VENDOR_DIR}" \
  pywebview pystray proxy-tools bottle typing-extensions six python-xlib

# Remove any compiled caches or unneeded dist-info cruft to keep package clean & small
find "${VENDOR_DIR}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

echo "[3/4] Building Debian package (ide-lyrics-studio_${APP_VERSION}_all.deb)..."
DEB_ROOT="${BUILD_DIR}/deb-root"
DEB_OUT="${DIST_DIR}/ide-lyrics-studio_${APP_VERSION}_all.deb"
rm -rf "${DEB_ROOT}" "${DEB_OUT}"

mkdir -p \
  "${DEB_ROOT}/DEBIAN" \
  "${DEB_ROOT}/usr/lib/ide-lyrics-studio" \
  "${DEB_ROOT}/usr/bin" \
  "${DEB_ROOT}/usr/share/applications" \
  "${DEB_ROOT}/usr/share/icons/hicolor/256x256/apps"

cp -a "${PAYLOAD_DIR}/." "${DEB_ROOT}/usr/lib/ide-lyrics-studio/"
cp "${ICON_PNG}" "${DEB_ROOT}/usr/share/icons/hicolor/256x256/apps/ide-lyrics-studio.png"

cat > "${DEB_ROOT}/usr/bin/ide-lyrics-studio" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export IDE_LYRICS_SYSTEM_INSTALL=1
export IDE_LYRICS_LAUNCHER="/usr/bin/ide-lyrics-studio"
export WEBKIT_DISABLE_DMABUF_RENDERER="${WEBKIT_DISABLE_DMABUF_RENDERER:-1}"
export GDK_BACKEND="${GDK_BACKEND:-x11,wayland}"
export PYTHONPATH="/usr/lib/ide-lyrics-studio/vendor${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 "/usr/lib/ide-lyrics-studio/desktop-app/app.py" "$@"
EOF
chmod 0755 "${DEB_ROOT}/usr/bin/ide-lyrics-studio"

cat > "${DEB_ROOT}/usr/share/applications/ide-lyrics-studio.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Version=1.0
Name=IDE Lyrics Studio
Comment=Universal IDE Lyrics Live Wallpaper & Tray Studio
Exec=/usr/bin/ide-lyrics-studio --show
Icon=ide-lyrics-studio
Terminal=false
Categories=AudioVideo;Audio;
StartupNotify=false
EOF

cat > "${DEB_ROOT}/DEBIAN/control" <<EOF
Package: ide-lyrics-studio
Version: ${APP_VERSION}
Section: sound
Priority: optional
Architecture: all
Maintainer: Mehdito-2005 <https://github.com/Mehdito-2005/ide-lyrics-studio>
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, python3-pil, gir1.2-gtk-3.0, gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0, gir1.2-ayatanaappindicator3-0.1 | gir1.2-appindicator3-0.1
Recommends: playerctl
Description: Live IDE Lyrics Desktop App & Wallpaper
 Universal real-time IDE code lyrics visualizer that runs as a live desktop
 wallpaper, resizable app window, or always-on-top mini overlay with system
 media sync (MPRIS2) and an interactive Cider / Spicetify setup wizard.
EOF

if command -v dpkg-deb >/dev/null 2>&1; then
  dpkg-deb --build --root-owner-group "${DEB_ROOT}" "${DEB_OUT}"
fi

echo "[4/4] Building portable Linux tarball (IDE-Lyrics-Studio-Linux.tar.gz)..."
TAR_STAGE="${BUILD_DIR}/tar-stage/IDE-Lyrics-Studio"
TAR_OUT="${DIST_DIR}/IDE-Lyrics-Studio-Linux.tar.gz"
rm -rf "${BUILD_DIR}/tar-stage" "${TAR_OUT}"
mkdir -p "${TAR_STAGE}"

cp -a "${PAYLOAD_DIR}/." "${TAR_STAGE}/"
cp "${ICON_PNG}" "${TAR_STAGE}/ide-lyrics-studio.png"

cat > "${TAR_STAGE}/ide-lyrics-studio" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
export IDE_LYRICS_SYSTEM_INSTALL=1
export IDE_LYRICS_LAUNCHER="${ROOT}/ide-lyrics-studio"
export WEBKIT_DISABLE_DMABUF_RENDERER="${WEBKIT_DISABLE_DMABUF_RENDERER:-1}"
export GDK_BACKEND="${GDK_BACKEND:-x11,wayland}"
export PYTHONPATH="${ROOT}/vendor${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 "${ROOT}/desktop-app/app.py" "$@"
EOF
chmod 0755 "${TAR_STAGE}/ide-lyrics-studio"

cat > "${TAR_STAGE}/install.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${HOME}/.local/share/ide-lyrics-studio"
BIN_DIR="${HOME}/.local/bin"
APPS_DIR="${HOME}/.local/share/applications"
ICON_DIR="${HOME}/.local/share/icons/hicolor/256x256/apps"

mkdir -p "${INSTALL_DIR}" "${BIN_DIR}" "${APPS_DIR}" "${ICON_DIR}"
rm -rf "${INSTALL_DIR:?}/"*
cp -a "${SRC_DIR}/." "${INSTALL_DIR}/"
chmod 0755 "${INSTALL_DIR}/ide-lyrics-studio"
ln -sf "${INSTALL_DIR}/ide-lyrics-studio" "${BIN_DIR}/ide-lyrics-studio"
cp "${INSTALL_DIR}/ide-lyrics-studio.png" "${ICON_DIR}/ide-lyrics-studio.png"

cat > "${APPS_DIR}/ide-lyrics-studio.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=IDE Lyrics Studio
Comment=Universal IDE Lyrics Live Wallpaper & Tray Studio
Exec=${BIN_DIR}/ide-lyrics-studio --show
Icon=${ICON_DIR}/ide-lyrics-studio.png
Terminal=false
Categories=AudioVideo;Audio;
StartupNotify=false
DESKTOP

echo "Installed IDE Lyrics Studio to ${INSTALL_DIR}"
echo "Launch from your app menu or run: ${BIN_DIR}/ide-lyrics-studio"
EOF
chmod 0755 "${TAR_STAGE}/install.sh"

tar -czf "${TAR_OUT}" -C "${BUILD_DIR}/tar-stage" "IDE-Lyrics-Studio"

echo "=== Build Complete ==="
echo "  Debian Pkg : ${DEB_OUT}"
echo "  Tarball    : ${TAR_OUT}"
