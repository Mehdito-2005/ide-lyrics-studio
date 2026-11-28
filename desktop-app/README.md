# IDE Lyrics Studio

Standalone desktop app, live wallpaper host, and always-on-top mini lyrics overlay for Windows, macOS, and Linux. Reads the active track from your OS media session and pulls synced lyrics from LRCLIB.

## Windows

### Run or build the `.exe`
Run `desktop-app\dist\IDE-Lyrics-Studio.exe`, or build a fresh single-file executable:

```powershell
powershell -ExecutionPolicy Bypass -File .\desktop-app\build-exe.ps1
```

`build-exe.ps1` generates `ide-lyrics.ico`, embeds Windows Explorer and Task Manager metadata from `version_info.txt`, and writes PyInstaller scratch files to `%TEMP%` so OneDrive does not lock the build.

### Build the Windows installer
Requires [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install JRSoftware.InnoSetup`):

```powershell
powershell -ExecutionPolicy Bypass -File .\desktop-app\build-exe.ps1 -Installer
# or compile directly after building the .exe:
iscc .\desktop-app\installer.iss
```

Produces `desktop-app\dist\IDE-Lyrics-Studio-Setup-1.1.0.exe`. Installs per-user to `%LOCALAPPDATA%\IDE Lyrics Studio` (no UAC prompt), creates a Start Menu shortcut, and offers optional Desktop shortcut and Windows startup checkboxes.

### Run from source
Double-click `IDE-Lyrics-Tray.vbs` (silent tray launch) or `Start-IDE-Lyrics.bat` (installs dependencies via `setup.ps1` on first run), or use PowerShell:

```powershell
.\desktop-app\launch.ps1
.\desktop-app\launch.ps1 -Mode wallpaper
.\desktop-app\launch.ps1 -Mode app -Show
.\desktop-app\launch.ps1 -Choose
```

Media sync uses Windows SMTC natively and picks up Spotify, Apple Music, Cider, Chrome, Edge, and other desktop players with no extra setup.

## macOS

### Run from source
```bash
bash desktop-app/setup-mac.sh
bash desktop-app/launch-mac.sh
```

Spotify and Apple Music work out of the box through AppleScript. To sync with browsers and all other macOS players (YouTube, Chrome, Arc, Cider, Tidal), install `nowplaying-cli`:

```bash
brew install nowplaying-cli
```

### Build `.app` bundle
```bash
bash desktop-app/build-mac.sh
```

Outputs `desktop-app/dist/IDE-Lyrics-Studio.app` and `desktop-app/dist/IDE-Lyrics-Studio-macOS.zip`. The bundle sets `LSUIElement=true` so it stays in the Menu Bar without a Dock icon, and Wallpaper mode renders at `kCGDesktopWindowLevel` behind desktop icons.

## Linux

### Run from source
```bash
bash desktop-app/setup-linux.sh
bash desktop-app/launch-linux.sh
```

`setup-linux.sh` installs Python packages and prints the distro command if GTK3, PyGObject, WebKitGTK 4.1, Ayatana AppIndicator, or `playerctl` is missing. Media sync uses MPRIS2 over D-Bus via `playerctl` (falling back to `gdbus`) and works with Spotify, Cider, Chrome, Firefox, VLC, and similar players.

### Build standalone binary
```bash
bash desktop-app/build-linux.sh
```

Outputs `desktop-app/dist/IDE-Lyrics-Studio`, a `.desktop` file template, and `desktop-app/dist/IDE-Lyrics-Studio-Linux.tar.gz`.

**X11 vs. Wayland:** X11 supports full desktop-layer wallpaper attachment (`_NET_WM_WINDOW_TYPE_DESKTOP`). Wayland compositors restrict desktop-layer placement, so wallpaper mode falls back to an undecorated `keep_below` window hidden from the taskbar.

## Modes & Tray Controls

Switch modes from the system tray / menu bar icon or inside **Settings (`S`)**:

- **App Window:** Normal resizable window. Drag the Clock, Track Card, or Lyrics to position them; coordinates scale proportionally when you switch to Wallpaper mode. Closing the window hides it back to the tray.
- **Live Wallpaper:** Pins the visualizer behind your desktop icons. Pick Primary Monitor, a specific monitor, or Span All Monitors from the tray menu.
- **Mini Overlay:** Compact always-on-top window with saved position and size.
- **Tray menu:** Live track label, Play/Pause/Next/Previous, mode and monitor switchers, Clean Feed (`E`), Reload UI, Start at Login, Open Logs, Check for Updates, and Quit.

Packaged builds store `config.json`, `desktop-app.log`, and WebView state outside the install folder:

- Windows: `%APPDATA%\IDELyricsStudio`
- macOS: `~/Library/Application Support/IDELyricsStudio`
- Linux: `~/.config/ide-lyrics-studio`

## GitHub Actions Releases

[.github/workflows/build-desktop-app.yml](../.github/workflows/build-desktop-app.yml) builds native artifacts for Windows (`.exe`), macOS (`.app.zip`), and Linux (`.tar.gz`). Trigger it manually from the Actions tab (`workflow_dispatch`) or publish a release by pushing a version tag:

```bash
git tag v1.1.0
git push origin v1.1.0
```

## Tests

```powershell
powershell -ExecutionPolicy Bypass -File .\desktop-app\test-desktop-app.ps1
```
