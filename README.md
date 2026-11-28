# IDE Lyrics Studio

![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20Apple%20Silicon%20%7C%20Linux-blue)
![Release](https://img.shields.io/github/v/release/Mehdito-2005/ide-lyrics-studio)
![License](https://img.shields.io/badge/license-MIT-green)

A standalone 3D IDE-style synced lyrics app, live desktop wallpaper, and **all-in-one setup hub** for **Windows, macOS (Apple Silicon), and Linux**. Works with any music player — Spotify, Apple Music, Cider, YouTube Music, Chrome, Firefox, VLC, Tidal, and more — and lives quietly in your System Tray / Menu Bar with zero taskbar or Dock clutter.

## Features

- **Mode-Scoped, Decluttered Settings Drawer**: Switch settings views between **App**, **Wallpaper**, **Mini Overlay**, and **All** with collapsible sections and dedicated Mini Overlay controls (Position, Layer, Song Card, Hover Bar / Lyrics Only, Click-Through Ghost Mode, Overlay Opacity) and Wallpaper Power Auto-Yield controls.
- **Interactive Setup & Integrations Wizard**: On first launch (or anytime from the System Tray / Settings), pick your launch mode (**Live Wallpaper**, **App Window**, or **Mini Overlay**), toggle **Launch at Login**, and optionally install the [Cider Plugin](https://github.com/Mehdito-2005/cider-ide-lyrics) or install & apply the [Spicetify Extension](https://github.com/Mehdito-2005/spicetify-ide-lyrics) in one click.
- **Three Display Modes + Transparent Floating Miniplayer & Ghost Mode**: Switch live between **App Window**, **Live Wallpaper** (rendered behind desktop icons), and a **Transparent Mini Overlay** that floats clean lyrics directly over your wallpaper — with an optional removable compact **Song Card** right under the lyrics, a **Lyrics Only (`Pure`)** mode to hide controls completely, a hover-only Miniplayer dock (drag to move, playback controls, **Left / Bottom / Right** dock snap, **Pin on Top** vs. **Over Wallpaper** layer switch), **Click-Through Ghost Mode**, native **Overlay Opacity** presets, and **Smart Power Auto-Yield**.
- **Immersive Album Mode, Cider-Style Album Backdrop & Multi-Mode Audio Visualizer**: Switch to **Immersive Album Mode** (`Look` tab or press `I`) for a large centered album cover above song info and centered lyrics; set Backdrop to **Album** (`artwork`) for a Cider-style feathered full-screen hero cover melting into a luminous color bleed with Brightness/Blur sliders and high-res artwork upgrade; and switch `#wave-canvas` between **Waves**, **Spectrum** (32-band equalizer), and radial **Halo** visualizers.
- **IDE Code Styles & Clean Lyrics Mode**: Switch between **IDE Code** and minimal **Clean Lyrics** (`Mono` / `Sans` / `Serif` / `Rounded`), or format lines using real programming constructs (`Full IDE`, `OOP` with `class`/`extends`/`constructor`/`self`/`this`/`super`/`@property`, `Loops & Fn` with `for`/`while`/`do...while`/`def`/`function`/`async`/`await`, `Python`, `JS / TS`, `Dict`, `Chain .`, or `Classic`) with syntax-colored keyword, method, variable, and type highlighting.
- **Universal Media Sync + Song vs. Video Detection**: Automatically distinguishes songs (including music videos, `- Topic`, and `Artist - Song` YouTube tracks) from normal videos (vlogs, tutorials, livestreams, podcasts, VLC clips, Netflix, Twitch), showing a `Song` / `Video` badge with one-click manual override, configurable video behavior (`Hide Lyrics` / `Show Info` / `Try Lyrics`), duration-matched LRCLIB lyrics, and per-player sync calibration (`[-3000ms, +3000ms]`, auto-offsetting Apple Music SMTC lag).
- **Highlight & Live Typewriter Modes**: Choose classic word-by-word highlighting with depth-of-field blur, or **Live Typewriter** mode that types out lyrics character-by-character with a gliding IDE caret.
- **Modular Element Controls & Silence Wait Threshold**: Show or hide any HUD element individually (`Clock HUD`, `Clock Date`, `Track Card`, `Album Art`, `Track Progress`, `Audio Waves`, `Bottom Hairline`, `Line Numbers`, `Hover Time`, `Vignette`, `Silence Progress Bar`) and configure how many seconds of instrumental silence trigger the countdown bar (`1.0s–15.0s`).
- **1-Click Vibe Presets & JSON Preset Export / Import**: Apply curated looks (`Cyberpunk Neon`, `Minimal Studio`, `Lo-Fi Typewriter`, `Cinematic Clean`) in one click from the `Look` tab, or back up and share your full settings as JSON in the `Studio` tab.
- **Interactive Layout & Hover Line Timestamps**: Drag the Clock, Track Card, and 3D Lyrics stage anywhere in App Mode (saved proportionally `0.0–1.0` for fullscreen Wallpaper mode), and hover any lyric line to reveal its `m:ss` timestamp pill.
- **Custom Backgrounds & Themes**: Automatically extracts syntax colors from the current song's album art, or switch to built-in themes (`Matrix`, `Cyber`, `Monokai`), solid/chroma backdrops, or drag-and-drop custom images and looping videos.
- **Clean Idle State & Multi-Language Support**: Hides the lyrics stage when idle, handles LTR/RTL scripts automatically (Persian, Arabic, Hebrew), and supports multi-monitor Wallpaper Display switching, system media controls, login autostart, and GitHub Release update checks.

## Download & Install

Grab the latest prebuilt release from the [Releases](https://github.com/Mehdito-2005/ide-lyrics-studio/releases) page:

| OS | Recommended Installer | Portable / Alternative | Notes |
| --- | --- | --- | --- |
| **Windows 10/11** | `IDE-Lyrics-Studio-Setup-1.8.4.1.exe` | `IDE-Lyrics-Studio.exe` | No admin or Python needed. On first launch, Windows SmartScreen may show **More info → Run anyway** since binaries are unsigned. |
| **macOS (Apple Silicon)** | `IDE-Lyrics-Studio-macOS-arm64.dmg` | `IDE-Lyrics-Studio-macOS.zip` | Drag `IDE-Lyrics-Studio.app` to Applications. Run `xattr -cr /Applications/IDE-Lyrics-Studio.app` once to clear Gatekeeper quarantine. For system-wide sync beyond Spotify/Apple Music on macOS 15.4+, install `brew tap ungive/media-control && brew install media-control`. |
| **Linux (Ubuntu / Debian / Mint / Pop!_OS)** | `ide-lyrics-studio_1.8.4.1_all.deb` | `IDE-Lyrics-Studio-Linux.tar.gz` | Install with `sudo apt install ./ide-lyrics-studio_1.8.4.1_all.deb` (pulls GTK3/WebKitGTK automatically, zero glibc lock-in). For Fedora/Arch, extract the `.tar.gz` and run `./install.sh` (requires `python3-gobject`, `webkit2gtk`, `pillow`, and `playerctl`). Works cleanly on X11 & Wayland/NVIDIA (`WEBKIT_DISABLE_DMABUF_RENDERER=1` preconfigured). A Flatpak manifest is also included in `desktop-app/flatpak/`. |

## How It Works Under the Hood

| Layer | Windows | macOS (Apple Silicon) | Linux |
| --- | --- | --- | --- |
| **Media Sync** | WinRT SMTC (`GlobalSystemMediaTransportControls`) | `media-control` (macOS 15.4+) → `nowplaying-cli` → AppleScript | MPRIS2 over D-Bus (`playerctl` / `gdbus`) |
| **Live Wallpaper** | Win32 `WorkerW` behind `SHELLDLL_DefView` | Cocoa `NSWindow` at `CGWindowLevelForKey(kCGDesktopIconWindowLevelKey) - 1` | GTK3/GDK `DESKTOP` & `keep_below` hints |
| **Web Engine** | Edge WebView2 (`pywebview`) | Native WKWebView (`cocoa`) | Native WebKitGTK (`gtk`) |
| **Single Instance** | Win32 named mutex (`CreateMutexW`) | `fcntl.flock` lockfile | `fcntl.flock` lockfile |
| **Login Autostart** | `HKCU\...\Run` registry entry (`--tray`) | `~/Library/LaunchAgents/*.plist` (`--tray`) | `~/.config/autostart/*.desktop` (`--tray`) |

## System Tray & Shortcuts

Right-click the tray / menu bar icon to switch modes, reopen the **Setup Wizard & Integrations**, control playback, pick your wallpaper display, toggle auto-start, reload the UI, or open the log folder. Closing the App Window (`X`) hides it back to the tray; use **Quit** to exit cleanly.

| Shortcut | Action |
| --- | --- |
| `S` | Open / close Settings drawer (`Look`, `Lyrics`, `Layout`, `Studio`) |
| `I` | Toggle Immersive Album Mode (large cover on top, song & lyrics centered) |
| `E` | Toggle Clean Feed (hide UI chrome for recording) |
| `C` | Toggle Clock HUD |
| `T` | Cycle Color Theme |
| `R` | Reset element positions |
| `[` / `]` / `\` | Nudge lyric sync offset (`-50ms` / `+50ms` / reset) |

## Run & Build From Source

Requires Python 3.10+. See [desktop-app/README.md](desktop-app/README.md) for full details.

```powershell
# Windows: launch in System Tray (opens Setup Wizard on very first run)
.\desktop-app\launch.ps1

# Windows: build portable .exe and Inno Setup installer
powershell -ExecutionPolicy Bypass -File .\desktop-app\build-exe.ps1 -Installer
```

```bash
# macOS (Apple Silicon)
bash desktop-app/setup-mac.sh
bash desktop-app/launch-mac.sh
bash desktop-app/build-mac.sh

# Linux (.deb + portable tarball)
bash desktop-app/setup-linux.sh
bash desktop-app/launch-linux.sh
bash desktop-app/build-linux.sh
```

## Project Layout

```text
├── live-wallpaper.html                 # 3D IDE lyrics renderer, Typewriter engine, Settings & Setup Wizard UI
├── desktop-app/
│   ├── app.py                          # Tray shell, local server, single-instance lock, autostart & wizard API
│   ├── integrations.py                 # Cider & Spicetify detection & 1-click installer engine
│   ├── smtc_bridge.py                  # Cross-platform media sync & playback commands (SMTC / macOS / MPRIS2)
│   ├── wallpaper_host.py               # Cross-platform wallpaper & multi-monitor window host
│   ├── build-exe.ps1 / build-mac.sh / build-linux.sh
│   ├── flatpak/com.idelyrics.studio.yml # Linux Flatpak manifest
│   ├── homebrew/ide-lyrics-studio.rb   # macOS Apple Silicon Homebrew Cask template
│   ├── installer.iss                   # Windows Inno Setup 6 per-user installer script
│   └── test-desktop-app.ps1            # Automated verification suite
└── .github/workflows/build-desktop-app.yml
```

## License

MIT — see [LICENSE](LICENSE).
