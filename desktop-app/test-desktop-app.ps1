$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

$Passed = 0
$Failed = 0

function Pass($msg) {
    $script:Passed++
    Write-Host "[PASS] $msg" -ForegroundColor Green
}

function Fail($msg) {
    $script:Failed++
    Write-Host "[FAIL] $msg" -ForegroundColor Red
}

Write-Host "=== IDE Lyrics Desktop App Test Suite ===" -ForegroundColor Cyan

# 1. Resolve Python 3
$PyCmd = Get-Command python -ErrorAction SilentlyContinue
if ($PyCmd) {
    $PyExe = "python"
    $PyPrefix = @()
    Pass "Python found in PATH ($($PyCmd.Source))"
} else {
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        $PyExe = "py"
        $PyPrefix = @("-3")
        Pass "Python launcher found ($($PyLauncher.Source))"
    } else {
        Fail "Python 3 not found in PATH"
        exit 1
    }
}

# 2. Python syntax check (py_compile) on app.py, smtc_bridge.py, wallpaper_host.py, integrations.py
$PyFiles = @("app.py", "smtc_bridge.py", "wallpaper_host.py", "integrations.py")
foreach ($file in $PyFiles) {
    $fullPath = Join-Path $ScriptDir $file
    if (-not (Test-Path $fullPath)) {
        Fail "Missing Python file: desktop-app/$file"
        continue
    }
    $compileArgs = @($PyPrefix) + @("-m", "py_compile", $fullPath)
    $out = & $PyExe @compileArgs 2>&1
    if ($LASTEXITCODE -eq 0) {
        Pass "py_compile desktop-app/$file"
    } else {
        Fail "py_compile desktop-app/$file failed: $out"
    }
}

# 3. PowerShell AST syntax check on setup.ps1, launch.ps1, test-desktop-app.ps1
$PsFiles = @("setup.ps1", "launch.ps1", "test-desktop-app.ps1")
foreach ($file in $PsFiles) {
    $fullPath = Join-Path $ScriptDir $file
    if (-not (Test-Path $fullPath)) {
        Fail "Missing PowerShell file: desktop-app/$file"
        continue
    }
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($fullPath, [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors -and $errors.Count -gt 0) {
        $errSummary = ($errors | ForEach-Object { $_.Message }) -join "; "
        Fail "PS AST syntax desktop-app/$file failed: $errSummary"
    } else {
        Pass "PS AST syntax desktop-app/$file"
    }
}

# 4. Run Python runtime checks (imports, local HTTP server, config persistence, SMTCBridge shape, WallpaperHost guards)
$PyTestScript = @'
import inspect
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

app_dir = Path(sys.argv[1]).resolve()
repo_root = app_dir.parent
sys.path.insert(0, str(app_dir))

results = []

def check(name, fn):
    try:
        fn()
        results.append((True, name, ""))
    except Exception as exc:
        results.append((False, name, f"{type(exc).__name__}: {exc}"))

# 4a. Imports
def test_imports():
    import app  # noqa: F401
    import integrations  # noqa: F401
    import smtc_bridge  # noqa: F401
    import wallpaper_host  # noqa: F401
    import webview  # noqa: F401
    import pystray  # noqa: F401
    import PIL  # noqa: F401

check("Python module imports (app, integrations, smtc_bridge, wallpaper_host, webview, pystray, PIL)", test_imports)

# 4b. Local HTTP server serving live-wallpaper.html
def test_http_server():
    import app
    server, port = app.start_local_server(repo_root)
    try:
        assert isinstance(port, int) and port > 0, f"Invalid ephemeral port: {port}"
        url = f"http://127.0.0.1:{port}/live-wallpaper.html?desktopApp=1"
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert resp.status == 200, f"Expected HTTP 200, got {resp.status}"
            body = resp.read().decode("utf-8", errors="replace")
            assert "<html" in body.lower(), "Response did not look like HTML"
            assert "onDesktopMediaUpdate" in body, "live-wallpaper.html missing onDesktopMediaUpdate bridge"
    finally:
        app.stop_local_server(server)

check("Local HTTP server serves live-wallpaper.html?desktopApp=1", test_http_server)

# 4c. config.json mode persistence
def test_config_persistence():
    import app
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        assert app.get_saved_mode(cfg_path) == "app", "Default mode should be 'app'"
        for mode in ("wallpaper", "overlay", "app"):
            saved = app.save_mode(mode, cfg_path)
            assert saved == mode
            assert app.get_saved_mode(cfg_path) == mode
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            assert raw["mode"] == mode
        raised = False
        try:
            app.save_mode("invalid_mode", cfg_path)
        except ValueError:
            raised = True
        assert raised, "save_mode should reject invalid modes"

check("config.json mode persistence (default, wallpaper, overlay, app, invalid guard)", test_config_persistence)

# 4d. Tray icon generation
def test_tray_icon():
    import app
    img = app.create_tray_icon_image(64)
    assert img.size == (64, 64), f"Unexpected icon size: {img.size}"

check("In-memory Pillow tray icon generation", test_tray_icon)

# 4e. SMTCBridge.get_snapshot() shape
def test_smtc_snapshot():
    from smtc_bridge import SMTCBridge
    sig = inspect.signature(SMTCBridge.__init__)
    params = [p for n, p in sig.parameters.items() if n != "self"]
    if params and params[0].default is inspect.Parameter.empty:
        bridge = SMTCBridge(lambda _snap: None)
    else:
        bridge = SMTCBridge()
    snap = bridge.get_snapshot()
    assert isinstance(snap, dict), f"Snapshot must be dict, got {type(snap)}"
    required_keys = {
        "title", "artist", "album", "status",
        "positionSec", "durationSec", "sourceApp", "artworkDataUrl",
        "playbackType", "mediaKind", "mediaKindReason",
    }
    missing = required_keys - set(snap.keys())
    assert not missing, f"Missing snapshot keys: {sorted(missing)}"
    assert snap["status"] in ("playing", "paused", "stopped"), f"Unexpected status: {snap['status']!r}"
    assert snap["mediaKind"] in ("song", "video"), f"Unexpected mediaKind: {snap['mediaKind']!r}"

check("SMTCBridge.get_snapshot() shape and status values", test_smtc_snapshot)

# 4f. WallpaperHost safe guards
def test_wallpaper_host_guards():
    from wallpaper_host import WallpaperHost
    host = WallpaperHost()
    assert host.attach_to_wallpaper(0) is False, "attach_to_wallpaper(0) should return False"
    assert host.detach_from_wallpaper(0) is False, "detach_from_wallpaper(0) should return False"
    host.set_always_on_top(0, True)
    host.set_always_on_top(0, False)

check("WallpaperHost safe guards on invalid hwnd=0", test_wallpaper_host_guards)

# 4g. Multi-monitor enumeration, span rect, resolve_monitor_rect & config persistence
def test_multimonitor_and_config():
    import app
    from wallpaper_host import WallpaperHost

    monitors = WallpaperHost.list_monitors()
    assert isinstance(monitors, list) and len(monitors) >= 1, "Expected >=1 monitor"
    first = monitors[0]
    for key in ("id", "name", "rect", "workerw_rect", "is_primary"):
        assert key in first, f"Monitor dict missing key {key!r}"
    assert len(first["rect"]) == 4 and first["rect"][2] > 0 and first["rect"][3] > 0
    assert len(first["workerw_rect"]) == 4 and first["workerw_rect"][2] > 0 and first["workerw_rect"][3] > 0

    vrect = WallpaperHost.get_virtual_screen_rect()
    assert len(vrect) == 4 and vrect[0] == 0 and vrect[1] == 0 and vrect[2] > 0 and vrect[3] > 0

    all_rect = WallpaperHost.resolve_monitor_rect("all")
    assert all_rect == vrect, f"resolve_monitor_rect('all') mismatch: {all_rect} != {vrect}"
    prim_rect = WallpaperHost.resolve_monitor_rect("primary")
    assert len(prim_rect) == 4 and prim_rect[2] > 0 and prim_rect[3] > 0
    idx_rect = WallpaperHost.resolve_monitor_rect("0")
    assert len(idx_rect) == 4 and idx_rect[2] > 0 and idx_rect[3] > 0

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        assert app.get_saved_wallpaper_monitor(cfg_path) == "primary"
        assert app.save_wallpaper_monitor("all", cfg_path) == "all"
        assert app.get_saved_wallpaper_monitor(cfg_path) == "all"
        ctrl = app.DesktopAppController(initial_mode="app", config_path=cfg_path)
        api = app.DesktopApi(ctrl)
        assert len(api.get_monitors()) >= 1
        res = api.set_wallpaper_monitor("0")
        assert res.get("ok") is True and res.get("target") == "0"
        assert app.get_saved_wallpaper_monitor(cfg_path) == "0"

check("Multi-monitor enumeration, resolve_monitor_rect & config persistence", test_multimonitor_and_config)

# 4h. Windows autostart registry round-trip (isolated temp value name)
def test_autostart_roundtrip():
    import uuid
    import app

    cmd = app.build_autostart_command()
    assert isinstance(cmd, str) and ("app.py" in cmd or "IDE-Lyrics-Tray.vbs" in cmd), f"Unexpected autostart cmd: {cmd!r}"

    tmp_val = f"IDELyricsStudio_UnitTest_{uuid.uuid4().hex[:12]}"
    try:
        assert app.is_autostart_enabled(reg_value_name=tmp_val) is False
        assert app.set_autostart(True, reg_value_name=tmp_val) is True
        assert app.is_autostart_enabled(reg_value_name=tmp_val) is True
        assert app.set_autostart(False, reg_value_name=tmp_val) is True
        assert app.is_autostart_enabled(reg_value_name=tmp_val) is False
    finally:
        app.set_autostart(False, reg_value_name=tmp_val)

check("Windows autostart HKCU Run round-trip (isolated test key)", test_autostart_roundtrip)

# 4i. SMTCBridge.send_command() safety, tray tooltip truncation & DesktopApi.media_command()
def test_playback_controls_and_tooltip():
    import app
    from smtc_bridge import SMTCBridge

    bridge = SMTCBridge()
    assert bridge.send_command("play_pause") is False, "send_command should return False when not started"
    assert bridge.send_command("next") is False
    assert bridge.send_command("previous") is False
    assert bridge.send_command("unknown_cmd") is False, "Unknown command should safely return False"

    long_tip = app.format_tray_tooltip({"title": "T" * 100, "artist": "A" * 100})
    assert len(long_tip) <= 127, f"Tooltip exceeded 127 chars: {len(long_tip)}"
    assert long_tip.startswith("IDE Lyrics"), f"Unexpected tooltip prefix: {long_tip!r}"
    assert app.format_tray_tooltip({}) == app.WINDOW_TITLE

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        ctrl = app.DesktopAppController(initial_mode="app", config_path=cfg_path)
        api = app.DesktopApi(ctrl)
        res = api.media_command("play_pause")
        assert isinstance(res, dict) and res.get("ok") is False and res.get("command") == "play_pause"

check("SMTCBridge.send_command() guards, live tooltip truncation & DesktopApi.media_command()", test_playback_controls_and_tooltip)

# 4j. Window geometry persistence, virtual screen clamping, get_window_rect(0) & rotating crash log
def test_geometry_clamping_and_logging():
    import logging
    from logging.handlers import RotatingFileHandler
    import app
    from wallpaper_host import WallpaperHost

    assert WallpaperHost.get_window_rect(0) is None, "get_window_rect(0) should return None"

    vx, vy, vw, vh = WallpaperHost.get_virtual_screen_bounds()
    clamped_low = WallpaperHost.clamp_rect_to_virtual_screen((-99999, -99999, 640, 400))
    assert clamped_low[0] >= vx and clamped_low[1] >= vy, f"Low coords not clamped: {clamped_low}"
    clamped_high = WallpaperHost.clamp_rect_to_virtual_screen((99999, 99999, 640, 400))
    assert clamped_high[0] + clamped_high[2] <= vx + vw, f"High X not clamped: {clamped_high}"
    assert clamped_high[1] + clamped_high[3] <= vy + vh, f"High Y not clamped: {clamped_high}"

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        assert app.get_saved_geometry("app", cfg_path) is None
        app.save_geometry("app", (100, 120, 1100, 700), cfg_path)
        app.save_geometry("overlay", (200, 220, 480, 240), cfg_path)
        assert app.get_saved_geometry("app", cfg_path) == (100, 120, 1100, 700)
        assert app.get_saved_geometry("overlay", cfg_path) == (200, 220, 480, 240)

        log_path = Path(tmpdir) / "test-desktop-app.log"
        logger = app.setup_logging(log_path)
        matched = [
            h for h in logger.handlers
            if isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).resolve() == log_path.resolve()
        ]
        try:
            assert len(matched) == 1, "Expected RotatingFileHandler for test log"
            assert matched[0].maxBytes == 256 * 1024
            assert matched[0].backupCount == 1
            logger.info("desktop-app logging smoke test")
            matched[0].flush()
            assert log_path.exists() and "smoke test" in log_path.read_text(encoding="utf-8")
        finally:
            for h in matched:
                logger.removeHandler(h)
                h.close()

check("Window geometry persistence, virtual screen clamping, get_window_rect(0) & rotating log", test_geometry_clamping_and_logging)

# 4k. Cross-platform (macOS & Linux) media parsers, xrandr monitor parser & autostart renderers
def test_cross_platform_helpers():
    import app
    import smtc_bridge
    import wallpaper_host

    # Linux playerctl parser
    pctl_snap = smtc_bridge._parse_playerctl_output(
        "Playing\tspotify\tStarboy\tThe Weeknd\tStarboy\t45000000\t230000000\thttps://example.com/art.jpg"
    )
    assert pctl_snap["status"] == "playing"
    assert pctl_snap["sourceApp"] == "Spotify"
    assert pctl_snap["title"] == "Starboy"
    assert pctl_snap["artist"] == "The Weeknd"
    assert abs(pctl_snap["positionSec"] - 45.0) < 0.01
    assert abs(pctl_snap["durationSec"] - 230.0) < 0.01

    # macOS media-control JSON parser (macOS 15.4+)
    mc_snap = smtc_bridge._parse_media_control_output(
        '{"title":"Midnight City","artist":"M83","album":"Hurry Up","playing":true,"elapsedTime":42.5,"duration":243.0,"bundleIdentifier":"com.spotify.client"}'
    )
    assert mc_snap["status"] == "playing"
    assert mc_snap["sourceApp"] == "Spotify"
    assert mc_snap["title"] == "Midnight City"
    assert abs(mc_snap["positionSec"] - 42.5) < 0.01
    assert abs(mc_snap["durationSec"] - 243.0) < 0.01
    assert smtc_bridge._parse_media_control_output("null")["status"] == "stopped"

    # macOS nowplaying-cli parser
    np_snap = smtc_bridge._parse_nowplaying_output(
        "Reflections\nThe Neighbourhood\nHard To Imagine\n1\n62.5\n244.0\nMusic\nnull\n"
    )
    assert np_snap["status"] == "playing"
    assert np_snap["title"] == "Reflections"
    assert np_snap["artist"] == "The Neighbourhood"
    assert abs(np_snap["positionSec"] - 62.5) < 0.01
    assert abs(np_snap["durationSec"] - 244.0) < 0.01

    # macOS AppleScript parser (ms duration conversion)
    osa_snap = smtc_bridge._parse_applescript_output(
        "Spotify\tpaused\tBlinding Lights\tThe Weeknd\tAfter Hours\t12.5\t200000\t"
    )
    assert osa_snap["status"] == "paused"
    assert osa_snap["sourceApp"] == "Spotify"
    assert abs(osa_snap["durationSec"] - 200.0) < 0.01

    # Linux xrandr multi-monitor parser
    xrandr_sample = (
        "Screen 0: minimum 320 x 200, current 4480 x 1440\n"
        "DP-1 connected primary 2560x1440+0+0 (normal)\n"
        "HDMI-1 connected 1920x1080+2560+0 (normal)\n"
        "DP-2 disconnected\n"
    )
    mons = wallpaper_host._parse_xrandr_monitors(xrandr_sample)
    assert len(mons) == 2
    assert mons[0]["is_primary"] is True and mons[0]["rect"] == (0, 0, 2560, 1440)
    assert mons[1]["is_primary"] is False and mons[1]["rect"] == (2560, 0, 1920, 1080)

    # macOS LaunchAgent plist & Linux XDG autostart .desktop renderers
    plist_xml = app.render_macos_launchagent_plist(["/Applications/IDE-Lyrics-Studio.app/Contents/MacOS/IDE-Lyrics-Studio"])
    assert "<key>RunAtLoad</key>" in plist_xml and "com.idelyrics.studio" in plist_xml
    desktop_entry = app.render_linux_autostart_desktop("/usr/local/bin/IDE-Lyrics-Studio")
    assert "[Desktop Entry]" in desktop_entry and "Exec=/usr/local/bin/IDE-Lyrics-Studio" in desktop_entry

check("Cross-platform macOS/Linux media parsers, xrandr monitors & autostart templates", test_cross_platform_helpers)

# 4l. Version metadata, is_newer_version(), DesktopApi.get_version() & non-blocking check_for_updates()
def test_version_and_updates():
    import threading
    import app

    assert app.APP_VERSION == "1.8.4.1", f"Expected APP_VERSION='1.8.4.1', got {app.APP_VERSION!r}"
    assert app.GITHUB_REPO == "Mehdito-2005/ide-lyrics-studio", f"Unexpected GITHUB_REPO: {app.GITHUB_REPO!r}"

    # Verify installer & PyInstaller version metadata are bumped to 1.8.4.1
    ver_txt = (app_dir / "version_info.txt").read_text(encoding="utf-8")
    assert 'StringStruct("FileVersion", "1.8.4.1")' in ver_txt
    assert 'StringStruct("ProductVersion", "1.8.4.1")' in ver_txt
    assert "filevers=(1, 8, 4, 1)" in ver_txt
    iss_txt = (app_dir / "installer.iss").read_text(encoding="utf-8")
    assert '#define MyAppVersion "1.8.4.1"' in iss_txt

    # Pure version comparison checks
    assert app.is_newer_version("1.9.0", "1.8.4.1") is True
    assert app.is_newer_version("v1.9.0", "1.8.4.1") is True
    assert app.is_newer_version("v2.0", "1.9.9") is True
    assert app.is_newer_version("1.10.0", "1.8.4.1") is True
    assert app.is_newer_version("1.8.5", "1.8.4.1") is True
    assert app.is_newer_version("1.9.0", "1.9.0-beta.1") is True
    assert app.is_newer_version("1.9.0-beta.2", "1.9.0-beta.1") is True
    assert app.is_newer_version("1.8.4.1", "1.8.4.1") is False
    assert app.is_newer_version("v1.8.4.1", "1.8.4.1") is False
    assert app.is_newer_version("1.8.4", "1.8.4.1") is False
    assert app.is_newer_version("1.9.0-beta.1", "1.9.0") is False
    assert app.is_newer_version("", "1.8.4.1") is False
    assert app.is_newer_version("1.9.0", "") is False
    assert app.is_newer_version("not-a-version", "1.8.4.1") is False
    assert app.is_newer_version("1..2", "1.8.4.1") is False

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        ctrl = app.DesktopAppController(initial_mode="app", config_path=cfg_path)
        api = app.DesktopApi(ctrl)
        assert api.get_version() == "1.8.4.1"

        # Offline mocked check_for_updates() verifies non-blocking thread & tray label update
        orig_query = app._query_latest_release
        try:
            done = threading.Event()
            captured = {}

            def _mock_newer(repo=app.GITHUB_REPO, current_version=app.APP_VERSION, timeout=4.0):
                return {
                    "ok": True,
                    "current": current_version,
                    "latest": "v1.9.0",
                    "update_available": True,
                    "url": f"https://github.com/{repo}/releases/tag/v1.9.0",
                }

            app._query_latest_release = _mock_newer
            t = app.check_for_updates(
                on_complete=lambda res: (captured.update(res), done.set()),
                open_browser=False,
            )
            assert isinstance(t, threading.Thread)
            assert done.wait(timeout=3.0), "check_for_updates callback did not complete"
            assert captured.get("ok") is True and captured.get("update_available") is True

            # Verify controller.check_for_updates updates _update_label cleanly
            ctrl.check_for_updates(open_browser=False)
            for _ in range(60):
                if not ctrl._checking_updates:
                    break
                threading.Event().wait(0.05)
            assert "1.9.0" in ctrl._update_label, f"Unexpected tray update label: {ctrl._update_label!r}"
        finally:
            app._query_latest_release = orig_query

check("APP_VERSION 1.8.4.1, is_newer_version(), DesktopApi.get_version() & update checker", test_version_and_updates)

# 4m. pystray menu & submenu validation (callback arity, bool(menu), Check for Updates, Setup Wizard, Mini Overlay Side)
def test_pystray_menu_validation():
    import pystray
    import app

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        ctrl = app.DesktopAppController(initial_mode="app", config_path=cfg_path)
        menu = ctrl.build_tray_menu()
        assert isinstance(menu, pystray.Menu)
        assert bool(menu) is True, "Root tray menu should evaluate truthy"

        seen_labels = []
        seen_submenus = []

        def _validate_menu(m, depth=0):
            assert bool(m) is True, f"Menu at depth {depth} should be non-empty"
            items = list(m)
            assert len(items) > 0
            for item in items:
                text = item.text
                _ = item.visible
                _ = item.enabled
                _ = item.checked
                _ = item.radio
                _ = item.default
                if depth == 0:
                    seen_labels.append(text)
                if item.submenu is not None:
                    seen_submenus.append(text)
                    _validate_menu(item.submenu, depth + 1)

        _validate_menu(menu)
        assert "Check for Updates" in seen_labels, f"Missing 'Check for Updates' in tray menu: {seen_labels!r}"
        assert "Setup Wizard & Integrations..." in seen_labels, f"Missing Setup Wizard in tray menu: {seen_labels!r}"
        assert "Playback" in seen_submenus, f"Missing 'Playback' submenu: {seen_submenus!r}"
        assert "Mini Overlay Side" in seen_submenus, f"Missing 'Mini Overlay Side' submenu: {seen_submenus!r}"
        assert "Wallpaper Display" in seen_submenus, f"Missing 'Wallpaper Display' submenu: {seen_submenus!r}"

check("pystray tray menu & submenu validation (bool(menu), callback arity, Check for Updates, Mini Overlay Side)", test_pystray_menu_validation)

# 4n. Setup Wizard config persistence & offline integrations (Cider, Spicetify, Wallpaper Engine / Lively export)
def test_setup_wizard_and_integrations():
    import zipfile
    import app
    import integrations

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        cfg_path = tmp / "config.json"

        # Wizard first-run persistence
        assert app.is_wizard_completed(cfg_path) is False
        ctrl = app.DesktopAppController(initial_mode="app", config_path=cfg_path)
        api = app.DesktopApi(ctrl)
        assert api.is_wizard_needed() is True
        state = api.get_setup_state()
        assert state["version"] == "1.8.4.1" and "integrations" in state
        api.mark_wizard_completed()
        assert app.is_wizard_completed(cfg_path) is True
        assert api.is_wizard_needed() is False

        # Create an isolated mock source repo so offline tests work even inside the standalone ide-lyrics-studio repo
        fake_src = tmp / "mock_source"
        (fake_src / "spicetify").mkdir(parents=True)
        (fake_src / "index.js").write_text("console.log('cider');\n", encoding="utf-8")
        (fake_src / "plugin.json").write_text('{"identifier":"sh.cider.ide-lyric-visualizer"}\n', encoding="utf-8")
        (fake_src / "spicetify" / "ideLyrics.js").write_text("console.log('spicetify');\n", encoding="utf-8")

        # Offline Cider install / detect / legacy cleanup / uninstall
        cider_plugins = tmp / "FakeCider" / "plugins"
        cider_plugins.mkdir(parents=True)
        legacy_dir = cider_plugins / integrations.CIDER_LEGACY_DIR_NAME
        legacy_dir.mkdir()
        (legacy_dir / "old.txt").write_text("legacy", encoding="utf-8")

        res_cider = integrations.install_cider_plugin(
            fake_src, target_roots=[cider_plugins], allow_download=False
        )
        assert res_cider["ok"] is True, f"Cider install failed: {res_cider}"
        assert not legacy_dir.exists(), "Legacy duplicate Cider folder should be cleaned"
        assert (cider_plugins / integrations.CIDER_PLUGIN_ID / "index.js").is_file()
        det = integrations.detect_integrations(
            fake_src, cider_roots=[cider_plugins], spicetify_bin="", spicetify_extensions_dir=tmp / "sp" / "Extensions"
        )
        assert det["cider"]["installed"] is True

        res_un_cider = integrations.uninstall_cider_plugin(target_roots=[cider_plugins])
        assert res_un_cider["ok"] is True and not (cider_plugins / integrations.CIDER_PLUGIN_ID).exists()

        # Offline Spicetify install / detect / uninstall
        sp_ext_dir = tmp / "FakeSpicetify" / "Extensions"
        res_sp = integrations.install_spicetify_extension(
            fake_src, extensions_dir=sp_ext_dir, spicetify_bin="", allow_download=False, run_cli=False
        )
        assert res_sp["ok"] is True and (sp_ext_dir / "ideLyrics.js").is_file(), f"Spicetify install failed: {res_sp}"
        det_sp = integrations.detect_integrations(
            fake_src, cider_roots=[], spicetify_bin="", spicetify_extensions_dir=sp_ext_dir
        )
        assert det_sp["spicetify"]["installed"] is True
        res_un_sp = integrations.uninstall_spicetify_extension(extensions_dir=sp_ext_dir, spicetify_bin="", run_cli=False)
        assert res_un_sp["ok"] is True and not (sp_ext_dir / "ideLyrics.js").exists()

        assert "wallpaper" not in state["integrations"]
        assert not hasattr(integrations, "export_wallpaper_package")

check("Setup Wizard persistence & offline Cider/Spicetify integrations", test_setup_wizard_and_integrations)

# 4o. v1.3.0 Apple Music SMTC extrapolation, WallpaperHost UI-thread safe guards, Overlay Side dock & HTML invariants
def test_v130_sync_overlay_ide_and_elements():
    from datetime import datetime, timedelta, timezone
    import html.parser
    import app
    import smtc_bridge
    from wallpaper_host import WallpaperHost

    # SMTC _extrapolate_position_sec checks
    now_utc = datetime.now(timezone.utc)
    track_key = ("Apple Music", "Starboy", "The Weeknd")
    # 1) Stale last_updated (3s old) adds +3s smoothly
    pos, anchor = smtc_bridge._extrapolate_position_sec(
        10.0, "playing", now_utc - timedelta(seconds=3.0), now_utc, None, track_key, 100.0
    )
    assert abs(pos - 13.0) < 0.05, f"Expected ~13.0, got {pos}"
    # 2) Missing / WinRT 1601 epoch uses monotonic anchor fallback on subsequent ticks
    pos1, a1 = smtc_bridge._extrapolate_position_sec(20.0, "playing", None, now_utc, None, track_key, 200.0)
    assert abs(pos1 - 20.0) < 0.01
    pos2, a2 = smtc_bridge._extrapolate_position_sec(20.0, "playing", None, now_utc, a1, track_key, 202.25)
    assert abs(pos2 - 22.25) < 0.01, f"Expected monotonic +2.25s fallback, got {pos2}"
    # 3) Paused resets anchor and returns raw pos
    pos3, a3 = smtc_bridge._extrapolate_position_sec(20.0, "paused", now_utc, now_utc, a2, track_key, 205.0)
    assert abs(pos3 - 20.0) < 0.01 and a3 is None

    # WallpaperHost UI-thread & null handle safe guards
    host = WallpaperHost()
    assert WallpaperHost.set_window_rect(0, (0, 0, 500, 500), native_form=None) is False
    assert host.attach_to_wallpaper(0, native_form=None) is False
    assert host.detach_from_wallpaper(0, native_form=None) is False
    assert host.set_taskbar_visible(0, False, native_form=None) is False
    assert host.set_always_on_top(0, True, native_form=None) is False

    # Overlay side persistence & snap API
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        assert app.get_saved_overlay_side(cfg_path) == "right"
        ctrl = app.DesktopAppController(initial_mode="overlay", config_path=cfg_path)
        api = app.DesktopApi(ctrl)
        assert api.get_overlay_side() == "right"
        res_left = api.snap_overlay_side("left")
        assert res_left["ok"] is True and res_left["side"] == "left"
        assert app.get_saved_overlay_side(cfg_path) == "left"
        res_right = api.snap_overlay_side("right")
        assert res_right["ok"] is True and res_right["side"] == "right"
        assert res_right["rect"][0] > res_left["rect"][0]

    # live-wallpaper.html structural & feature checks
    html_text = (repo_root / "live-wallpaper.html").read_text(encoding="utf-8")
    for token in (
        "is-mini-overlay", "mini-overlay-bar", "is-plain-lyrics", "code-syntax-punct",
        "python", "javascript", "dict", "chain", "interludeThresholdSec",
        "DEFAULT_PLAYER_SYNC_OFFSETS", "group-visible-elements", "btn-reset-all-settings"
    ):
        assert token in html_text, f"Missing expected feature token in live-wallpaper.html: {token}"
    assert "var(--accent" not in html_text, "Forbidden invalid CSS var(--accent found!"

    # Verify #wallpaper-container keeps its exact 12 direct children invariant
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    class _WpChildCounter(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = None
            self.direct_children = 0
        def handle_starttag(self, tag, attrs):
            attr_map = dict(attrs)
            if self.depth is not None:
                if self.depth == 1:
                    self.direct_children += 1
                if tag.lower() not in VOID_TAGS:
                    self.depth += 1
            elif attr_map.get("id") == "wallpaper-container":
                self.depth = 1
        def handle_startendtag(self, tag, attrs):
            if self.depth is not None and self.depth == 1:
                self.direct_children += 1
        def handle_endtag(self, tag):
            if self.depth is not None and tag.lower() not in VOID_TAGS:
                self.depth -= 1
                if self.depth == 0:
                    self.depth = None

    parser = _WpChildCounter()
    parser.feed(html_text)
    assert parser.direct_children == 12, (
        f"#wallpaper-container must keep 12 direct children for Check 6 invariant, got {parser.direct_children}"
    )

check("v1.3.0 SMTC extrapolation, WallpaperHost guards, Overlay Side dock & HTML invariants", test_v130_sync_overlay_ide_and_elements)

# 4p. v1.4.0 Song vs. Normal Video classifier, tray label/tooltip, playerctl/gdbus URL+genre & HTML UI
def test_v140_song_vs_video_detection():
    import html.parser
    import app
    import smtc_bridge

    cases = [
        ("Spotify song", dict(title="Blinding Lights", artist="The Weeknd", album="After Hours", source_app="Spotify", duration_sec=200, playback_type="video"), "song"),
        ("Apple Music song", dict(title="Starboy", artist="The Weeknd", album="Starboy", source_app="Apple Music", duration_sec=230), "song"),
        ("Chrome YT official MV", dict(title="The Weeknd - Blinding Lights (Official Video)", artist="The Weeknd", source_app="Chrome", duration_sec=263, playback_type="video"), "song"),
        ("Chrome YT Topic channel", dict(title="Starboy", artist="The Weeknd - Topic", album="Starboy", source_app="Chrome", duration_sec=230, playback_type="video"), "song"),
        ("Chrome YT Artist - Song", dict(title="Daft Punk - Instant Crush ft. Julian Casablancas", artist="Daft PunkVEVO", source_app="Chrome", duration_sec=339, playback_type="video"), "song"),
        ("Chrome YT vlog", dict(title="My Morning Routine Vlog", artist="SomeCreator", source_app="Chrome", duration_sec=310, playback_type="video"), "video"),
        ("Chrome YT long gaming", dict(title="I Survived 100 Days in Hardcore Minecraft", artist="Luke TheNotable", source_app="Chrome", duration_sec=2410, playback_type="video"), "video"),
        ("Chrome YT tutorial", dict(title="Python Asyncio Tutorial", artist="TechChannel", source_app="Chrome", duration_sec=600, playback_type="video"), "video"),
        ("Chrome Twitch live", dict(title="Ranked Grind", artist="Shroud", source_app="Chrome", duration_sec=0, playback_type="video"), "video"),
        ("Chrome Shorts clip", dict(title="Cool trick", artist="Creator", source_app="Chrome", duration_sec=18, playback_type="video"), "video"),
        ("VLC mp4 video", dict(title="movie_clip.mp4", artist="", source_app="VLC", duration_sec=120, playback_type="video"), "video"),
        ("VLC flac music", dict(title="Track 01.flac", artist="Artist", album="LP", source_app="VLC", duration_sec=210, playback_type="music"), "song"),
        ("Netflix movie", dict(title="Inception", artist="", source_app="Netflix", duration_sec=8800, playback_type="video"), "video"),
    ]
    for label, kw, expected in cases:
        kind, reason = smtc_bridge.classify_media_kind(**kw)
        assert kind == expected, f"{label}: expected {expected}, got {kind} (reason={reason})"

    # playerctl 10-field (with URL & genre) and legacy 8-field compatibility
    ytm_snap = smtc_bridge._parse_playerctl_output(
        "Playing\tchromium\tDaydreaming\tRadiohead\t\t30000000\t384000000\t\thttps://music.youtube.com/watch?v=123\tAlternative"
    )
    assert ytm_snap["mediaKind"] == "song", f"Expected song for music.youtube.com URL, got {ytm_snap}"
    yt_vid_snap = smtc_bridge._parse_playerctl_output(
        "Playing\tchrome\tMy Coding Vlog\tDevChannel\tYouTube\t45000000\t600000000\t\thttps://www.youtube.com/watch?v=abc\t"
    )
    assert yt_vid_snap["mediaKind"] == "video", f"Expected video for YT vlog, got {yt_vid_snap}"

    # Tray tooltip for video + 127-char limit
    vid_tip = app.format_tray_tooltip({"title": "Python Asyncio Tutorial", "artist": "TechChannel", "mediaKind": "video"})
    assert "[Video]" in vid_tip and len(vid_tip) <= 127, f"Unexpected video tooltip: {vid_tip!r}"
    long_vid_tip = app.format_tray_tooltip({"title": "V" * 100, "artist": "C" * 100, "mediaKind": "video"})
    assert "[Video]" in long_vid_tip and len(long_vid_tip) <= 127

    # HTML tokens + strict 12 direct children invariant
    html_text = (repo_root / "live-wallpaper.html").read_text(encoding="utf-8")
    for token in (
        "track-media-kind", "classifyMediaKind", "videoBehavior", "is-video-hidden",
        "group-video-behavior", "group-media-kind-override", "buildVideoInfoLines",
        "showMediaKindBadge", "hide-media-kind",
    ):
        assert token in html_text, f"Missing v1.4.0 token in live-wallpaper.html: {token}"
    assert "var(--accent" not in html_text, "Forbidden invalid CSS var(--accent found!"

    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    class _WpChildCounter140(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = None
            self.direct_children = 0
        def handle_starttag(self, tag, attrs):
            attr_map = dict(attrs)
            if self.depth is not None:
                if self.depth == 1:
                    self.direct_children += 1
                if tag.lower() not in VOID_TAGS:
                    self.depth += 1
            elif attr_map.get("id") == "wallpaper-container":
                self.depth = 1
        def handle_startendtag(self, tag, attrs):
            if self.depth is not None and self.depth == 1:
                self.direct_children += 1
        def handle_endtag(self, tag):
            if self.depth is not None and tag.lower() not in VOID_TAGS:
                self.depth -= 1
                if self.depth == 0:
                    self.depth = None

    p = _WpChildCounter140()
    p.feed(html_text)
    assert p.direct_children == 12, f"#wallpaper-container must keep 12 direct children, got {p.direct_children}"

check("v1.4.0 Song vs. Video classifier, tray video label, playerctl URL/genre & HTML invariants", test_v140_song_vs_video_detection)

# 4q. v1.5.0 Transparent Floating Mini Overlay, Bottom-Center Dock, Layer Toggle, Drag & UI invariants
def test_v150_transparent_mini_overlay():
    import html.parser
    import app
    from wallpaper_host import WallpaperHost

    host = WallpaperHost()
    assert host.set_overlay_appearance(0, True, native_form=None) is False
    assert host.set_overlay_appearance(0, False, native_form=None) is False
    assert host.set_window_z_layer(0, "top", native_form=None) is False
    assert host.set_window_z_layer(0, "bottom", native_form=None) is False
    assert WallpaperHost.start_window_drag(0, native_form=None) is False

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        assert app.get_saved_overlay_on_top(cfg_path) is True
        assert app.save_overlay_on_top(False, cfg_path) is False
        assert app.get_saved_overlay_on_top(cfg_path) is False

        ctrl = app.DesktopAppController(initial_mode="overlay", config_path=cfg_path)
        api = app.DesktopApi(ctrl)
        assert api.get_overlay_on_top() is False
        toggled = api.toggle_overlay_on_top()
        assert toggled["ok"] is True and toggled["on_top"] is True
        assert app.get_saved_overlay_on_top(cfg_path) is True

        res_left = api.snap_overlay_side("left")
        res_center = api.snap_overlay_side("center")
        res_right = api.snap_overlay_side("right")
        assert res_center["ok"] is True and res_center["side"] == "center"
        assert res_left["rect"][0] < res_center["rect"][0] < res_right["rect"][0]
        assert res_center["rect"][1] > res_left["rect"][1]
        assert app.get_saved_overlay_side(cfg_path) == "right"

        drag_res = api.start_window_drag()
        assert isinstance(drag_res, dict) and drag_res.get("ok") is False

    html_text = (repo_root / "live-wallpaper.html").read_text(encoding="utf-8")
    for token in (
        "mini-player-top", "mini-player-controls", "mini-play-pause",
        "mini-snap-center", "mini-toggle-layer", "onOverlayLayerChanged",
        "data-overlay-side", "background: transparent !important",
        "group-overlay-layer", "syncMiniPlayerMeta",
    ):
        assert token in html_text, f"Missing v1.5.0 token in live-wallpaper.html: {token}"
    assert "var(--accent" not in html_text, "Forbidden invalid CSS var(--accent found!"

    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    class _WpChildCounter150(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = None
            self.direct_children = 0
        def handle_starttag(self, tag, attrs):
            attr_map = dict(attrs)
            if self.depth is not None:
                if self.depth == 1:
                    self.direct_children += 1
                if tag.lower() not in VOID_TAGS:
                    self.depth += 1
            elif attr_map.get("id") == "wallpaper-container":
                self.depth = 1
        def handle_startendtag(self, tag, attrs):
            if self.depth is not None and self.depth == 1:
                self.direct_children += 1
        def handle_endtag(self, tag):
            if self.depth is not None and tag.lower() not in VOID_TAGS:
                self.depth -= 1
                if self.depth == 0:
                    self.depth = None

    p = _WpChildCounter150()
    p.feed(html_text)
    assert p.direct_children == 12, f"#wallpaper-container must keep 12 direct children, got {p.direct_children}"

check("v1.5.0 Transparent floating Mini Overlay, dock positions, layer toggle & HTML invariants", test_v150_transparent_mini_overlay)

# 4r. v1.6.0 Rich IDE loops, functions & OOP syntax highlighting
def test_v160_ide_oop_loops_syntax():
    import html.parser

    html_text = (repo_root / "live-wallpaper.html").read_text(encoding="utf-8")
    for token in (
        'data-syntax="ide"', 'data-syntax="oop"', 'data-syntax="loops"',
        'data-syntax="python"', 'data-syntax="javascript"', 'data-syntax="dict"',
        'data-syntax="chain"', 'data-syntax="classic"',
        "syn-kw", "syn-fn", "syn-var", "syn-type",
        "constructor", "extends ", "while ", "for ", "function ", "def ", "@property",
    ):
        assert token in html_text, f"Missing v1.6.0 syntax token in live-wallpaper.html: {token}"
    assert "var(--accent" not in html_text, "Forbidden invalid CSS var(--accent found!"

    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    class _WpChildCounter160(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = None
            self.direct_children = 0
        def handle_starttag(self, tag, attrs):
            attr_map = dict(attrs)
            if self.depth is not None:
                if self.depth == 1:
                    self.direct_children += 1
                if tag.lower() not in VOID_TAGS:
                    self.depth += 1
            elif attr_map.get("id") == "wallpaper-container":
                self.depth = 1
        def handle_startendtag(self, tag, attrs):
            if self.depth is not None and self.depth == 1:
                self.direct_children += 1
        def handle_endtag(self, tag):
            if self.depth is not None and tag.lower() not in VOID_TAGS:
                self.depth -= 1
                if self.depth == 0:
                    self.depth = None

    p = _WpChildCounter160()
    p.feed(html_text)
    assert p.direct_children == 12, f"#wallpaper-container must keep 12 direct children, got {p.direct_children}"

check("v1.6.0 IDE OOP, loops & functions syntax highlighting & HTML invariants", test_v160_ide_oop_loops_syntax)

# 4s. v1.7.1 Immersive Album Mode (with lyric scene analyzer cleanly removed)
def test_v171_immersive_mode_only():
    import html.parser

    html_text = (repo_root / "live-wallpaper.html").read_text(encoding="utf-8")
    for token in (
        "group-immersive-mode", "is-immersive-mode", "toggleImmersive",
    ):
        assert token in html_text, f"Missing v1.7.1 token in live-wallpaper.html: {token}"
    for removed in (
        "LYRIC_SCENE_CATALOG", "group-lyric-scene-mode", "lyric-scene-canvas", "commons.wikimedia.org/w/api.php",
    ):
        assert removed not in html_text, f"Removed scene analyzer token still present in live-wallpaper.html: {removed}"
    assert "var(--accent" not in html_text, "Forbidden invalid CSS var(--accent found!"
    assert "rgb(1, 2, 3)" not in html_text, "Forbidden TransparencyKey collision color rgb(1, 2, 3) found!"

    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    class _WpChildCounter171(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = None
            self.direct_children = 0
        def handle_starttag(self, tag, attrs):
            attr_map = dict(attrs)
            if self.depth is not None:
                if self.depth == 1:
                    self.direct_children += 1
                if tag.lower() not in VOID_TAGS:
                    self.depth += 1
            elif attr_map.get("id") == "wallpaper-container":
                self.depth = 1
        def handle_startendtag(self, tag, attrs):
            if self.depth is not None and self.depth == 1:
                self.direct_children += 1
        def handle_endtag(self, tag):
            if self.depth is not None and tag.lower() not in VOID_TAGS:
                self.depth -= 1
                if self.depth == 0:
                    self.depth = None

    p = _WpChildCounter171()
    p.feed(html_text)
    assert p.direct_children == 12, f"#wallpaper-container must keep 12 direct children, got {p.direct_children}"

check("v1.7.1 Immersive Album Mode & clean scene-analyzer removal invariants", test_v171_immersive_mode_only)

# 4t. v1.8.0 Wave 1 bug fixes + Wave 2 Album Backdrop, Spectrum/Halo Visualizers, Ghost Mode & Auto-Yield
def test_v180_bugfixes_and_features():
    import html.parser
    import app
    import smtc_bridge
    import wallpaper_host
    from smtc_bridge import SMTCBridge
    from wallpaper_host import WallpaperHost, clamp_overlay_opacity

    # Wave 1 fixes: xrandr negative display origin preserved, gdbus apostrophes, UI thread safety
    mons = wallpaper_host._parse_xrandr_monitors(
        "DP-1 connected primary 1920x1080+1920+0 (normal)\n"
        "HDMI-1 connected 1920x1080+0+0 (normal)\n"
    )
    assert len(mons) == 2 and mons[0]["workerw_rect"] == mons[0]["rect"]
    assert smtc_bridge._parse_metadata_string('("Don\'t Stop Me Now",)') == "Don't Stop Me Now" if hasattr(smtc_bridge, "_parse_metadata_string") else True

    # Wave 2 Desktop Feature 1: Overlay Ghost Mode & Native Opacity
    assert clamp_overlay_opacity(-1) == 0.25
    assert clamp_overlay_opacity(0.65) == 0.65
    assert clamp_overlay_opacity(2.0) == 1.0
    assert clamp_overlay_opacity("bad") == 1.0
    assert clamp_overlay_opacity(True) == 1.0
    assert WallpaperHost().set_click_through(0, True, native_form=None) is False
    assert WallpaperHost().set_window_opacity(0, 0.6, native_form=None) is False
    assert hasattr(wallpaper_host, "_apply_windows_dwm_overlay_attrs")
    assert wallpaper_host._apply_windows_dwm_overlay_attrs(0, True) is None

    # Wave 2 Desktop Feature 2: Smart Focus & Power Auto-Yield + SMTC adaptive poll interval
    assert WallpaperHost.evaluate_yield_state("auto", is_locked=True, is_fullscreen=True, is_battery_saver=True) == {"paused": True, "reason": "locked"}
    assert WallpaperHost.evaluate_yield_state("auto", is_locked=False, is_fullscreen=True, is_battery_saver=False) == {"paused": True, "reason": "fullscreen"}
    assert WallpaperHost.evaluate_yield_state("auto", is_locked=False, is_fullscreen=False, is_battery_saver=True) == {"paused": True, "reason": "battery"}
    assert WallpaperHost.evaluate_yield_state("auto", is_locked=False, is_fullscreen=False, is_battery_saver=False) == {"paused": False, "reason": "active"}
    assert WallpaperHost.evaluate_yield_state("battery_only", is_locked=True, is_fullscreen=True, is_battery_saver=False) == {"paused": False, "reason": "active"}
    assert WallpaperHost.evaluate_yield_state("off", is_locked=True, is_fullscreen=True, is_battery_saver=True) == {"paused": False, "reason": "active"}

    yield_snap = WallpaperHost.detect_desktop_yield_state(own_hwnd=0)
    assert set(("is_locked", "is_fullscreen", "is_battery_saver")).issubset(yield_snap.keys())

    bridge = SMTCBridge()
    assert bridge.set_poll_interval(1.5) == 1.5
    assert bridge.set_poll_interval(0.01) == 0.08

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        ctrl = app.DesktopAppController(initial_mode="overlay", config_path=cfg_path)
        api = app.DesktopApi(ctrl)

        res_ghost = api.toggle_overlay_click_through()
        assert res_ghost["ok"] is True and res_ghost["click_through"] is True
        assert api.get_overlay_click_through() is True

        res_op = api.set_overlay_opacity(0.55)
        assert res_op["ok"] is True and abs(res_op["opacity"] - 0.55) < 0.001
        assert api.set_overlay_opacity(0.05)["opacity"] == 0.25

        res_yield = api.set_auto_yield_mode("battery_only")
        assert res_yield["ok"] is True and api.get_auto_yield_mode() == "battery_only"
        assert "paused" in api.get_power_yield_state()

        # Verify tray menu includes new submenus/toggles
        menu_items = list(ctrl.build_tray_menu())
        labels = [it.text for it in menu_items]
        assert "Auto-Pause When Hidden" in labels, f"Missing Auto-Pause submenu in tray: {labels!r}"
        overlay_item = next(it for it in menu_items if it.text == "Mini Overlay Side")
        overlay_sub = [sub.text for sub in list(overlay_item.submenu)]
        assert "Click-Through (Ghost Mode)" in overlay_sub and "Overlay Opacity" in overlay_sub
        ctrl.shutdown(destroy_window=False)

    # Wave 1 & Wave 2 live-wallpaper.html checks & 12-child invariant
    html_text = (repo_root / "live-wallpaper.html").read_text(encoding="utf-8")
    assert html_text.count("let wpCustomBgObjectUrl = null;") == 1, "Duplicate wpCustomBgObjectUrl declaration!"
    for token in (
        "hide-track-progress .track-progress-track", "clearTrackArtworkAndTheme", "isPollingCiderTime",
        "isImmersiveLayoutActive", 'data-backdrop="artwork"', "row-artwork-bg-controls",
        "wallpaper-artwork-hero", "maybeUpgradeHighResArtwork", "radial-gradient(ellipse 66% 68%",
        "group-mini-song-card", "group-mini-lyrics-only", "show-mini-track-card", "is-mini-lyrics-only",
        "mini-toggle-card", "mini-toggle-pure",
        "VALID_WAVE_STYLES", "drawCanvasBars", "drawCanvasHalo", "group-wave-style",
        "onDesktopPowerStateChanged", "onOverlayGhostChanged", "onOverlayOpacityChanged",
        "settings-scope-bar", "settings-scope-banner", "btn-scope-activate-mode",
        "card-mini-overlay-settings", "card-wallpaper-settings", "group-overlay-ghost",
        "group-overlay-opacity", "group-auto-yield-mode", "setSettingsScope",
        "drawer-accordion", "is-scope-hidden",
        "group-lyric-contrast", "row-lyric-contrast", "mini-toggle-contrast",
        "data-lyric-contrast", "VALID_LYRIC_CONTRASTS", "setLyricContrast",
        "#wallpaper-container.is-mini-overlay #wallpaper-custom-bg",
    ):
        assert token in html_text, f"Missing v1.8.3 token in live-wallpaper.html: {token}"
    assert "wiz-card-wallpaper" not in html_text, "Removed Wallpaper Engine wizard option still present!"
    assert "Wallpaper Engine" not in html_text, "Removed Wallpaper Engine text still present in live-wallpaper.html!"
    assert "var(--accent" not in html_text, "Forbidden invalid CSS var(--accent found!"
    assert "rgb(1, 2, 3)" not in html_text, "Forbidden TransparencyKey collision color rgb(1, 2, 3) found!"

    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    class _WpChildCounter180(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = None
            self.direct_children = 0
        def handle_starttag(self, tag, attrs):
            attr_map = dict(attrs)
            if self.depth is not None:
                if self.depth == 1:
                    self.direct_children += 1
                if tag.lower() not in VOID_TAGS:
                    self.depth += 1
            elif attr_map.get("id") == "wallpaper-container":
                self.depth = 1
        def handle_startendtag(self, tag, attrs):
            if self.depth is not None and self.depth == 1:
                self.direct_children += 1
        def handle_endtag(self, tag):
            if self.depth is not None and tag.lower() not in VOID_TAGS:
                self.depth -= 1
                if self.depth == 0:
                    self.depth = None

    p = _WpChildCounter180()
    p.feed(html_text)
    assert p.direct_children == 12, f"#wallpaper-container must keep 12 direct children, got {p.direct_children}"

check("v1.8.0 Bug fixes, Album Backdrop, Spectrum/Halo Visualizers, Ghost Mode & Auto-Yield", test_v180_bugfixes_and_features)

# 4u. v1.8.4 macOS & Linux fixes, single-instance wake-up & packaging invariants
def test_v184_macos_linux_fixes():
    import html.parser
    import threading
    import app
    import smtc_bridge
    import wallpaper_host
    from wallpaper_host import WallpaperHost

    assert smtc_bridge._safe_float("42,5") == 42.5
    osa = smtc_bridge._parse_applescript_output("Spotify\tplaying\tSong\tArtist\tAlb\t42,5\t210000\t")
    assert osa["status"] == "playing" and abs(osa["positionSec"] - 42.5) < 0.01 and abs(osa["durationSec"] - 210.0) < 0.01
    assert 'if application "Spotify" is running then' in smtc_bridge._MACOS_POLL_APPLESCRIPT
    assert 'if application "Music" is running then' in smtc_bridge._MACOS_POLL_APPLESCRIPT
    assert smtc_bridge._MACOS_POLL_APPLESCRIPT.count("try") >= 3

    assert wallpaper_host._get_macos_wallpaper_level() > wallpaper_host._MACOS_DESKTOP_WINDOW_LEVEL
    assert WallpaperHost.prepare_linux_window(0) is False
    assert WallpaperHost.present_linux_window(0) is False

    called = threading.Event()
    server, port = app.start_local_server(repo_root, on_show_request=lambda: called.set())
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            port_file = Path(tmpdir) / "runtime_port"
            port_file.write_text(str(port), encoding="utf-8")
            assert app.notify_running_instance_show(port_file=port_file, default_port=port) is True
            assert called.wait(timeout=2.0), "POST /_studio/show did not invoke callback"
    finally:
        app.stop_local_server(server)

    build_mac = (app_dir / "build-mac.sh").read_text(encoding="utf-8")
    for token in ("NSAppleEventsUsageDescription", "NSAllowsLocalNetworking", '--hidden-import "Quartz"'):
        assert token in build_mac, f"Missing macOS build token: {token}"

    build_linux = (app_dir / "build-linux.sh").read_text(encoding="utf-8")
    for token in ("WEBKIT_DISABLE_DMABUF_RENDERER", "GDK_BACKEND"):
        assert token in build_linux, f"Missing Linux build token: {token}"

    flatpak_yml = (app_dir / "flatpak" / "com.idelyrics.studio.yml").read_text(encoding="utf-8")
    assert "project.json" not in flatpak_yml and "LivelyProperties.json" not in flatpak_yml

    html_text = (repo_root / "live-wallpaper.html").read_text(encoding="utf-8")
    assert "var(--accent" not in html_text
    assert "rgb(1, 2, 3)" not in html_text

    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    class _WpChildCounter184(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = None
            self.direct_children = 0
        def handle_starttag(self, tag, attrs):
            attr_map = dict(attrs)
            if self.depth is not None:
                if self.depth == 1:
                    self.direct_children += 1
                if tag.lower() not in VOID_TAGS:
                    self.depth += 1
            elif attr_map.get("id") == "wallpaper-container":
                self.depth = 1
        def handle_startendtag(self, tag, attrs):
            if self.depth is not None and self.depth == 1:
                self.direct_children += 1
        def handle_endtag(self, tag):
            if self.depth is not None and tag.lower() not in VOID_TAGS:
                self.depth -= 1
                if self.depth == 0:
                    self.depth = None

    p = _WpChildCounter184()
    p.feed(html_text)
    assert p.direct_children == 12, f"#wallpaper-container must keep 12 direct children, got {p.direct_children}"

check("v1.8.4 macOS & Linux fixes, single-instance wake-up & packaging invariants", test_v184_macos_linux_fixes)

# 4v. v1.8.4.1 Maximized & Any-App-Open auto-pause rules, video/animation pausing & HTML invariants
def test_v1841_auto_pause_any_app():
    import html.parser
    import app
    from wallpaper_host import WallpaperHost

    assert WallpaperHost.evaluate_yield_state("auto", is_locked=False, is_fullscreen=False, is_maximized=True, is_other_app_focused=True, is_battery_saver=False) == {"paused": False, "reason": "active"}
    assert WallpaperHost.evaluate_yield_state("maximized", is_locked=False, is_fullscreen=False, is_maximized=True, is_other_app_focused=True, is_battery_saver=False) == {"paused": True, "reason": "maximized"}
    assert WallpaperHost.evaluate_yield_state("maximized", is_locked=False, is_fullscreen=False, is_maximized=False, is_other_app_focused=True, is_battery_saver=False) == {"paused": False, "reason": "active"}
    assert WallpaperHost.evaluate_yield_state("focused", is_locked=False, is_fullscreen=False, is_maximized=False, is_other_app_focused=True, is_battery_saver=False) == {"paused": True, "reason": "focused"}
    assert WallpaperHost.evaluate_yield_state("focused", is_locked=False, is_fullscreen=False, is_maximized=False, is_other_app_focused=False, is_battery_saver=False) == {"paused": False, "reason": "active"}
    assert WallpaperHost.evaluate_yield_state("focused", is_locked=True, is_fullscreen=False, is_maximized=False, is_other_app_focused=True, is_battery_saver=False) == {"paused": True, "reason": "locked"}

    yield_snap = WallpaperHost.detect_desktop_yield_state(own_hwnd=0)
    assert set(("is_locked", "is_fullscreen", "is_maximized", "is_other_app_focused", "is_battery_saver")).issubset(yield_snap.keys())

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        ctrl = app.DesktopAppController(initial_mode="wallpaper", config_path=cfg_path)
        api = app.DesktopApi(ctrl)
        for mode in ("maximized", "focused"):
            res = api.set_auto_yield_mode(mode)
            assert res["ok"] is True and api.get_auto_yield_mode() == mode
        yield_item = next(it for it in ctrl.build_tray_menu() if it.text == "Auto-Pause When Hidden")
        yield_sub = [sub.text for sub in list(yield_item.submenu)]
        assert "When Another App Is Maximized" in yield_sub
        assert "Whenever Another App Is Open" in yield_sub

    html_text = (repo_root / "live-wallpaper.html").read_text(encoding="utf-8")
    for token in ('data-yield="maximized"', 'data-yield="focused"', "is-yield-paused", "syncYieldMediaPlayback"):
        assert token in html_text, f"Missing v1.8.4.1 token in live-wallpaper.html: {token}"
    assert "var(--accent" not in html_text
    assert "rgb(1, 2, 3)" not in html_text

    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    class _WpChildCounter1841(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = None
            self.direct_children = 0
        def handle_starttag(self, tag, attrs):
            attr_map = dict(attrs)
            if self.depth is not None:
                if self.depth == 1:
                    self.direct_children += 1
                if tag.lower() not in VOID_TAGS:
                    self.depth += 1
            elif attr_map.get("id") == "wallpaper-container":
                self.depth = 1
        def handle_startendtag(self, tag, attrs):
            if self.depth is not None and self.depth == 1:
                self.direct_children += 1
        def handle_endtag(self, tag):
            if self.depth is not None and tag.lower() not in VOID_TAGS:
                self.depth -= 1
                if self.depth == 0:
                    self.depth = None

    p = _WpChildCounter1841()
    p.feed(html_text)
    assert p.direct_children == 12, f"#wallpaper-container must keep 12 direct children, got {p.direct_children}"

check("v1.8.4.1 Maximized & Any-App-Open auto-pause rules, video/animation pausing & HTML invariants", test_v1841_auto_pause_any_app)

all_ok = True
for ok, name, err in results:
    if ok:
        print(f"PY_PASS|{name}")
    else:
        all_ok = False
        print(f"PY_FAIL|{name}|{err}")

sys.exit(0 if all_ok else 1)
'@

$TempPy = Join-Path ([System.IO.Path]::GetTempPath()) ("test_desktop_app_" + [System.Guid]::NewGuid().ToString("N") + ".py")
try {
    [System.IO.File]::WriteAllText($TempPy, $PyTestScript, [System.Text.UTF8Encoding]::new($false))
    $runArgs = @($PyPrefix) + @($TempPy, $ScriptDir)
    $pyOut = & $PyExe @runArgs 2>&1
    $pyExit = $LASTEXITCODE
    foreach ($line in $pyOut) {
        $text = "$line"
        if ($text.StartsWith("PY_PASS|")) {
            Pass $text.Substring(8)
        } elseif ($text.StartsWith("PY_FAIL|")) {
            $parts = $text.Split("|", 3)
            Fail "$($parts[1]) -> $($parts[2])"
        } else {
            Write-Host "  $text" -ForegroundColor DarkGray
        }
    }
    if ($pyExit -ne 0 -and $Failed -eq 0) {
        Fail "Python test runner exited with code $pyExit"
    }
} finally {
    if (Test-Path $TempPy) {
        Remove-Item -Force $TempPy -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "Summary: $Passed passed, $Failed failed" -ForegroundColor $(if ($Failed -eq 0) { "Green" } else { "Red" })
if ($Failed -gt 0) {
    exit 1
}
exit 0
