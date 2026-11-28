"""Standalone cross-platform desktop shell for IDE Lyrics Studio.

Serves live-wallpaper.html over localhost inside a pywebview window, relays
system media playback state via SMTCBridge, and switches between desktop app,
live wallpaper, and always-on-top mini overlay modes from the system tray.
"""

from __future__ import annotations

import argparse
import atexit
from contextlib import suppress
import ctypes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import inspect
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable, Optional
import urllib.error
import urllib.request
import webbrowser
import xml.sax.saxutils as saxutils

_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS = sys.platform == "darwin"
_IS_LINUX = sys.platform.startswith("linux")

if _IS_LINUX:
    os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
    os.environ.setdefault("GDK_BACKEND", "x11,wayland")

IS_FROZEN = bool(getattr(sys, "frozen", False))


def _user_data_dir() -> Path:
    override = os.environ.get("IDE_LYRICS_DATA_DIR", "").strip()
    if override:
        return Path(override)
    if _IS_WINDOWS:
        return Path(os.environ.get("APPDATA") or Path.home()) / "IDELyricsStudio"
    if _IS_MACOS:
        return Path.home() / "Library" / "Application Support" / "IDELyricsStudio"
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg_cfg) if xdg_cfg else Path.home() / ".config") / "ide-lyrics-studio"


if IS_FROZEN:
    APP_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    REPO_ROOT = APP_DIR
    DATA_DIR = _user_data_dir()
else:
    APP_DIR = Path(__file__).resolve().parent
    REPO_ROOT = APP_DIR.parent
    _system_install = os.environ.get("IDE_LYRICS_SYSTEM_INSTALL") == "1" or not os.access(APP_DIR, os.W_OK)
    DATA_DIR = _user_data_dir() if _system_install else APP_DIR

CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "desktop-app.log"
STORAGE_DIR = DATA_DIR / "webview_storage"
RUNTIME_PORT_PATH = DATA_DIR / "runtime_port"
MACOS_LAUNCHAGENT_LABEL = "com.idelyrics.studio"
MACOS_LAUNCHAGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LAUNCHAGENT_LABEL}.plist"
LINUX_AUTOSTART_DESKTOP = (
    (Path(os.environ["XDG_CONFIG_HOME"]) if os.environ.get("XDG_CONFIG_HOME") else Path.home() / ".config")
    / "autostart"
    / "ide-lyrics-studio.desktop"
)

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

try:
    from wallpaper_host import clamp_overlay_opacity
except Exception:
    def clamp_overlay_opacity(value: Any, min_opacity: float = 0.25) -> float:
        if isinstance(value, bool) or value is None:
            return 1.0
        try:
            num = float(value)
        except Exception:
            return 1.0
        if num != num or num in (float("inf"), float("-inf")):
            return 1.0
        return round(max(float(min_opacity), min(1.0, num)), 4)

WINDOW_TITLE = "IDE Lyrics Studio"
APP_VERSION = "1.8.4.1"
GITHUB_REPO = "Mehdito-2005/ide-lyrics-studio"
VALID_MODES = ("app", "wallpaper", "overlay")
VALID_OVERLAY_SIDES = ("left", "center", "right")
VALID_AUTO_YIELD_MODES = ("auto", "maximized", "focused", "battery_only", "off")
DEFAULT_MODE = "app"
DEFAULT_WALLPAPER_MONITOR = "primary"
DEFAULT_OVERLAY_SIDE = "right"
DEFAULT_OVERLAY_CLICK_THROUGH = False
DEFAULT_OVERLAY_OPACITY = 1.0
OVERLAY_OPACITY_PRESETS = (1.0, 0.85, 0.70, 0.50, 0.35)
DEFAULT_AUTO_YIELD_MODE = "auto"
DEFAULT_APP_SIZE = (1280, 800)
DEFAULT_OVERLAY_SIZE = (460, 320)
PREFERRED_HTTP_PORT = 17385
AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_REG_VALUE = "IDELyricsStudio"

_LOGGER_NAME = "ide_lyrics_desktop"
_LOG_MAX_BYTES = 256 * 1024
_LOG_BACKUP_COUNT = 1
_PREV_SYS_EXCEPTHOOK = sys.excepthook
_PREV_THREADING_EXCEPTHOOK = getattr(threading, "excepthook", None)
_EXCEPTHOOKS_INSTALLED = False

_SINGLE_INSTANCE_MUTEX_NAME = "Local\\IDE_Lyrics_Studio_SingleInstance_v1"
_ERROR_ALREADY_EXISTS = 183


def _safe_call(fn: Callable[..., Any], *args: Any, default: Any = None, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except Exception:
        return default


def _get_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)


def setup_logging(log_path: Path = LOG_PATH) -> logging.Logger:
    """Attach a 256 KB rotating file handler and global crash hooks."""
    global _EXCEPTHOOKS_INSTALLED

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    resolved = str(Path(log_path).resolve())
    has_handler = any(
        isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "") == resolved
        for h in logger.handlers
    )
    if not has_handler:
        with suppress(Exception):
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                resolved,
                maxBytes=_LOG_MAX_BYTES,
                backupCount=_LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            logger.addHandler(handler)

    if not _EXCEPTHOOKS_INSTALLED:
        def _sys_hook(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
            if not issubclass(exc_type, KeyboardInterrupt):
                with suppress(Exception):
                    logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
            with suppress(Exception):
                _PREV_SYS_EXCEPTHOOK(exc_type, exc_value, exc_tb)

        sys.excepthook = _sys_hook

        if hasattr(threading, "excepthook"):
            def _thread_hook(args: Any) -> None:
                if args.exc_type is not SystemExit:
                    with suppress(Exception):
                        logger.critical(
                            "Uncaught exception in thread %s",
                            getattr(args.thread, "name", "unknown"),
                            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                        )
                if callable(_PREV_THREADING_EXCEPTHOOK):
                    with suppress(Exception):
                        _PREV_THREADING_EXCEPTHOOK(args)

            threading.excepthook = _thread_hook

        _EXCEPTHOOKS_INSTALLED = True

    return logger


def _normalize_geometry(val: Any) -> Optional[list[int]]:
    if not isinstance(val, (list, tuple)) or len(val) != 4:
        return None
    try:
        x, y, w, h = (int(v) for v in val)
    except Exception:
        return None
    return [x, y, w, h] if w > 0 and h > 0 else None


def _normalize_config(raw: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "mode": DEFAULT_MODE,
        "wallpaper_monitor": DEFAULT_WALLPAPER_MONITOR,
        "overlay_side": DEFAULT_OVERLAY_SIDE,
        "overlay_on_top": True,
        "overlay_click_through": DEFAULT_OVERLAY_CLICK_THROUGH,
        "overlay_opacity": DEFAULT_OVERLAY_OPACITY,
        "auto_yield_mode": DEFAULT_AUTO_YIELD_MODE,
        "wizard_completed": False,
    }
    if isinstance(raw, dict):
        cfg.update(raw)
    if cfg.get("mode") not in VALID_MODES:
        cfg["mode"] = DEFAULT_MODE
    mon = cfg.get("wallpaper_monitor")
    if not isinstance(mon, str) or not mon.strip():
        cfg["wallpaper_monitor"] = DEFAULT_WALLPAPER_MONITOR
    side = str(cfg.get("overlay_side") or DEFAULT_OVERLAY_SIDE).strip().lower()
    cfg["overlay_side"] = side if side in VALID_OVERLAY_SIDES else DEFAULT_OVERLAY_SIDE
    cfg["overlay_on_top"] = bool(cfg.get("overlay_on_top", True))
    cfg["overlay_click_through"] = bool(cfg.get("overlay_click_through", DEFAULT_OVERLAY_CLICK_THROUGH))
    cfg["overlay_opacity"] = clamp_overlay_opacity(cfg.get("overlay_opacity", DEFAULT_OVERLAY_OPACITY))
    yield_mode = str(cfg.get("auto_yield_mode") or DEFAULT_AUTO_YIELD_MODE).strip().lower()
    cfg["auto_yield_mode"] = yield_mode if yield_mode in VALID_AUTO_YIELD_MODES else DEFAULT_AUTO_YIELD_MODE
    cfg["wizard_completed"] = bool(cfg.get("wizard_completed", False))
    return cfg


_CONFIG_LOCK = threading.RLock()


def load_config(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    with _CONFIG_LOCK:
        with suppress(Exception):
            if config_path.exists():
                raw = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return _normalize_config(raw)
        return _normalize_config()


def save_config(data: dict[str, Any], config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    with _CONFIG_LOCK:
        cfg = load_config(config_path)
        cfg.update(data)
        cfg = _normalize_config(cfg)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_name: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=config_path.parent,
                suffix=".tmp",
                delete=False,
            ) as tmp_file:
                tmp_name = tmp_file.name
                tmp_file.write(json.dumps(cfg, indent=2) + "\n")
                tmp_file.flush()
                with suppress(OSError):
                    os.fsync(tmp_file.fileno())
            os.replace(tmp_name, config_path)
            tmp_name = None
        finally:
            if tmp_name is not None:
                with suppress(OSError):
                    os.unlink(tmp_name)
        return cfg


def get_saved_mode(config_path: Path = CONFIG_PATH) -> str:
    mode = str(load_config(config_path).get("mode", DEFAULT_MODE)).strip().lower()
    return mode if mode in VALID_MODES else DEFAULT_MODE


def save_mode(mode: str, config_path: Path = CONFIG_PATH) -> str:
    normalized = str(mode).strip().lower()
    if normalized not in VALID_MODES:
        raise ValueError(f"Invalid mode {mode!r}; expected one of {VALID_MODES}")
    save_config({"mode": normalized}, config_path)
    return normalized


def get_saved_wallpaper_monitor(config_path: Path = CONFIG_PATH) -> str:
    val = str(load_config(config_path).get("wallpaper_monitor", DEFAULT_WALLPAPER_MONITOR)).strip()
    return val or DEFAULT_WALLPAPER_MONITOR


def save_wallpaper_monitor(target: str, config_path: Path = CONFIG_PATH) -> str:
    normalized = str(target or DEFAULT_WALLPAPER_MONITOR).strip() or DEFAULT_WALLPAPER_MONITOR
    save_config({"wallpaper_monitor": normalized}, config_path)
    return normalized


def get_saved_overlay_side(config_path: Path = CONFIG_PATH) -> str:
    side = str(load_config(config_path).get("overlay_side", DEFAULT_OVERLAY_SIDE)).strip().lower()
    return side if side in VALID_OVERLAY_SIDES else DEFAULT_OVERLAY_SIDE


def save_overlay_side(side: str, config_path: Path = CONFIG_PATH) -> str:
    normalized = str(side or DEFAULT_OVERLAY_SIDE).strip().lower()
    if normalized not in VALID_OVERLAY_SIDES:
        normalized = DEFAULT_OVERLAY_SIDE
    save_config({"overlay_side": normalized}, config_path)
    return normalized


def get_saved_overlay_on_top(config_path: Path = CONFIG_PATH) -> bool:
    return bool(load_config(config_path).get("overlay_on_top", True))


def save_overlay_on_top(enabled: bool, config_path: Path = CONFIG_PATH) -> bool:
    val = bool(enabled)
    save_config({"overlay_on_top": val}, config_path)
    return val


def get_saved_overlay_click_through(config_path: Path = CONFIG_PATH) -> bool:
    return bool(load_config(config_path).get("overlay_click_through", DEFAULT_OVERLAY_CLICK_THROUGH))


def save_overlay_click_through(enabled: bool, config_path: Path = CONFIG_PATH) -> bool:
    val = bool(enabled)
    save_config({"overlay_click_through": val}, config_path)
    return val


def get_saved_overlay_opacity(config_path: Path = CONFIG_PATH) -> float:
    return clamp_overlay_opacity(load_config(config_path).get("overlay_opacity", DEFAULT_OVERLAY_OPACITY))


def save_overlay_opacity(opacity: float, config_path: Path = CONFIG_PATH) -> float:
    val = clamp_overlay_opacity(opacity)
    save_config({"overlay_opacity": val}, config_path)
    return val


def get_saved_auto_yield_mode(config_path: Path = CONFIG_PATH) -> str:
    mode = str(load_config(config_path).get("auto_yield_mode", DEFAULT_AUTO_YIELD_MODE)).strip().lower()
    return mode if mode in VALID_AUTO_YIELD_MODES else DEFAULT_AUTO_YIELD_MODE


def save_auto_yield_mode(mode: str, config_path: Path = CONFIG_PATH) -> str:
    normalized = str(mode).strip().lower()
    if normalized not in VALID_AUTO_YIELD_MODES:
        raise ValueError(f"Invalid auto_yield_mode {mode!r}; expected one of {VALID_AUTO_YIELD_MODES}")
    save_config({"auto_yield_mode": normalized}, config_path)
    return normalized


def get_saved_geometry(
    mode: str,
    config_path: Path = CONFIG_PATH,
) -> Optional[tuple[int, int, int, int]]:
    normalized = str(mode).strip().lower()
    if normalized not in ("app", "overlay"):
        return None
    geom = _normalize_geometry(load_config(config_path).get(f"{normalized}_geometry"))
    return (geom[0], geom[1], geom[2], geom[3]) if geom is not None else None


def save_geometry(
    mode: str,
    rect: tuple[int, int, int, int],
    config_path: Path = CONFIG_PATH,
) -> list[int]:
    normalized = str(mode).strip().lower()
    if normalized not in ("app", "overlay"):
        raise ValueError(f"Geometry persistence only supports 'app' or 'overlay', got {mode!r}")
    geom = _normalize_geometry(rect)
    if geom is None:
        raise ValueError(f"Invalid geometry rect: {rect!r}")
    save_config({f"{normalized}_geometry": geom}, config_path)
    return geom


def is_wizard_completed(config_path: Path = CONFIG_PATH) -> bool:
    return bool(load_config(config_path).get("wizard_completed", False))


def set_wizard_completed(completed: bool = True, config_path: Path = CONFIG_PATH) -> bool:
    cfg = save_config({"wizard_completed": bool(completed)}, config_path)
    return bool(cfg.get("wizard_completed", False))


def build_autostart_command() -> str:
    """Resolve the windowless launch command used for OS login startup."""
    launcher = os.environ.get("IDE_LYRICS_LAUNCHER", "").strip()
    if launcher:
        return f'"{launcher}" --tray'
    if IS_FROZEN:
        return f'"{Path(sys.executable).resolve()}" --tray'
    exe_path = Path(sys.executable)
    app_script = APP_DIR / "app.py"
    if _IS_WINDOWS:
        if exe_path.name.lower() == "pythonw.exe":
            return f'"{exe_path}" "{app_script}" --tray'
        sibling_pythonw = exe_path.with_name("pythonw.exe")
        if sibling_pythonw.exists():
            return f'"{sibling_pythonw}" "{app_script}" --tray'
        vbs_launcher = APP_DIR / "IDE-Lyrics-Tray.vbs"
        if vbs_launcher.exists():
            return f'wscript.exe //B "{vbs_launcher}"'
    return f'"{exe_path}" "{app_script}" --tray'


def _build_autostart_argv(command: Optional[str] = None) -> list[str]:
    if command:
        parts = _safe_call(shlex.split, command)
        if parts:
            return parts
    launcher = os.environ.get("IDE_LYRICS_LAUNCHER", "").strip()
    if launcher:
        return [launcher, "--tray"]
    if IS_FROZEN:
        return [str(Path(sys.executable).resolve()), "--tray"]
    return [str(Path(sys.executable).resolve()), str(APP_DIR / "app.py"), "--tray"]


def render_macos_launchagent_plist(
    program_args: list[str],
    label: str = MACOS_LAUNCHAGENT_LABEL,
) -> str:
    escaped_label = saxutils.escape(label)
    args_xml = "\n".join(
        f"        <string>{saxutils.escape(str(arg))}</string>" for arg in program_args
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{escaped_label}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{args_xml}\n"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "</dict>\n"
        "</plist>\n"
    )


def render_linux_autostart_desktop(exec_cmd: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        f"Name={WINDOW_TITLE}\n"
        "Comment=Live IDE Lyrics Desktop App & Wallpaper\n"
        f"Exec={exec_cmd}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Audio;\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def is_autostart_enabled(
    reg_value_name: str = AUTOSTART_REG_VALUE,
    reg_path: str = AUTOSTART_REG_PATH,
    plist_path: Path = MACOS_LAUNCHAGENT_PLIST,
    desktop_path: Path = LINUX_AUTOSTART_DESKTOP,
) -> bool:
    if _IS_WINDOWS:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_READ) as key:
                value, _reg_type = winreg.QueryValueEx(key, reg_value_name)
                return bool(isinstance(value, str) and value.strip())
        except (FileNotFoundError, OSError):
            return False
    if _IS_MACOS:
        return Path(plist_path).is_file()
    if _IS_LINUX:
        return Path(desktop_path).is_file()
    return False


def _sync_autostart_file(target: Path, enabled: bool, content_fn: Callable[[], str], label: str) -> bool:
    try:
        if enabled:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content_fn(), encoding="utf-8")
        elif target.exists():
            target.unlink()
        return True
    except OSError as exc:
        _get_logger().warning("Failed to update %s autostart: %s", label, exc)
        return False


def set_autostart(
    enabled: bool,
    reg_value_name: str = AUTOSTART_REG_VALUE,
    reg_path: str = AUTOSTART_REG_PATH,
    command: Optional[str] = None,
    plist_path: Path = MACOS_LAUNCHAGENT_PLIST,
    desktop_path: Path = LINUX_AUTOSTART_DESKTOP,
) -> bool:
    """Enable or disable login autostart (HKCU Run, macOS LaunchAgent, or Linux XDG)."""
    if _IS_WINDOWS:
        try:
            import winreg

            if enabled:
                cmd = (command or build_autostart_command()).strip()
                with winreg.CreateKeyEx(
                    winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_SET_VALUE
                ) as key:
                    winreg.SetValueEx(key, reg_value_name, 0, winreg.REG_SZ, cmd)
                return True

            with suppress(FileNotFoundError):
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_SET_VALUE
                ) as key:
                    winreg.DeleteValue(key, reg_value_name)
            return True
        except OSError as exc:
            _get_logger().warning("Failed to update Windows autostart: %s", exc)
            return False

    if _IS_MACOS:
        return _sync_autostart_file(
            Path(plist_path),
            enabled,
            lambda: render_macos_launchagent_plist(_build_autostart_argv(command)),
            "macOS LaunchAgent",
        )

    if _IS_LINUX:
        return _sync_autostart_file(
            Path(desktop_path),
            enabled,
            lambda: render_linux_autostart_desktop((command or build_autostart_command()).strip()),
            "Linux XDG",
        )

    return False


def _format_track_summary(snap: Optional[dict[str, Any]]) -> str:
    if not isinstance(snap, dict):
        return ""
    title = str(snap.get("title") or "").strip()
    artist = str(snap.get("artist") or "").strip()
    if title and artist:
        return f"{title} · {artist}"
    return title or artist


def format_tray_tooltip(snap: Optional[dict[str, Any]], max_len: int = 127) -> str:
    """Format the live tray tooltip within Win32's 127-char NOTIFYICONDATA limit."""
    limit = max(16, int(max_len))
    summary = _format_track_summary(snap)
    media_kind = str((snap or {}).get("mediaKind") or "song").strip().lower()
    if summary:
        prefix = "IDE Lyrics [Video] - " if media_kind == "video" else "IDE Lyrics - "
        text = f"{prefix}{summary}"
    else:
        text = WINDOW_TITLE
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _parse_version(raw: str) -> Optional[tuple[tuple[int, ...], str]]:
    text = str(raw or "").strip()
    if not text:
        return None
    if text[:1].lower() == "v":
        text = text[1:].strip()
    text = text.split("+", 1)[0].strip()
    if not text:
        return None
    core, _, prerelease = text.partition("-")
    parts = core.split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts), prerelease.strip().lower()


def is_newer_version(latest: str, current: str) -> bool:
    """Return True if latest represents a strictly newer version than current."""
    parsed_latest = _parse_version(latest)
    parsed_current = _parse_version(current)
    if parsed_latest is None or parsed_current is None:
        return False

    latest_nums, latest_pre = parsed_latest
    current_nums, current_pre = parsed_current

    width = max(len(latest_nums), len(current_nums))
    latest_pad = latest_nums + (0,) * (width - len(latest_nums))
    current_pad = current_nums + (0,) * (width - len(current_nums))
    if latest_pad != current_pad:
        return latest_pad > current_pad

    if bool(latest_pre) != bool(current_pre):
        return not latest_pre
    if latest_pre and current_pre:
        def _pre_key(tag: str) -> tuple[tuple[int, Any], ...]:
            return tuple(
                (0, int(tok)) if tok.isdigit() else (1, tok)
                for tok in re.split(r"[.\-_]+", tag)
                if tok
            )

        return _pre_key(latest_pre) > _pre_key(current_pre)
    return False


def _query_latest_release(
    repo: str = GITHUB_REPO,
    current_version: str = APP_VERSION,
    timeout: float = 4.0,
) -> dict[str, Any]:
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    fallback_url = f"https://github.com/{repo}/releases/latest"
    req = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"IDE-Lyrics-Studio/{current_version}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=max(0.5, float(timeout))) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {
                "ok": True,
                "current": current_version,
                "latest": "",
                "update_available": False,
                "url": fallback_url,
            }
        return {
            "ok": False,
            "current": current_version,
            "latest": "",
            "update_available": False,
            "url": fallback_url,
            "error": f"HTTP {exc.code}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "current": current_version,
            "latest": "",
            "update_available": False,
            "url": fallback_url,
            "error": str(exc),
        }

    if not isinstance(payload, dict):
        return {
            "ok": False,
            "current": current_version,
            "latest": "",
            "update_available": False,
            "url": fallback_url,
            "error": "Invalid release payload",
        }

    latest_tag = str(payload.get("tag_name") or "").strip()
    raw_url = str(payload.get("html_url") or "").strip()
    release_url = raw_url if raw_url.startswith("https://") else fallback_url
    return {
        "ok": True,
        "current": current_version,
        "latest": latest_tag,
        "update_available": is_newer_version(latest_tag, current_version),
        "url": release_url,
    }


def check_for_updates(
    on_complete: Optional[Callable[[dict[str, Any]], None]] = None,
    *,
    repo: str = GITHUB_REPO,
    current_version: str = APP_VERSION,
    timeout: float = 4.0,
    open_browser: bool = False,
) -> threading.Thread:
    """Fetch the latest GitHub release on a background thread without blocking."""
    def _worker() -> None:
        result = _query_latest_release(
            repo=repo,
            current_version=current_version,
            timeout=timeout,
        )
        logger = _get_logger()
        if result["ok"]:
            if result["update_available"]:
                logger.info(
                    "Update available: %s -> %s (%s)",
                    current_version,
                    result["latest"],
                    result["url"],
                )
                if open_browser and result["url"].startswith("https://"):
                    _safe_call(webbrowser.open, result["url"])
            else:
                logger.info(
                    "Up to date: current=%s latest=%s",
                    current_version,
                    result["latest"] or "none",
                )
        else:
            logger.warning("Update check failed: %s", result.get("error", "unknown error"))

        if callable(on_complete):
            _safe_call(on_complete, result)

    thread = threading.Thread(target=_worker, name="ide-lyrics-update-check", daemon=True)
    thread.start()
    return thread


def _make_handler(
    root_dir: Path,
    on_show_request: Optional[Callable[[], None]] = None,
) -> type[SimpleHTTPRequestHandler]:
    root_str = str(root_dir)

    class QuietLocalHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=root_str, **kwargs)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def handle_one_request(self) -> None:
            with suppress(ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                super().handle_one_request()

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] == "/_studio/show" and callable(on_show_request):
                threading.Thread(target=lambda: _safe_call(on_show_request), daemon=True).start()
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with suppress(OSError):
                    self.wfile.write(body)
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return QuietLocalHandler


class _DaemonThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, SO_REUSEADDR lets a second server bind the same port silently;
    # keep False on Windows so start_local_server falls back cleanly when busy.
    allow_reuse_address = not _IS_WINDOWS


def start_local_server(
    root_dir: Path = REPO_ROOT,
    on_show_request: Optional[Callable[[], None]] = None,
) -> tuple[ThreadingHTTPServer, int]:
    """Start a localhost HTTP server on a stable port (falling back to ephemeral).

    Keeping a stable port preserves WebView localStorage (themes, layout,
    custom backgrounds) across launches.
    """
    handler_cls = _make_handler(root_dir, on_show_request=on_show_request)
    try:
        server = _DaemonThreadingHTTPServer(("127.0.0.1", PREFERRED_HTTP_PORT), handler_cls)
    except OSError:
        server = _DaemonThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = int(server.server_address[1])
    threading.Thread(target=server.serve_forever, name="ide-lyrics-http", daemon=True).start()
    return server, port


def notify_running_instance_show(
    port_file: Path = RUNTIME_PORT_PATH,
    default_port: int = PREFERRED_HTTP_PORT,
    timeout: float = 0.8,
) -> bool:
    """Ask an already-running Studio instance to bring its window to the front."""
    ports: list[int] = []
    with suppress(Exception):
        if Path(port_file).is_file():
            raw = Path(port_file).read_text(encoding="utf-8").strip()
            if raw.isdigit() and int(raw) > 0:
                ports.append(int(raw))
    if default_port not in ports:
        ports.append(default_port)

    for port in ports:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/_studio/show",
                data=b"{}",
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=max(0.2, float(timeout))) as resp:
                if int(getattr(resp, "status", 0)) == 200:
                    return True
        except Exception:
            continue
    return False


def stop_local_server(server: Optional[ThreadingHTTPServer]) -> None:
    if server is None:
        return
    with suppress(Exception):
        server.shutdown()
    with suppress(Exception):
        server.server_close()


def show_mode_picker(current_mode: str = DEFAULT_MODE) -> str:
    try:
        import tkinter as tk
    except Exception:
        return current_mode

    chosen = current_mode

    try:
        root = tk.Tk()
        root.title("IDE Lyrics Studio - Launch Mode")
        root.configure(bg="#0f111a")
        root.resizable(False, False)

        width, height = 380, 275
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{width}x{height}+{(sw - width) // 2}+{(sh - height) // 2}")

        tk.Label(
            root,
            text=WINDOW_TITLE,
            font=("Segoe UI", 15, "bold"),
            fg="#e2e8f0",
            bg="#0f111a",
        ).pack(pady=(18, 4))

        tk.Label(
            root,
            text="Choose a launch mode (change anytime from tray or settings)",
            font=("Segoe UI", 9),
            fg="#94a3b8",
            bg="#0f111a",
        ).pack(pady=(0, 14))

        def pick(mode_val: str) -> None:
            nonlocal chosen
            chosen = mode_val
            root.destroy()

        options = (
            ("app", "App Window", "Resizable desktop window"),
            ("wallpaper", "Live Wallpaper", "Renders behind desktop icons"),
            ("overlay", "Mini Overlay", "Compact always-on-top widget"),
        )
        for mode_key, label_text, desc in options:
            is_curr = mode_key == current_mode
            tk.Button(
                root,
                text=f"{label_text} - {desc}",
                font=("Segoe UI", 9, "bold" if is_curr else "normal"),
                fg="#f8fafc",
                bg="#2b2548" if is_curr else "#1a1d2e",
                activebackground="#3b3166",
                activeforeground="#ffffff",
                relief="flat",
                cursor="hand2",
                anchor="w",
                padx=14,
                pady=9,
                command=lambda m=mode_key: pick(m),
            ).pack(fill="x", padx=22, pady=5)

        root.bind("<Escape>", lambda _e: root.destroy())
        root.attributes("-topmost", True)
        root.mainloop()
    except Exception:
        return current_mode

    return chosen


def create_tray_icon_image(size: int = 64) -> Any:
    """Render the tray/app icon in memory with Pillow."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = max(2, size // 16)
    draw.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=size // 4,
        fill=(18, 20, 34, 255),
        outline=(139, 92, 246, 255),
        width=max(2, size // 24),
    )

    cx, cy = size // 2, size // 2
    for scale, color in ((0.34, (124, 58, 237, 90)), (0.24, (34, 211, 238, 120))):
        r = int(size * scale)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)

    bar_x0 = int(size * 0.27)
    bar_h = max(3, size // 14)
    bars = (
        (0.30, 0.73, (196, 181, 253, 255)),
        (0.46, 0.64, (103, 232, 249, 255)),
        (0.62, 0.54, (244, 114, 182, 255)),
    )
    for y_frac, x1_frac, color in bars:
        y0 = int(size * y_frac)
        draw.rounded_rectangle([bar_x0, y0, int(size * x1_frac), y0 + bar_h], radius=2, fill=color)

    dot_r = max(3, size // 14)
    dot_cx, dot_cy = int(size * 0.69), int(size * 0.65)
    draw.ellipse(
        [dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r],
        fill=(56, 189, 248, 255),
    )
    return img


def _win32_kernel32() -> Any:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    return kernel32


class SingleInstanceLock:
    """Process-level single-instance guard (Win32 named mutex on Windows, flock on POSIX)."""

    def __init__(
        self,
        name: str = _SINGLE_INSTANCE_MUTEX_NAME,
        lock_file: Optional[Path] = None,
    ) -> None:
        self._name = name
        self._lock_file = lock_file or (DATA_DIR / "ide-lyrics.lock")
        self._handle: int = 0
        self._fp: Any = None
        self.acquired: bool = False

    def acquire(self) -> bool:
        if _IS_WINDOWS:
            try:
                kernel32 = _win32_kernel32()
                handle = kernel32.CreateMutexW(None, False, self._name)
                last_err = ctypes.get_last_error()
                if not handle:
                    self.acquired = True
                    return True
                if last_err == _ERROR_ALREADY_EXISTS:
                    kernel32.CloseHandle(handle)
                    self._handle = 0
                    self.acquired = False
                    return False
                self._handle = int(handle)
                self.acquired = True
                return True
            except Exception:
                self.acquired = True
                return True

        try:
            import fcntl

            self._lock_file.parent.mkdir(parents=True, exist_ok=True)
            fp = open(self._lock_file, "a+", encoding="utf-8")
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fp.close()
                self._fp = None
                self.acquired = False
                return False
            self._fp = fp
            self.acquired = True
            return True
        except Exception:
            self.acquired = True
            return True

    def release(self) -> None:
        if self._handle and _IS_WINDOWS:
            with suppress(Exception):
                _win32_kernel32().CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = 0
            self.acquired = False
        if self._fp is not None:
            with suppress(Exception):
                import fcntl

                fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
            with suppress(Exception):
                self._fp.close()
            self._fp = None
            self.acquired = False


def _resolve_window_hwnd(window: Any, title: str = WINDOW_TITLE) -> int:
    if window is None:
        return 0
    with suppress(Exception):
        native = getattr(window, "native", None)
        handle = getattr(native, "Handle", None) if native is not None else None
        if handle is not None:
            val = int(handle.ToInt64()) if hasattr(handle, "ToInt64") else int(handle)
            if val:
                return val

    with suppress(Exception):
        hwnd = getattr(window, "_hwnd", 0)
        if hwnd:
            return int(hwnd)

    if os.name == "nt":
        with suppress(Exception):
            hwnd = ctypes.windll.user32.FindWindowW(None, title)
            if hwnd:
                return int(hwnd)
    return 0


def _get_screen_size() -> tuple[int, int]:
    if _IS_WINDOWS:
        with suppress(Exception):
            user32 = ctypes.windll.user32
            return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
    with suppress(Exception):
        from wallpaper_host import WallpaperHost

        _, _, w, h = WallpaperHost.get_primary_screen_rect()
        return int(w), int(h)
    return 1920, 1080


def _has_linux_tray_host() -> bool:
    """Return True if the current Linux session has an active tray/indicator host."""
    if not _IS_LINUX:
        return True
    with suppress(Exception):
        from gi.repository import Gio, GLib  # type: ignore

        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        res = conn.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE,
            400,
            None,
        )
        if res and bool(res.unpack()[0]):
            return True
    with suppress(Exception):
        import pystray  # type: ignore

        mod_name = getattr(getattr(pystray, "Icon", None), "__module__", "")
        session_type = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
        if (mod_name.endswith("._gtk") or mod_name.endswith("._xorg")) and session_type != "wayland":
            return True
    return False


class DesktopAppController:
    """Coordinates the WebView window, wallpaper host, media bridge, local server, and tray."""

    def __init__(
        self,
        initial_mode: str = DEFAULT_MODE,
        config_path: Path = CONFIG_PATH,
        start_hidden: bool = True,
        open_wizard: bool = False,
    ) -> None:
        self.config_path = config_path
        self.mode: str = initial_mode if initial_mode in VALID_MODES else DEFAULT_MODE
        self.wallpaper_monitor: str = get_saved_wallpaper_monitor(config_path)
        self.overlay_side: str = get_saved_overlay_side(config_path)
        self.overlay_on_top: bool = get_saved_overlay_on_top(config_path)
        self.overlay_click_through: bool = get_saved_overlay_click_through(config_path)
        self.overlay_opacity: float = get_saved_overlay_opacity(config_path)
        self.auto_yield_mode: str = get_saved_auto_yield_mode(config_path)
        self._autostart_enabled: bool = is_autostart_enabled()
        self.start_hidden: bool = bool(start_hidden)
        self._open_wizard_on_load: bool = bool(open_wizard)
        self.window: Any = None
        self._url: str = ""
        self._hwnd: int = 0
        self._is_loaded = threading.Event()
        self._initial_load_handled = False
        self._shutdown_lock = threading.Lock()
        self._shutting_down = False
        self._allow_close = False
        self._window_visible = False
        self._mode_lock = threading.Lock()
        self._attached_to_wallpaper = False
        self._last_tooltip_key: Optional[tuple[str, str, str]] = None
        self._now_playing_label: str = "Nothing playing"
        self._update_label: str = "Check for Updates"
        self._update_check_lock = threading.Lock()
        self._checking_updates = False
        self._yield_lock = threading.Lock()
        self._yield_stop_event = threading.Event()
        self._yield_thread: Optional[threading.Thread] = None
        self._yield_state: dict[str, Any] = {
            "paused": False,
            "reason": "active",
            "mode": self.auto_yield_mode,
        }

        self.http_server: Optional[ThreadingHTTPServer] = None
        self.http_port: int = 0
        self.tray_icon: Any = None
        self._tray_available: bool = False
        self._on_signals_reinstall: Optional[Callable[[], None]] = None
        self.smtc_bridge: Any = None
        self.wallpaper_host: Any = None
        self._smtc_cls: Any = None
        self._integrations: Any = None

        self._init_backends()

    def _init_backends(self) -> None:
        with suppress(Exception):
            from wallpaper_host import WallpaperHost

            self.wallpaper_host = WallpaperHost()

        with suppress(Exception):
            from smtc_bridge import SMTCBridge

            self._smtc_cls = SMTCBridge

        with suppress(Exception):
            import integrations

            self._integrations = integrations

    def get_hwnd(self) -> int:
        hwnd = _resolve_window_hwnd(self.window, WINDOW_TITLE)
        if hwnd:
            self._hwnd = hwnd
        return self._hwnd

    def _get_native_form(self) -> Any:
        return getattr(self.window, "native", None) if self.window is not None else None

    def _get_host_handle(self) -> Any:
        if _IS_WINDOWS:
            return self.get_hwnd()
        return self._get_native_form()

    def get_mode(self) -> str:
        return self.mode

    def get_version(self) -> str:
        return APP_VERSION

    def _set_taskbar_visible(self, visible: bool) -> None:
        # Never toggle WinForms ShowInTaskbar post-handle-creation; that recreates
        # the HWND and tears down the hosted WebView2 control.
        handle = self._get_host_handle()
        if (
            self.wallpaper_host is not None
            and (handle or _IS_MACOS)
            and hasattr(self.wallpaper_host, "set_taskbar_visible")
        ):
            _safe_call(
                self.wallpaper_host.set_taskbar_visible,
                handle,
                visible,
                native_form=self._get_native_form(),
            )

    def on_before_show(self) -> None:
        """Runs synchronously before initial handle creation to prevent a taskbar/Dock flash."""
        if _IS_MACOS:
            if not self.start_hidden:
                self._set_taskbar_visible(self.mode == "app")
            else:
                self._set_taskbar_visible(False)
            self.start_tray()
            return
        with suppress(Exception):
            native = getattr(self.window, "native", None) if self.window is not None else None
            if native is not None and hasattr(native, "ShowInTaskbar"):
                if not getattr(native, "IsHandleCreated", False):
                    native.ShowInTaskbar = False
            elif _IS_LINUX:
                if native is not None and self.wallpaper_host is not None and hasattr(self.wallpaper_host, "prepare_linux_window"):
                    _safe_call(self.wallpaper_host.prepare_linux_window, native)
                if native is not None:
                    self._set_taskbar_visible(not self.start_hidden and self.mode == "app")
                self.start_tray()

    def _show_window(self) -> None:
        if self.window is None:
            return
        # pywebview 6.x GTK reads window.hidden on show; clear it before showing.
        if _IS_LINUX:
            with suppress(Exception):
                if hasattr(self.window, "hidden"):
                    self.window.hidden = False
        _safe_call(self.window.show)
        if _IS_LINUX and self.wallpaper_host is not None and hasattr(self.wallpaper_host, "present_linux_window"):
            handle = self._get_host_handle()
            if handle:
                _safe_call(self.wallpaper_host.present_linux_window, handle)
        self._set_taskbar_visible(bool(_IS_MACOS or _IS_LINUX) and self.mode == "app")
        self._window_visible = True

    def _detach_wallpaper_if_attached(self, handle: Any = None) -> None:
        if not self._attached_to_wallpaper:
            return
        target = self._get_host_handle() if handle is None else handle
        if self.wallpaper_host is not None and target:
            _safe_call(
                self.wallpaper_host.detach_from_wallpaper,
                target,
                native_form=self._get_native_form(),
            )
        self._attached_to_wallpaper = False

    def _set_on_top(self, handle: Any, enabled: bool) -> None:
        if self.wallpaper_host is not None and handle:
            _safe_call(
                self.wallpaper_host.set_always_on_top,
                handle,
                enabled,
                native_form=self._get_native_form(),
            )
        # Never mutate pywebview window.on_top on Windows directly: WinForms' Form.TopMost
        # setter isn't marshaled via Invoke() and deadlocks cross-thread calls.
        if not _IS_WINDOWS and self.window is not None:
            with suppress(Exception):
                self.window.on_top = enabled

    def _set_overlay_appearance(self, handle: Any, enabled: bool) -> None:
        if (
            self.wallpaper_host is not None
            and (handle or _IS_MACOS)
            and hasattr(self.wallpaper_host, "set_overlay_appearance")
        ):
            _safe_call(
                self.wallpaper_host.set_overlay_appearance,
                handle,
                enabled,
                native_form=self._get_native_form(),
            )

    def _set_window_z_layer(self, handle: Any, layer: str) -> None:
        if self.wallpaper_host is not None and handle and hasattr(self.wallpaper_host, "set_window_z_layer"):
            _safe_call(
                self.wallpaper_host.set_window_z_layer,
                handle,
                layer,
                native_form=self._get_native_form(),
            )
            return
        if layer == "top":
            self._set_on_top(handle, True)
        else:
            self._set_on_top(handle, False)
        if not _IS_WINDOWS and self.window is not None:
            with suppress(Exception):
                self.window.on_top = (layer == "top")

    def _set_click_through(self, handle: Any, enabled: bool) -> None:
        if (
            self.wallpaper_host is not None
            and handle
            and hasattr(self.wallpaper_host, "set_click_through")
        ):
            _safe_call(
                self.wallpaper_host.set_click_through,
                handle,
                bool(enabled),
                native_form=self._get_native_form(),
            )

    def _set_window_opacity(self, handle: Any, opacity: float) -> None:
        if (
            self.wallpaper_host is not None
            and handle
            and hasattr(self.wallpaper_host, "set_window_opacity")
        ):
            _safe_call(
                self.wallpaper_host.set_window_opacity,
                handle,
                clamp_overlay_opacity(opacity),
                native_form=self._get_native_form(),
            )

    def _apply_overlay_effects(self, handle: Any, active: bool) -> None:
        if active:
            self._set_click_through(handle, self.overlay_click_through)
            self._set_window_opacity(handle, self.overlay_opacity)
        else:
            self._set_click_through(handle, False)
            self._set_window_opacity(handle, 1.0)

    def _eval_js(self, js: str, *, background: bool = True) -> None:
        if not self._is_loaded.is_set() or self.window is None or self._shutting_down:
            return
        if background:
            def _run() -> None:
                if self.window is not None and not self._shutting_down:
                    _safe_call(self.window.evaluate_js, js)

            threading.Thread(target=_run, daemon=True).start()
        else:
            _safe_call(self.window.evaluate_js, js)

    def _push_current_snapshot(self) -> None:
        if self.smtc_bridge is None:
            return
        snap = _safe_call(self.smtc_bridge.get_snapshot)
        if isinstance(snap, dict):
            self.push_media_update(snap)

    def _can_hide_to_tray(self) -> bool:
        return bool(self.tray_icon is not None and self._tray_available)

    def show_existing_window(self) -> dict[str, Any]:
        target_mode = "app" if (self.mode == "wallpaper" and self._attached_to_wallpaper) else (
            self.mode if self.mode in ("app", "overlay") else "app"
        )
        return self.set_mode(target_mode, show_window=True)

    def hide_to_tray(self) -> dict[str, Any]:
        with self._mode_lock:
            self._capture_and_save_geometry()
            self._detach_wallpaper_if_attached()
            self._set_taskbar_visible(False)
            if self.window is not None:
                _safe_call(self.window.hide)
            self._window_visible = False
            self._refresh_tray_menu()
            return {"ok": True, "visible": False}

    def on_window_closing(self) -> Optional[bool]:
        """Hide back to the tray on window close unless an explicit quit was requested."""
        if self._allow_close or self._shutting_down or not self._can_hide_to_tray():
            self.shutdown(destroy_window=False)
            return None
        threading.Thread(target=self.hide_to_tray, daemon=True).start()
        return False

    def _capture_and_save_geometry(self, mode: Optional[str] = None) -> None:
        target_mode = str(mode or self.mode).strip().lower()
        if target_mode not in ("app", "overlay") or self._attached_to_wallpaper or not self._window_visible:
            return

        rect: Optional[tuple[int, int, int, int]] = None
        hwnd = self.get_hwnd()
        if _IS_WINDOWS and hwnd:
            with suppress(Exception):
                if ctypes.windll.user32.IsIconic(ctypes.c_void_p(hwnd)):
                    return
        if self.wallpaper_host is not None and hwnd and hasattr(self.wallpaper_host, "get_window_rect"):
            rect = _safe_call(self.wallpaper_host.get_window_rect, hwnd)

        if rect is None and self.window is not None:
            with suppress(Exception):
                w = int(getattr(self.window, "width", 0))
                h = int(getattr(self.window, "height", 0))
                if w > 0 and h > 0:
                    rect = (int(getattr(self.window, "x", 0)), int(getattr(self.window, "y", 0)), w, h)

        if rect is not None:
            _safe_call(save_geometry, target_mode, rect, self.config_path)

    def _resolve_wallpaper_rect(self) -> Optional[tuple[int, int, int, int]]:
        if self.wallpaper_host is not None and hasattr(self.wallpaper_host, "resolve_monitor_rect"):
            return _safe_call(self.wallpaper_host.resolve_monitor_rect, self.wallpaper_monitor)
        return None

    def get_monitors(self) -> list[dict[str, Any]]:
        if self.wallpaper_host is not None and hasattr(self.wallpaper_host, "list_monitors"):
            mons = _safe_call(self.wallpaper_host.list_monitors)
            if mons is not None:
                return list(mons)
        return []

    def set_wallpaper_monitor(self, target: str) -> dict[str, Any]:
        normalized = save_wallpaper_monitor(target, self.config_path)
        self.wallpaper_monitor = normalized
        rect = self._resolve_wallpaper_rect()

        with self._mode_lock:
            if self.mode == "wallpaper" and self._attached_to_wallpaper and self.wallpaper_host is not None:
                handle = self._get_host_handle()
                if handle:
                    try:
                        attached = bool(
                            self.wallpaper_host.attach_to_wallpaper(
                                handle,
                                monitor_rect=rect,
                                native_form=self._get_native_form(),
                            )
                        )
                        self._attached_to_wallpaper = attached
                        self._window_visible = attached
                    except Exception as exc:
                        _get_logger().warning("Failed to re-attach wallpaper on monitor %s: %s", normalized, exc)
                        return {"ok": False, "target": normalized, "error": str(exc)}

        self._refresh_tray_menu()
        return {
            "ok": True,
            "target": normalized,
            "rect": list(rect) if rect is not None else None,
        }

    def toggle_autostart(self) -> dict[str, Any]:
        ok = set_autostart(not self._autostart_enabled)
        self._autostart_enabled = is_autostart_enabled()
        self._refresh_tray_menu()
        return {"ok": ok, "enabled": self._autostart_enabled}

    def media_command(self, cmd: str) -> dict[str, Any]:
        normalized = str(cmd or "").strip().lower()
        ok = False
        if self.smtc_bridge is not None and hasattr(self.smtc_bridge, "send_command"):
            try:
                ok = bool(self.smtc_bridge.send_command(normalized))
            except Exception as exc:
                _get_logger().warning("media_command(%s) failed: %s", normalized, exc)
        return {"ok": ok, "command": normalized}

    def reload_ui(self) -> dict[str, Any]:
        if self.window is not None and self._url:
            try:
                self.window.load_url(self._url)
            except Exception as exc:
                _get_logger().warning("reload_ui load_url failed: %s", exc)

        with self._mode_lock:
            if self.mode == "wallpaper" and self.wallpaper_host is not None:
                handle = self._get_host_handle()
                if handle:
                    try:
                        attached = bool(
                            self.wallpaper_host.attach_to_wallpaper(
                                handle,
                                monitor_rect=self._resolve_wallpaper_rect(),
                                native_form=self._get_native_form(),
                            )
                        )
                        self._attached_to_wallpaper = attached
                        self._window_visible = attached
                    except Exception as exc:
                        _get_logger().warning("reload_ui re-attach failed: %s", exc)

        self._push_current_snapshot()
        self._refresh_tray_menu()
        return {"ok": True}

    def open_logs_folder(self) -> dict[str, Any]:
        target_dir = DATA_DIR
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            if _IS_WINDOWS and hasattr(os, "startfile"):
                os.startfile(str(target_dir))  # type: ignore[attr-defined]
                return {"ok": True, "path": str(target_dir)}
            if _IS_MACOS:
                subprocess.Popen(["open", str(target_dir)])
                return {"ok": True, "path": str(target_dir)}
            if _IS_LINUX:
                subprocess.Popen(["xdg-open", str(target_dir)])
                return {"ok": True, "path": str(target_dir)}
        except Exception as exc:
            _get_logger().warning("open_logs_folder failed: %s", exc)
            return {"ok": False, "error": str(exc)}
        return {"ok": False, "path": str(target_dir)}

    def set_mode(self, mode: str, show_window: bool = True) -> dict[str, Any]:
        normalized = str(mode).strip().lower()
        if normalized not in VALID_MODES:
            return {"ok": False, "mode": self.mode, "error": f"Invalid mode: {mode}"}

        with self._mode_lock:
            if self.mode in ("app", "overlay") and normalized != self.mode:
                self._capture_and_save_geometry(self.mode)

            handle = self._get_host_handle()
            attached = self._attached_to_wallpaper

            try:
                self._set_taskbar_visible(False)

                if normalized == "wallpaper":
                    handle = self._get_host_handle()
                    if self.wallpaper_host is not None and handle:
                        if self._window_visible and self.window is not None:
                            _safe_call(self.window.hide)
                        self._apply_overlay_effects(handle, False)
                        self._set_overlay_appearance(handle, False)
                        rect = self._resolve_wallpaper_rect()
                        attached = bool(
                            self.wallpaper_host.attach_to_wallpaper(
                                handle,
                                monitor_rect=rect,
                                native_form=self._get_native_form(),
                            )
                        )
                        if attached:
                            if _IS_WINDOWS:
                                _safe_call(
                                    self.wallpaper_host.set_always_on_top,
                                    handle,
                                    False,
                                    native_form=self._get_native_form(),
                                )
                            if _IS_LINUX and self.window is not None and hasattr(self.window, "hidden"):
                                with suppress(Exception):
                                    self.window.hidden = False
                        elif self.window is not None:
                            _safe_call(self.window.hide)
                        self._attached_to_wallpaper = attached
                        self._window_visible = attached
                    else:
                        if self.window is not None:
                            _safe_call(self.window.hide)
                        attached = False
                        self._attached_to_wallpaper = False
                        self._window_visible = False
                else:
                    self._detach_wallpaper_if_attached(handle)
                    attached = False
                    is_overlay = normalized == "overlay"
                    self.mode = normalized
                    self._set_overlay_appearance(handle, is_overlay)
                    if not is_overlay:
                        self._apply_overlay_effects(handle, False)
                        self._set_window_z_layer(handle, "normal")
                    self._set_taskbar_visible(bool(_IS_MACOS or _IS_LINUX) and not is_overlay)
                    self._apply_window_geometry(normalized)
                    if show_window:
                        self._show_window()
                    if is_overlay:
                        target_handle = self._get_host_handle() or handle
                        self._set_window_z_layer(
                            target_handle,
                            "top" if self.overlay_on_top else "bottom",
                        )
                        self._apply_overlay_effects(target_handle, True)
            except Exception as exc:
                _get_logger().warning("set_mode(%s) failed: %s", normalized, exc)
                return {"ok": False, "mode": self.mode, "error": str(exc)}

            self.mode = normalized
            _safe_call(save_mode, normalized, self.config_path)
            self._refresh_tray_menu()
            self._notify_js_mode(normalized)
            self._notify_js_overlay_prefs()
            return {"ok": True, "mode": self.mode, "attached": attached}

    def _clamp_geometry(self, rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        if self.wallpaper_host is not None and hasattr(self.wallpaper_host, "clamp_rect_to_virtual_screen"):
            clamped = _safe_call(self.wallpaper_host.clamp_rect_to_virtual_screen, rect)
            if clamped is not None:
                return clamped
        return rect

    def _compute_overlay_side_rect(
        self,
        side: str,
        size: Optional[tuple[int, int]] = None,
    ) -> tuple[int, int, int, int]:
        w, h = size if size is not None else DEFAULT_OVERLAY_SIZE
        w, h = max(280, int(w)), max(140, int(h))
        sw, sh = _get_screen_size()
        margin = 24
        norm_side = side if side in VALID_OVERLAY_SIDES else DEFAULT_OVERLAY_SIDE
        if norm_side == "left":
            x = margin
            y = max(24, (sh - h) // 2)
        elif norm_side == "center":
            x = max(0, (sw - w) // 2)
            y = max(24, sh - h - 72)
        else:
            x = max(0, sw - w - margin)
            y = max(24, (sh - h) // 2)
        return self._clamp_geometry((x, y, w, h))

    def _apply_rect(self, rect: tuple[int, int, int, int]) -> None:
        if self.window is None:
            return
        x, y, w, h = (int(v) for v in rect)
        if _IS_WINDOWS and self.wallpaper_host is not None and hasattr(self.wallpaper_host, "set_window_rect"):
            hwnd = self.get_hwnd()
            if hwnd and self.wallpaper_host.set_window_rect(
                hwnd,
                (x, y, w, h),
                native_form=self._get_native_form(),
            ):
                return
        _safe_call(self.window.resize, w, h)
        _safe_call(self.window.move, x, y)

    def _apply_window_geometry(self, mode: str) -> None:
        if self.window is None:
            return
        saved = get_saved_geometry(mode, self.config_path)
        if saved is not None:
            rect = self._clamp_geometry(saved)
        elif mode == "overlay":
            rect = self._compute_overlay_side_rect(self.overlay_side)
        else:
            w, h = DEFAULT_APP_SIZE
            sw, sh = _get_screen_size()
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 2)
            rect = (x, y, w, h)
        self._apply_rect(rect)

    def snap_overlay_side(self, side: str) -> dict[str, Any]:
        normalized = save_overlay_side(side, self.config_path)
        self.overlay_side = normalized

        with self._mode_lock:
            if self.mode == "overlay" and not self._attached_to_wallpaper:
                self._capture_and_save_geometry("overlay")
            saved = get_saved_geometry("overlay", self.config_path)
            size = (saved[2], saved[3]) if saved is not None else DEFAULT_OVERLAY_SIZE
            rect = self._compute_overlay_side_rect(normalized, size=size)
            _safe_call(save_geometry, "overlay", rect, self.config_path)
            if self.mode == "overlay" and not self._attached_to_wallpaper:
                self._apply_rect(rect)

        self._refresh_tray_menu()
        self._eval_js(
            f"window.onOverlaySideChanged && window.onOverlaySideChanged({json.dumps(normalized)})",
            background=True,
        )
        return {"ok": True, "side": normalized, "rect": list(rect)}

    def set_overlay_on_top(self, enabled: bool) -> dict[str, Any]:
        val = save_overlay_on_top(enabled, self.config_path)
        self.overlay_on_top = val
        with self._mode_lock:
            if self.mode == "overlay" and not self._attached_to_wallpaper:
                handle = self._get_host_handle()
                if handle:
                    self._set_window_z_layer(handle, "top" if val else "bottom")
        self._refresh_tray_menu()
        self._notify_js_overlay_prefs()
        return {"ok": True, "on_top": val}

    def toggle_overlay_on_top(self) -> dict[str, Any]:
        return self.set_overlay_on_top(not self.overlay_on_top)

    def set_overlay_click_through(self, enabled: bool) -> dict[str, Any]:
        val = save_overlay_click_through(enabled, self.config_path)
        self.overlay_click_through = val
        with self._mode_lock:
            if self.mode == "overlay" and not self._attached_to_wallpaper:
                handle = self._get_host_handle()
                if handle:
                    self._set_click_through(handle, val)
        self._refresh_tray_menu()
        self._notify_js_overlay_prefs()
        return {"ok": True, "click_through": val}

    def toggle_overlay_click_through(self) -> dict[str, Any]:
        return self.set_overlay_click_through(not self.overlay_click_through)

    def set_overlay_opacity(self, opacity: float) -> dict[str, Any]:
        val = save_overlay_opacity(opacity, self.config_path)
        self.overlay_opacity = val
        with self._mode_lock:
            if self.mode == "overlay" and not self._attached_to_wallpaper:
                handle = self._get_host_handle()
                if handle:
                    self._set_window_opacity(handle, val)
        self._refresh_tray_menu()
        self._notify_js_overlay_prefs()
        return {"ok": True, "opacity": val}

    def start_window_drag(self) -> dict[str, Any]:
        if self.wallpaper_host is not None and hasattr(self.wallpaper_host, "start_window_drag"):
            handle = self._get_host_handle()
            ok = bool(
                self.wallpaper_host.start_window_drag(
                    handle,
                    native_form=self._get_native_form(),
                )
            )
            return {"ok": ok}
        return {"ok": False}

    def _notify_js_mode(self, mode: str) -> None:
        self._eval_js(
            f"window.onDesktopModeChanged && window.onDesktopModeChanged({json.dumps(mode)})",
            background=True,
        )

    def _notify_js_overlay_prefs(self) -> None:
        side_js = json.dumps(self.overlay_side)
        top_js = "true" if self.overlay_on_top else "false"
        ghost_js = "true" if self.overlay_click_through else "false"
        opacity_js = json.dumps(self.overlay_opacity)
        self._eval_js(
            f"(function(){{"
            f"window.onOverlaySideChanged&&window.onOverlaySideChanged({side_js});"
            f"window.onOverlayLayerChanged&&window.onOverlayLayerChanged({top_js});"
            f"window.onOverlayGhostChanged&&window.onOverlayGhostChanged({ghost_js});"
            f"window.onOverlayOpacityChanged&&window.onOverlayOpacityChanged({opacity_js});"
            f"}})()",
            background=True,
        )

    def _get_target_monitor_screen_rect(self) -> Optional[tuple[int, int, int, int]]:
        if self.mode != "wallpaper":
            return None
        target = str(self.wallpaper_monitor or DEFAULT_WALLPAPER_MONITOR).strip().lower()
        if target == "all":
            return None
        monitors = self.get_monitors()
        if not monitors:
            return None
        if target != "primary":
            for mon in monitors:
                if target in (str(mon.get("id", "")).lower(), str(mon.get("name", "")).lower()):
                    rect = mon.get("rect")
                    if rect and len(rect) == 4:
                        return tuple(int(v) for v in rect)  # type: ignore[return-value]
            if target.isdigit():
                idx = int(target)
                if 0 <= idx < len(monitors):
                    rect = monitors[idx].get("rect")
                    if rect and len(rect) == 4:
                        return tuple(int(v) for v in rect)  # type: ignore[return-value]
        chosen = next((m for m in monitors if m.get("is_primary")), monitors[0])
        rect = chosen.get("rect")
        return tuple(int(v) for v in rect) if (rect and len(rect) == 4) else None  # type: ignore[return-value]

    def _evaluate_current_yield(self) -> dict[str, Any]:
        policy = self.auto_yield_mode
        if policy == "off" or self.wallpaper_host is None:
            return {"paused": False, "reason": "active", "mode": policy}

        os_state = {
            "is_locked": False,
            "is_fullscreen": False,
            "is_maximized": False,
            "is_other_app_focused": False,
            "is_battery_saver": False,
        }
        if hasattr(self.wallpaper_host, "detect_desktop_yield_state"):
            own_hwnd = self.get_hwnd() if self.window is not None else 0
            target_rect = self._get_target_monitor_screen_rect()
            detected = _safe_call(
                self.wallpaper_host.detect_desktop_yield_state,
                own_hwnd=own_hwnd,
                target_monitor_rect=target_rect,
            )
            if isinstance(detected, dict):
                os_state.update(detected)

        keep_pinned_overlay = bool(self.mode == "overlay" and self.overlay_on_top)
        if hasattr(self.wallpaper_host, "evaluate_yield_state"):
            evaluated = _safe_call(
                self.wallpaper_host.evaluate_yield_state,
                policy,
                is_locked=bool(os_state.get("is_locked", False)),
                is_fullscreen=False if keep_pinned_overlay else bool(os_state.get("is_fullscreen", False)),
                is_maximized=False if keep_pinned_overlay else bool(os_state.get("is_maximized", False)),
                is_other_app_focused=False if keep_pinned_overlay else bool(os_state.get("is_other_app_focused", False)),
                is_battery_saver=bool(os_state.get("is_battery_saver", False)),
            )
            if isinstance(evaluated, dict):
                return {
                    "paused": bool(evaluated.get("paused", False)),
                    "reason": str(evaluated.get("reason") or "active"),
                    "mode": policy,
                }
        return {"paused": False, "reason": "active", "mode": policy}

    def _apply_yield_state(self, state: dict[str, Any], *, force_notify: bool = False) -> None:
        new_paused = bool(state.get("paused", False))
        new_reason = str(state.get("reason") or "active")
        new_mode = str(state.get("mode") or self.auto_yield_mode)
        with self._yield_lock:
            prev = dict(self._yield_state)
            changed = (
                force_notify
                or prev.get("paused") != new_paused
                or prev.get("reason") != new_reason
                or prev.get("mode") != new_mode
            )
            self._yield_state = {
                "paused": new_paused,
                "reason": new_reason,
                "mode": new_mode,
            }

        if changed:
            if self.smtc_bridge is not None and hasattr(self.smtc_bridge, "set_poll_interval"):
                _safe_call(self.smtc_bridge.set_poll_interval, 1.5 if new_paused else 0.15)
            payload = json.dumps({"paused": new_paused, "reason": new_reason, "mode": new_mode})
            self._eval_js(
                f"window.onDesktopPowerStateChanged && window.onDesktopPowerStateChanged({payload})",
                background=True,
            )

    def get_power_yield_state(self) -> dict[str, Any]:
        with self._yield_lock:
            state = dict(self._yield_state)
        state["mode"] = self.auto_yield_mode
        return state

    def set_auto_yield_mode(self, mode: str) -> dict[str, Any]:
        normalized = str(mode or "").strip().lower()
        if normalized not in VALID_AUTO_YIELD_MODES:
            return {
                "ok": False,
                "mode": self.auto_yield_mode,
                "error": f"Invalid auto-yield mode: {mode}",
            }
        saved = save_auto_yield_mode(normalized, self.config_path)
        self.auto_yield_mode = saved
        state = self._evaluate_current_yield()
        self._apply_yield_state(state, force_notify=True)
        self._refresh_tray_menu()
        return {
            "ok": True,
            "mode": saved,
            "paused": bool(state["paused"]),
            "reason": str(state["reason"]),
        }

    def start_yield_watcher(self) -> None:
        with self._yield_lock:
            if self._yield_thread is not None and self._yield_thread.is_alive():
                return
            self._yield_stop_event.clear()
            self._yield_thread = threading.Thread(
                target=self._yield_watch_loop,
                name="ide-lyrics-yield-watcher",
                daemon=True,
            )
            self._yield_thread.start()

    def _yield_watch_loop(self) -> None:
        while not self._yield_stop_event.is_set() and not self._shutting_down:
            with suppress(Exception):
                state = self._evaluate_current_yield()
                self._apply_yield_state(state)
            if self._yield_stop_event.wait(1.5):
                break

    def open_settings_in_app_mode(self) -> None:
        prev_mode = self.mode if self.mode in VALID_MODES else "app"
        self.set_mode("app", show_window=True)
        self._eval_js(
            f"window.openSettingsDrawer && window.openSettingsDrawer({json.dumps(prev_mode)})"
        )

    def open_setup_wizard(self) -> dict[str, Any]:
        self.set_mode("app", show_window=True)
        self._eval_js("window.openSetupWizard && window.openSetupWizard()", background=True)
        return {"ok": True}

    def is_wizard_needed(self) -> bool:
        return bool(self._open_wizard_on_load or not is_wizard_completed(self.config_path))

    def mark_wizard_completed(self) -> dict[str, Any]:
        self._open_wizard_on_load = False
        set_wizard_completed(True, self.config_path)
        return {"ok": True, "wizard_completed": True}

    def detect_integrations(self) -> dict[str, Any]:
        if self._integrations is None:
            return {
                "platform": sys.platform,
                "cider": {"detected": False, "installed": False},
                "spicetify": {"detected": False, "cli_found": False, "installed": False},
            }
        return self._integrations.detect_integrations(REPO_ROOT)

    def get_setup_state(self) -> dict[str, Any]:
        return {
            "version": APP_VERSION,
            "mode": self.mode,
            "autostart": self._autostart_enabled,
            "wizard_completed": is_wizard_completed(self.config_path),
            "wizard_needed": self.is_wizard_needed(),
            "integrations": self.detect_integrations(),
        }

    def install_cider_plugin(self) -> dict[str, Any]:
        if self._integrations is None:
            return {"ok": False, "error": "Integrations module unavailable."}
        return self._integrations.install_cider_plugin(REPO_ROOT)

    def uninstall_cider_plugin(self) -> dict[str, Any]:
        if self._integrations is None:
            return {"ok": False, "error": "Integrations module unavailable."}
        return self._integrations.uninstall_cider_plugin()

    def install_spicetify_extension(self) -> dict[str, Any]:
        if self._integrations is None:
            return {"ok": False, "error": "Integrations module unavailable."}
        return self._integrations.install_spicetify_extension(REPO_ROOT)

    def uninstall_spicetify_extension(self) -> dict[str, Any]:
        if self._integrations is None:
            return {"ok": False, "error": "Integrations module unavailable."}
        return self._integrations.uninstall_spicetify_extension()

    def apply_wizard_setup(self, options: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        opts = options if isinstance(options, dict) else {}
        results: dict[str, Any] = {"ok": True, "steps": {}}

        if "autostart" in opts:
            want_auto = bool(opts.get("autostart"))
            ok_auto = set_autostart(want_auto)
            self._autostart_enabled = is_autostart_enabled()
            self._refresh_tray_menu()
            results["steps"]["autostart"] = {"ok": ok_auto, "enabled": self._autostart_enabled}

        if opts.get("install_cider"):
            res_cider = self.install_cider_plugin()
            results["steps"]["cider"] = res_cider
            if not res_cider.get("ok"):
                results["ok"] = False

        if opts.get("install_spicetify"):
            res_sp = self.install_spicetify_extension()
            results["steps"]["spicetify"] = res_sp
            if not res_sp.get("ok"):
                results["ok"] = False

        chosen_mode = str(opts.get("mode") or "").strip().lower()
        if chosen_mode in VALID_MODES:
            _safe_call(save_mode, chosen_mode, self.config_path)
            results["mode"] = chosen_mode
            if opts.get("apply_mode_now", False):
                results["steps"]["mode"] = self.set_mode(chosen_mode, show_window=True)

        self.mark_wizard_completed()
        results["state"] = self.get_setup_state()
        return results

    def _run_on_tray_thread(self, fn: Callable[[], Any]) -> None:
        if _IS_MACOS:
            if threading.current_thread() is threading.main_thread():
                _safe_call(fn)
                return
            try:
                from PyObjCTools import AppHelper  # type: ignore

                AppHelper.callAfter(fn)
                return
            except Exception:
                pass
        elif _IS_LINUX:
            if threading.current_thread() is threading.main_thread():
                _safe_call(fn)
                return
            try:
                from gi.repository import GLib  # type: ignore

                GLib.idle_add(lambda: (_safe_call(fn), False)[1])
                return
            except Exception:
                pass
        _safe_call(fn)

    def toggle_fullscreen(self) -> dict[str, Any]:
        if self.window is None:
            return {"ok": False, "error": "Window not ready"}
        try:
            self.window.toggle_fullscreen()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def toggle_clean_feed(self) -> None:
        js = (
            "(function(){"
            "if (typeof setCleanFeed === 'function' && typeof settings !== 'undefined') {"
            "  setCleanFeed(!settings.cleanFeed);"
            "} else {"
            "  var onBtn = document.querySelector('[data-cleanfeed=\"on\"]');"
            "  var offBtn = document.querySelector('[data-cleanfeed=\"off\"]');"
            "  if (onBtn && offBtn) {"
            "    (onBtn.classList.contains('is-active') ? offBtn : onBtn).click();"
            "  }"
            "}"
            "})();"
        )
        self._eval_js(js)

    def _update_tray_now_playing(self, snap: dict[str, Any]) -> None:
        title = str(snap.get("title") or "").strip()
        artist = str(snap.get("artist") or "").strip()
        status = str(snap.get("status") or "stopped").strip().lower()
        media_kind = str(snap.get("mediaKind") or "song").strip().lower()
        key = (title, artist, status, media_kind)
        if key == self._last_tooltip_key:
            return
        self._last_tooltip_key = key

        summary = _format_track_summary(snap)
        label = (f"[Video] {summary}" if (summary and media_kind == "video") else summary) or "Nothing playing"
        if len(label) > 64:
            label = label[:63].rstrip() + "…"
        self._now_playing_label = label

        if self.tray_icon is not None:
            tip = format_tray_tooltip(snap)
            icon_ref = self.tray_icon
            self._run_on_tray_thread(lambda: setattr(icon_ref, "title", tip))
            self._refresh_tray_menu()

    def push_media_update(self, snap: dict[str, Any]) -> None:
        if self._shutting_down or not isinstance(snap, dict):
            return
        self._update_tray_now_playing(snap)
        payload = json.dumps(snap, ensure_ascii=False)
        self._eval_js(f"window.onDesktopMediaUpdate && window.onDesktopMediaUpdate({payload})")

    def start_smtc(self) -> None:
        if self._smtc_cls is None:
            return
        try:
            init_params = [
                p for name, p in inspect.signature(self._smtc_cls.__init__).parameters.items()
                if name != "self"
            ]
            if init_params and init_params[0].name in ("on_update", "callback"):
                self.smtc_bridge = self._smtc_cls(self.push_media_update)
            else:
                self.smtc_bridge = self._smtc_cls()

            start_fn = getattr(self.smtc_bridge, "start", None)
            if callable(start_fn):
                start_params = list(inspect.signature(start_fn).parameters.values())
                if start_params and start_params[0].name in ("callback", "on_update"):
                    start_fn(self.push_media_update)
                else:
                    start_fn()
        except Exception:
            self.smtc_bridge = None

    def check_for_updates(self, open_browser: bool = True) -> dict[str, Any]:
        with self._update_check_lock:
            if self._checking_updates:
                return {"ok": True, "status": "checking", "version": APP_VERSION}
            self._checking_updates = True

        self._update_label = "Checking for Updates…"
        self._refresh_tray_menu()

        def _on_complete(result: dict[str, Any]) -> None:
            if result.get("ok"):
                if result.get("update_available"):
                    latest = str(result.get("latest") or "").lstrip("vV")
                    self._update_label = f"Update Available (v{latest})" if latest else "Update Available"
                else:
                    self._update_label = f"Up to Date (v{APP_VERSION})"
            else:
                self._update_label = "Update Check Failed (Retry)"
            with self._update_check_lock:
                self._checking_updates = False
            self._refresh_tray_menu()

        check_for_updates(on_complete=_on_complete, open_browser=open_browser)
        return {"ok": True, "status": "checking", "version": APP_VERSION}

    def build_tray_menu(self) -> Any:
        from pystray import Menu, MenuItem

        # pystray inspects callback arity: actions take (icon, item); checked/text take (item).
        def _action(fn: Callable[[], Any]) -> Callable[[Any, Any], None]:
            def _cb(_icon: Any, _item: Any) -> None:
                threading.Thread(target=fn, daemon=True).start()

            return _cb

        def _monitor_checked(target: str) -> Callable[[Any], bool]:
            target_low = target.lower()

            def _checked(_item: Any) -> bool:
                return self.wallpaper_monitor.lower() == target_low

            return _checked

        def _monitor_item(label: str, target: str) -> Any:
            return MenuItem(
                label,
                _action(lambda t=target: self.set_wallpaper_monitor(t)),
                checked=_monitor_checked(target),
                radio=True,
            )

        def _build_monitor_items() -> list[Any]:
            items: list[Any] = [_monitor_item("Primary Display", "primary")]
            for mon in self.get_monitors():
                mon_id = str(mon.get("id", "0"))
                items.append(_monitor_item(str(mon.get("label") or f"Monitor {mon_id}"), mon_id))
            items.append(Menu.SEPARATOR)
            items.append(_monitor_item("Span All Displays", "all"))
            return items

        playback_menu = Menu(
            MenuItem(lambda _item: self._now_playing_label, None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("Play / Pause", _action(lambda: self.media_command("play_pause"))),
            MenuItem("Next", _action(lambda: self.media_command("next"))),
            MenuItem("Previous", _action(lambda: self.media_command("previous"))),
        )

        return Menu(
            MenuItem("Open Settings", _action(self.open_settings_in_app_mode), default=True),
            MenuItem("Setup Wizard & Integrations...", _action(self.open_setup_wizard)),
            MenuItem("Hide Window", _action(self.hide_to_tray)),
            Menu.SEPARATOR,
            MenuItem("Playback", playback_menu),
            Menu.SEPARATOR,
            MenuItem(
                "App Window",
                _action(lambda: self.set_mode("app", show_window=True)),
                checked=lambda _item: self._window_visible and self.mode == "app",
                radio=True,
            ),
            MenuItem(
                "Live Wallpaper",
                _action(lambda: self.set_mode("wallpaper", show_window=True)),
                checked=lambda _item: self._attached_to_wallpaper and self.mode == "wallpaper",
                radio=True,
            ),
            MenuItem(
                "Mini Overlay",
                _action(lambda: self.set_mode("overlay", show_window=True)),
                checked=lambda _item: self._window_visible and self.mode == "overlay",
                radio=True,
            ),
            MenuItem(
                "Mini Overlay Side",
                Menu(
                    MenuItem(
                        "Dock Left",
                        _action(lambda: self.snap_overlay_side("left")),
                        checked=lambda _item: self.overlay_side == "left",
                        radio=True,
                    ),
                    MenuItem(
                        "Dock Bottom-Center",
                        _action(lambda: self.snap_overlay_side("center")),
                        checked=lambda _item: self.overlay_side == "center",
                        radio=True,
                    ),
                    MenuItem(
                        "Dock Right",
                        _action(lambda: self.snap_overlay_side("right")),
                        checked=lambda _item: self.overlay_side == "right",
                        radio=True,
                    ),
                    Menu.SEPARATOR,
                    MenuItem(
                        "Pin Above Windows",
                        _action(lambda: self.set_overlay_on_top(True)),
                        checked=lambda _item: bool(self.overlay_on_top),
                        radio=True,
                    ),
                    MenuItem(
                        "Over Wallpaper Only",
                        _action(lambda: self.set_overlay_on_top(False)),
                        checked=lambda _item: not self.overlay_on_top,
                        radio=True,
                    ),
                    Menu.SEPARATOR,
                    MenuItem(
                        "Click-Through (Ghost Mode)",
                        _action(self.toggle_overlay_click_through),
                        checked=lambda _item: bool(self.overlay_click_through),
                    ),
                    MenuItem(
                        "Overlay Opacity",
                        Menu(
                            *(
                                MenuItem(
                                    f"{int(round(op * 100))}%",
                                    _action(lambda val=op: self.set_overlay_opacity(val)),
                                    checked=lambda _item, val=op: abs(float(self.overlay_opacity) - val) < 0.03,
                                    radio=True,
                                )
                                for op in OVERLAY_OPACITY_PRESETS
                            )
                        ),
                    ),
                ),
            ),
            MenuItem("Wallpaper Display", Menu(_build_monitor_items)),
            MenuItem(
                "Auto-Pause When Hidden",
                Menu(
                    MenuItem(
                        "Fullscreen & Lock (Smart Auto)",
                        _action(lambda: self.set_auto_yield_mode("auto")),
                        checked=lambda _item: self.auto_yield_mode == "auto",
                        radio=True,
                    ),
                    MenuItem(
                        "When Another App Is Maximized",
                        _action(lambda: self.set_auto_yield_mode("maximized")),
                        checked=lambda _item: self.auto_yield_mode == "maximized",
                        radio=True,
                    ),
                    MenuItem(
                        "Whenever Another App Is Open",
                        _action(lambda: self.set_auto_yield_mode("focused")),
                        checked=lambda _item: self.auto_yield_mode == "focused",
                        radio=True,
                    ),
                    MenuItem(
                        "Battery Saver Only",
                        _action(lambda: self.set_auto_yield_mode("battery_only")),
                        checked=lambda _item: self.auto_yield_mode == "battery_only",
                        radio=True,
                    ),
                    MenuItem(
                        "Always Active (Never Pause)",
                        _action(lambda: self.set_auto_yield_mode("off")),
                        checked=lambda _item: self.auto_yield_mode == "off",
                        radio=True,
                    ),
                ),
            ),
            Menu.SEPARATOR,
            MenuItem("Toggle Clean Feed", _action(self.toggle_clean_feed)),
            MenuItem("Reload", _action(self.reload_ui)),
            Menu.SEPARATOR,
            MenuItem(
                "Start with Windows" if _IS_WINDOWS else "Launch at Login",
                _action(self.toggle_autostart),
                checked=lambda _item: self._autostart_enabled,
            ),
            MenuItem("Open Logs", _action(self.open_logs_folder)),
            MenuItem(lambda _item: self._update_label, _action(self.check_for_updates)),
            Menu.SEPARATOR,
            MenuItem("Quit", _action(self.quit_app)),
        )

    def start_tray(self) -> None:
        if self.tray_icon is not None:
            return
        try:
            import pystray
            menu = self.build_tray_menu()
        except Exception as exc:
            _get_logger().warning("Tray initialization skipped: %s", exc)
            return

        image = _safe_call(create_tray_icon_image, 64)
        if image is None:
            return

        try:
            self.tray_icon = pystray.Icon("ide_lyrics_studio", image, WINDOW_TITLE, menu)
            if _IS_MACOS or _IS_LINUX:
                def _tray_setup(icon: Any) -> None:
                    self._run_on_tray_thread(lambda: _safe_call(setattr, icon, "visible", True))

                if hasattr(self.tray_icon, "run_detached"):
                    try:
                        self.tray_icon.run_detached(setup=_tray_setup)
                    except NotImplementedError:
                        threading.Thread(
                            target=lambda: self.tray_icon.run(setup=_tray_setup),
                            name="ide-lyrics-tray",
                            daemon=True,
                        ).start()
                else:
                    threading.Thread(
                        target=lambda: self.tray_icon.run(setup=_tray_setup),
                        name="ide-lyrics-tray",
                        daemon=True,
                    ).start()
                self._tray_available = True if _IS_MACOS else _has_linux_tray_host()
                if callable(self._on_signals_reinstall):
                    _safe_call(self._on_signals_reinstall)
            else:
                if hasattr(self.tray_icon, "run_detached"):
                    self.tray_icon.run_detached()
                else:
                    threading.Thread(target=self.tray_icon.run, name="ide-lyrics-tray", daemon=True).start()
                self._tray_available = True
        except Exception as exc:
            _get_logger().warning("Failed to start system tray: %s", exc)
            self.tray_icon = None
            self._tray_available = False

    def _refresh_tray_menu(self) -> None:
        if self.tray_icon is not None:
            icon_ref = self.tray_icon
            self._run_on_tray_thread(icon_ref.update_menu)

    def on_webview_loaded(self) -> None:
        self._is_loaded.set()

        def _post_load() -> None:
            if self._shutting_down:
                return
            is_initial = False
            with self._mode_lock:
                if not self._initial_load_handled:
                    self._initial_load_handled = True
                    is_initial = True

            if not is_initial:
                self._notify_js_mode(self.mode)
                self._notify_js_overlay_prefs()
                self._eval_js(
                    f"window.onDesktopPowerStateChanged && window.onDesktopPowerStateChanged({json.dumps(self.get_power_yield_state())})",
                    background=True,
                )
                self._push_current_snapshot()
                self._refresh_tray_menu()
                return

            self.get_hwnd()
            self._set_taskbar_visible(False)

            if self._open_wizard_on_load:
                self._open_wizard_on_load = False
                self.start_hidden = False
                self.set_mode("app", show_window=True)
                self._eval_js("window.openSetupWizard && window.openSetupWizard()", background=True)
            elif self.mode == "wallpaper":
                self.start_hidden = False
                self.set_mode("wallpaper", show_window=True)
            elif not self.start_hidden:
                self.set_mode(self.mode, show_window=True)
            else:
                self.start_hidden = False
                self._window_visible = False
                self._notify_js_mode(self.mode)
                self._notify_js_overlay_prefs()
                self._refresh_tray_menu()

            self._eval_js(
                f"window.onDesktopPowerStateChanged && window.onDesktopPowerStateChanged({json.dumps(self.get_power_yield_state())})",
                background=True,
            )
            self._push_current_snapshot()

        threading.Thread(target=_post_load, name="ide-lyrics-post-load", daemon=True).start()

    def quit_app(self) -> dict[str, Any]:
        self._allow_close = True
        self.shutdown(destroy_window=True)
        return {"ok": True}

    def shutdown(self, destroy_window: bool = False) -> None:
        with self._shutdown_lock:
            if self._shutting_down:
                return
            self._shutting_down = True

        self._capture_and_save_geometry()
        self._detach_wallpaper_if_attached()

        self._yield_stop_event.set()
        with self._yield_lock:
            yield_thread = self._yield_thread
            self._yield_thread = None
        if yield_thread is not None and yield_thread.is_alive() and yield_thread is not threading.current_thread():
            yield_thread.join(timeout=2.0)

        if self.smtc_bridge is not None:
            _safe_call(self.smtc_bridge.stop)

        stop_local_server(self.http_server)
        self.http_server = None
        with suppress(OSError):
            if RUNTIME_PORT_PATH.is_file():
                RUNTIME_PORT_PATH.unlink()

        if self.tray_icon is not None:
            icon_ref = self.tray_icon
            self.tray_icon = None
            self._tray_available = False
            if _IS_MACOS:
                def _cleanup_mac_tray() -> None:
                    with suppress(Exception):
                        status_bar = getattr(icon_ref, "_status_bar", None)
                        status_item = getattr(icon_ref, "_status_item", None)
                        if status_bar is not None and status_item is not None:
                            status_bar.removeStatusItem_(status_item)
                    _safe_call(icon_ref.stop)

                self._run_on_tray_thread(_cleanup_mac_tray)
            else:
                self._run_on_tray_thread(icon_ref.stop)

        if destroy_window and self.window is not None:
            _safe_call(self.window.destroy)


class DesktopApi:
    """JS bridge exposed on window.pywebview.api inside live-wallpaper.html."""

    def __init__(self, controller: DesktopAppController) -> None:
        self._controller = controller

    def get_mode(self) -> str:
        return self._controller.get_mode()

    def get_version(self) -> str:
        return self._controller.get_version()

    def check_for_updates(self) -> dict[str, Any]:
        return self._controller.check_for_updates()

    def set_mode(self, mode: str) -> dict[str, Any]:
        return self._controller.set_mode(mode)

    def get_overlay_side(self) -> str:
        return self._controller.overlay_side

    def snap_overlay_side(self, side: str) -> dict[str, Any]:
        return self._controller.snap_overlay_side(side)

    def get_overlay_on_top(self) -> bool:
        return bool(self._controller.overlay_on_top)

    def set_overlay_on_top(self, enabled: bool) -> dict[str, Any]:
        return self._controller.set_overlay_on_top(enabled)

    def toggle_overlay_on_top(self) -> dict[str, Any]:
        return self._controller.toggle_overlay_on_top()

    def get_overlay_click_through(self) -> bool:
        return bool(self._controller.overlay_click_through)

    def set_overlay_click_through(self, enabled: bool) -> dict[str, Any]:
        return self._controller.set_overlay_click_through(enabled)

    def toggle_overlay_click_through(self) -> dict[str, Any]:
        return self._controller.toggle_overlay_click_through()

    def get_overlay_opacity(self) -> float:
        return float(self._controller.overlay_opacity)

    def set_overlay_opacity(self, opacity: float) -> dict[str, Any]:
        return self._controller.set_overlay_opacity(opacity)

    def get_auto_yield_mode(self) -> str:
        return self._controller.auto_yield_mode

    def set_auto_yield_mode(self, mode: str) -> dict[str, Any]:
        return self._controller.set_auto_yield_mode(mode)

    def get_power_yield_state(self) -> dict[str, Any]:
        return self._controller.get_power_yield_state()

    def start_window_drag(self) -> dict[str, Any]:
        return self._controller.start_window_drag()

    def open_settings(self) -> dict[str, Any]:
        self._controller.open_settings_in_app_mode()
        return {"ok": True}

    def get_monitors(self) -> list[dict[str, Any]]:
        return self._controller.get_monitors()

    def set_wallpaper_monitor(self, target: str) -> dict[str, Any]:
        return self._controller.set_wallpaper_monitor(target)

    def media_command(self, cmd: str) -> dict[str, Any]:
        return self._controller.media_command(cmd)

    def toggle_fullscreen(self) -> dict[str, Any]:
        return self._controller.toggle_fullscreen()

    def is_wizard_needed(self) -> bool:
        return self._controller.is_wizard_needed()

    def mark_wizard_completed(self) -> dict[str, Any]:
        return self._controller.mark_wizard_completed()

    def get_setup_state(self) -> dict[str, Any]:
        return self._controller.get_setup_state()

    def detect_integrations(self) -> dict[str, Any]:
        return self._controller.detect_integrations()

    def install_cider_plugin(self) -> dict[str, Any]:
        return self._controller.install_cider_plugin()

    def uninstall_cider_plugin(self) -> dict[str, Any]:
        return self._controller.uninstall_cider_plugin()

    def install_spicetify_extension(self) -> dict[str, Any]:
        return self._controller.install_spicetify_extension()

    def uninstall_spicetify_extension(self) -> dict[str, Any]:
        return self._controller.uninstall_spicetify_extension()

    def apply_wizard_setup(self, options: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self._controller.apply_wizard_setup(options)

    def quit_app(self) -> dict[str, Any]:
        return self._controller.quit_app()


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="IDE Lyrics Studio - Standalone Desktop App & Live Wallpaper"
    )
    parser.add_argument(
        "--mode",
        choices=VALID_MODES,
        default=None,
        help="Launch mode: app, wallpaper, or overlay (overrides saved preference)",
    )
    parser.add_argument(
        "--choose",
        action="store_true",
        help="Show launch mode picker dialog before starting",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Show the window immediately instead of starting hidden in the tray",
    )
    parser.add_argument(
        "--tray",
        "--background",
        dest="tray",
        action="store_true",
        help="Start quietly in the system tray (skips first-run setup wizard)",
    )
    parser.add_argument(
        "--wizard",
        action="store_true",
        help="Open the Setup & Integrations Wizard on launch",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    setup_logging(LOG_PATH)

    args = parse_args(argv)

    instance_lock = SingleInstanceLock()
    if not instance_lock.acquire():
        if not args.tray:
            notify_running_instance_show(RUNTIME_PORT_PATH)
        return 0
    atexit.register(instance_lock.release)

    with suppress(Exception):
        from wallpaper_host import enable_dpi_awareness

        enable_dpi_awareness()

    mode = get_saved_mode(CONFIG_PATH)
    if args.mode:
        mode = save_mode(args.mode, CONFIG_PATH)
    elif args.choose:
        picked = show_mode_picker(mode)
        if picked in VALID_MODES:
            mode = save_mode(picked, CONFIG_PATH)

    try:
        import webview
    except ImportError as exc:
        print("ERROR: pywebview is not installed. Run desktop-app/setup.ps1 first.", file=sys.stderr)
        print(f"Details: {exc}", file=sys.stderr)
        return 1

    first_run_wizard = bool(
        args.wizard
        or (not is_wizard_completed(CONFIG_PATH) and not args.tray and not args.mode)
    )
    controller = DesktopAppController(
        initial_mode=mode,
        config_path=CONFIG_PATH,
        start_hidden=bool(args.tray and not args.show and not args.choose and not first_run_wizard),
        open_wizard=first_run_wizard,
    )
    api = DesktopApi(controller)

    server, port = start_local_server(REPO_ROOT, on_show_request=controller.show_existing_window)
    controller.http_server = server
    controller.http_port = port
    with suppress(OSError):
        RUNTIME_PORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RUNTIME_PORT_PATH.write_text(str(port), encoding="utf-8")
    url = f"http://127.0.0.1:{port}/live-wallpaper.html?desktopApp=1"
    controller._url = url

    atexit.register(controller.shutdown)

    _sig_shutdown_started = False

    def _sig_handler(_signum: int, _frame: Any) -> None:
        nonlocal _sig_shutdown_started
        controller._allow_close = True
        if _sig_shutdown_started or controller._shutting_down:
            return
        _sig_shutdown_started = True
        threading.Thread(
            target=controller.shutdown,
            kwargs={"destroy_window": True},
            name="ide-lyrics-signal-shutdown",
            daemon=True,
        ).start()

    def _register_signals() -> None:
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                _safe_call(signal.signal, sig, _sig_handler)

    _register_signals()

    initial_w, initial_h = DEFAULT_OVERLAY_SIZE if mode == "overlay" else DEFAULT_APP_SIZE
    window = webview.create_window(
        WINDOW_TITLE,
        url=url,
        js_api=api,
        width=initial_w,
        height=initial_h,
        resizable=True,
        hidden=True,
        focus=True,
        easy_drag=False,
        background_color="#0b0d14",
        on_top=(mode == "overlay" and controller.overlay_on_top),
    )
    controller.window = window

    window.events.before_show += controller.on_before_show
    window.events.loaded += controller.on_webview_loaded
    window.events.closing += controller.on_window_closing
    window.events.closed += lambda: controller.shutdown(destroy_window=False)

    controller._on_signals_reinstall = _register_signals
    controller.start_smtc()
    controller.start_yield_watcher()
    if _IS_WINDOWS:
        controller.start_tray()
        _register_signals()

    with suppress(Exception):
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    gui_backend: Optional[str] = (
        "edgechromium"
        if _IS_WINDOWS
        else ("cocoa" if _IS_MACOS else ("gtk" if _IS_LINUX else None))
    )
    try:
        webview.start(
            gui=gui_backend,
            debug=False,
            private_mode=False,
            storage_path=str(STORAGE_DIR),
        )
    finally:
        controller.shutdown(destroy_window=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
