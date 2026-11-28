"""Cross-platform wallpaper attachment and window-mode helper.

Attaches a native window behind desktop icons via Win32 WorkerW on Windows,
AppKit kCGDesktopWindowLevel on macOS, or GTK3 desktop/keep-below hints on Linux.
"""

from __future__ import annotations

from contextlib import suppress
import ctypes
from ctypes import wintypes
import math
import re
import shutil
import subprocess
import sys
import threading
from typing import Any, Callable, Optional

_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS = sys.platform == "darwin"
_IS_LINUX = sys.platform.startswith("linux")

_MACOS_DESKTOP_WINDOW_LEVEL = -2147483623  # kCGDesktopWindowLevel
_MACOS_NORMAL_WINDOW_LEVEL = 0
_MACOS_FLOATING_WINDOW_LEVEL = 3

_WM_SPAWN_WORKER = 0x052C
_SMTO_NORMAL = 0x0000
_GWL_STYLE = -16
_GWL_EXSTYLE = -20

_WS_POPUP = 0x80000000
_WS_CHILD = 0x40000000
_WS_VISIBLE = 0x10000000
_WS_CAPTION = 0x00C00000
_WS_BORDER = 0x00800000
_WS_DLGFRAME = 0x00400000
_WS_SYSMENU = 0x00080000
_WS_THICKFRAME = 0x00040000
_WS_MINIMIZEBOX = 0x00020000
_WS_MAXIMIZEBOX = 0x00010000

_WS_CHROME_MASK = (
    _WS_CAPTION
    | _WS_BORDER
    | _WS_DLGFRAME
    | _WS_SYSMENU
    | _WS_THICKFRAME
    | _WS_MINIMIZEBOX
    | _WS_MAXIMIZEBOX
    | _WS_POPUP
)

_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_APPWINDOW = 0x00040000
_WS_EX_NOACTIVATE = 0x08000000

_MONITOR_DEFAULTTONEAREST = 2
_GA_ROOT = 2
_DESKTOP_READOBJECTS = 0x0001
_QUNS_NOT_PRESENT = 1
_QUNS_RUNNING_D3D_FULL_SCREEN = 3
_QUNS_PRESENTATION_MODE = 4
_SHELL_WINDOW_CLASSES: frozenset[str] = frozenset({
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "NotifyIconOverflowWindow",
    "DV2ControlHost",
})

_HWND_TOP = 0
_HWND_BOTTOM = 1
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_WM_NCLBUTTONDOWN = 0x00A1
_HTCAPTION = 2

_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020
_SWP_SHOWWINDOW = 0x0040

_SW_HIDE = 0
_SW_SHOW = 5
_SW_RESTORE = 9
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

_DWMWA_NCRENDERING_POLICY = 2
_DWMNCRP_DISABLED = 1
_DWMNCRP_ENABLED = 2
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWCP_DEFAULT = 0
_DWMWCP_DONOTROUND = 1
_DWMWA_BORDER_COLOR = 34
_DWMWA_COLOR_NONE = 0xFFFFFFFE
_DWMWA_COLOR_DEFAULT = 0xFFFFFFFF
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMSBT_AUTO = 0
_DWMSBT_NONE = 1

_SM_CXSCREEN = 0
_SM_CYSCREEN = 1
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_MONITORINFOF_PRIMARY = 0x00000001

_XRANDR_MONITOR_RE = re.compile(
    r"^(\S+)\s+connected(?:\s+(primary))?\s+(\d+)x(\d+)\+(-?\d+)\+(-?\d+)",
    re.MULTILINE,
)


class _DWM_MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


def _apply_windows_dwm_overlay_attrs(hwnd: Any, enabled: bool) -> None:
    """Disable or restore Windows 11 DWM Mica backdrop, rounded frame, and border for overlay transparency."""
    if not _IS_WINDOWS:
        return
    try:
        hwnd_int = int(hwnd or 0)
    except Exception:
        return
    if hwnd_int == 0:
        return
    with suppress(Exception):
        dwmapi = ctypes.windll.dwmapi
        hwnd_ptr = ctypes.c_void_p(hwnd_int)

        def _set_attr(attr: int, val: int) -> None:
            buf = ctypes.c_uint32(val & 0xFFFFFFFF)
            dwmapi.DwmSetWindowAttribute(hwnd_ptr, wintypes.DWORD(attr), ctypes.byref(buf), ctypes.sizeof(buf))

        if enabled:
            _set_attr(_DWMWA_SYSTEMBACKDROP_TYPE, _DWMSBT_NONE)
            _set_attr(_DWMWA_WINDOW_CORNER_PREFERENCE, _DWMWCP_DONOTROUND)
            _set_attr(_DWMWA_BORDER_COLOR, _DWMWA_COLOR_NONE)
            _set_attr(_DWMWA_NCRENDERING_POLICY, _DWMNCRP_DISABLED)
            margins = _DWM_MARGINS(0, 0, 0, 0)
            dwmapi.DwmExtendFrameIntoClientArea(hwnd_ptr, ctypes.byref(margins))
        else:
            _set_attr(_DWMWA_NCRENDERING_POLICY, _DWMNCRP_ENABLED)
            _set_attr(_DWMWA_WINDOW_CORNER_PREFERENCE, _DWMWCP_DEFAULT)
            _set_attr(_DWMWA_BORDER_COLOR, _DWMWA_COLOR_DEFAULT)
            _set_attr(_DWMWA_SYSTEMBACKDROP_TYPE, _DWMSBT_AUTO)


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


def clamp_overlay_opacity(value: Any, min_opacity: float = 0.25) -> float:
    """Clamp overlay window opacity to [min_opacity, 1.0], defaulting to 1.0 on invalid/bool input."""
    if isinstance(value, bool) or value is None:
        return 1.0
    try:
        lo = float(min_opacity)
        if not math.isfinite(lo):
            lo = 0.25
        lo = max(0.05, min(1.0, lo))
    except Exception:
        lo = 0.25
    try:
        num = float(value)
    except Exception:
        return 1.0
    if not math.isfinite(num):
        return 1.0
    return round(max(lo, min(1.0, num)), 4)


def enable_dpi_awareness() -> None:
    """Enable Per-Monitor V2 DPI awareness on Windows so screen bounds use real pixels."""
    if not _IS_WINDOWS:
        return
    with suppress(Exception):
        user32 = ctypes.windll.user32
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
            if user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
            ):
                return
        if hasattr(user32, "SetProcessDPIAware"):
            user32.SetProcessDPIAware()


if _IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _LONG_PTR = ctypes.c_ssize_t
    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    _USER32_SIGNATURES: tuple[tuple[str, list[Any], Any], ...] = (
        ("FindWindowW", [wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND),
        ("FindWindowExW", [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND),
        (
            "SendMessageTimeoutW",
            [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
                wintypes.UINT,
                wintypes.UINT,
                ctypes.POINTER(ctypes.c_size_t),
            ],
            _LONG_PTR,
        ),
        ("EnumWindows", [_WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL),
        ("SetParent", [wintypes.HWND, wintypes.HWND], wintypes.HWND),
        ("GetParent", [wintypes.HWND], wintypes.HWND),
        ("IsWindow", [wintypes.HWND], wintypes.BOOL),
        ("GetWindowRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
        (
            "SetWindowPos",
            [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT],
            wintypes.BOOL,
        ),
        ("GetSystemMetrics", [ctypes.c_int], ctypes.c_int),
        ("ShowWindow", [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        ("SetForegroundWindow", [wintypes.HWND], wintypes.BOOL),
        ("UpdateWindow", [wintypes.HWND], wintypes.BOOL),
        ("ReleaseCapture", [], wintypes.BOOL),
        ("SendMessageW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], _LONG_PTR),
        (
            "EnumDisplayMonitors",
            [wintypes.HDC, ctypes.POINTER(wintypes.RECT), _MONITORENUMPROC, wintypes.LPARAM],
            wintypes.BOOL,
        ),
        ("GetMonitorInfoW", [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFOEXW)], wintypes.BOOL),
        ("GetForegroundWindow", [], wintypes.HWND),
        ("GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        ("IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
        ("IsIconic", [wintypes.HWND], wintypes.BOOL),
        ("MonitorFromWindow", [wintypes.HWND, wintypes.DWORD], wintypes.HMONITOR),
        ("GetAncestor", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
        ("OpenInputDesktop", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], ctypes.c_void_p),
        ("CloseDesktop", [ctypes.c_void_p], wintypes.BOOL),
    )
    for _fn_name, _argtypes, _restype in _USER32_SIGNATURES:
        with suppress(Exception):
            _fn = getattr(_user32, _fn_name)
            _fn.argtypes = _argtypes
            _fn.restype = _restype

    _shell32 = None
    with suppress(Exception):
        _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        _shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
        _shell32.SHQueryUserNotificationState.restype = ctypes.c_long

    _kernel32 = None
    with suppress(Exception):
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(_SYSTEM_POWER_STATUS)]
        _kernel32.GetSystemPowerStatus.restype = wintypes.BOOL

    if ctypes.sizeof(ctypes.c_void_p) == 8 and hasattr(_user32, "GetWindowLongPtrW"):
        _get_window_long = _user32.GetWindowLongPtrW
        _set_window_long = _user32.SetWindowLongPtrW
    else:
        _get_window_long = _user32.GetWindowLongW
        _set_window_long = _user32.SetWindowLongW

    _get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
    _get_window_long.restype = _LONG_PTR
    _set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, _LONG_PTR]
    _set_window_long.restype = _LONG_PTR
else:
    _user32 = None
    _shell32 = None
    _kernel32 = None
    _get_window_long = None
    _set_window_long = None


def _parse_xrandr_monitors(output: str) -> list[dict]:
    monitors: list[dict] = []
    for match in _XRANDR_MONITOR_RE.finditer(str(output or "")):
        name, primary_flag, w_s, h_s, x_s, y_s = match.groups()
        w, h, x, y = int(w_s), int(h_s), int(x_s), int(y_s)
        if w <= 0 or h <= 0:
            continue
        idx = len(monitors)
        monitors.append(
            {
                "id": str(idx),
                "name": name,
                "label": f"Monitor {idx + 1} ({w}x{h})",
                "rect": (x, y, w, h),
                "workerw_rect": (x, y, w, h),
                "is_primary": bool(primary_flag),
            }
        )

    if monitors and not any(m["is_primary"] for m in monitors):
        monitors[0]["is_primary"] = True
    return monitors


def _query_xrandr_monitors() -> list[dict]:
    xrandr = shutil.which("xrandr")
    if not xrandr:
        return []
    with suppress(Exception):
        proc = subprocess.run(
            [xrandr, "--query"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        if proc.returncode == 0:
            return _parse_xrandr_monitors(proc.stdout)
    return []


def _query_gdk_monitors() -> list[dict]:
    """Enumerate monitors via Gdk.Display when xrandr is unavailable (e.g. Wayland)."""
    monitors: list[dict] = []
    if not _IS_LINUX:
        return monitors
    with suppress(Exception):
        from gi.repository import Gdk  # type: ignore

        display = Gdk.Display.get_default()
        if display is None:
            return monitors
        n = int(display.get_n_monitors())
        for idx in range(n):
            mon = display.get_monitor(idx)
            if mon is None:
                continue
            geom = mon.get_geometry()
            w, h = int(geom.width), int(geom.height)
            if w <= 0 or h <= 0:
                continue
            x, y = int(geom.x), int(geom.y)
            is_primary = bool(mon.is_primary()) if hasattr(mon, "is_primary") else (idx == 0)
            model = str(mon.get_model() or "") if hasattr(mon, "get_model") else ""
            name = model.strip() or f"DISPLAY{idx + 1}"
            monitors.append(
                {
                    "id": str(idx),
                    "name": name,
                    "label": f"Monitor {idx + 1} ({w}x{h})",
                    "rect": (x, y, w, h),
                    "workerw_rect": (x, y, w, h),
                    "is_primary": is_primary,
                }
            )
    if monitors and not any(m["is_primary"] for m in monitors):
        monitors[0]["is_primary"] = True
    return monitors


def _query_linux_monitors() -> list[dict]:
    return _query_xrandr_monitors() or _query_gdk_monitors()


def _get_macos_wallpaper_level() -> int:
    """Return a Cocoa window level strictly above the system desktop picture and below desktop icons."""
    if _IS_MACOS:
        with suppress(Exception):
            import Quartz  # type: ignore

            return int(Quartz.CGWindowLevelForKey(Quartz.kCGDesktopIconWindowLevelKey)) - 1
    return _MACOS_DESKTOP_WINDOW_LEVEL + 1


def _set_macos_webview_transparent(nswin: Any, transparent: bool) -> None:
    cv = None
    with suppress(Exception):
        cv = nswin.contentView()
    if cv is None:
        return
    with suppress(Exception):
        cv.setValue_forKey_(not bool(transparent), "drawsBackground")
    with suppress(Exception):
        cv.setValue_forKey_(bool(transparent), "drawsTransparentBackground")


def _find_gtk_webview(widget: Any) -> Any:
    """Walk a GTK3 container recursively to locate the hosted WebKit2.WebView widget."""
    if widget is None:
        return None
    if hasattr(widget, "set_background_color") and hasattr(widget, "get_settings"):
        return widget
    children: list[Any] = []
    with suppress(Exception):
        if hasattr(widget, "get_children"):
            children = list(widget.get_children() or [])
        elif hasattr(widget, "get_child"):
            child = widget.get_child()
            if child is not None:
                children = [child]
    for child in children:
        found = _find_gtk_webview(child)
        if found is not None:
            return found
    return None


def _dispatch_to_main_thread(schedule_fn: Callable[[Callable[[], Any]], Any], fn: Callable[[], Any]) -> Any:
    if threading.current_thread() is threading.main_thread():
        return fn()
    done = threading.Event()
    box: dict[str, Any] = {}

    def _wrapped() -> bool:
        try:
            box["res"] = fn()
        except Exception as exc:
            box["err"] = exc
        finally:
            done.set()
        return False

    schedule_fn(_wrapped)
    done.wait(timeout=2.0)
    return box.get("res")


def _run_on_macos_main_thread(fn: Callable[[], Any]) -> Any:
    try:
        from PyObjCTools import AppHelper  # type: ignore

        return _dispatch_to_main_thread(AppHelper.callAfter, fn)
    except Exception:
        return fn()


def _run_on_gtk_main_thread(fn: Callable[[], Any]) -> Any:
    try:
        from gi.repository import GLib  # type: ignore

        return _dispatch_to_main_thread(GLib.idle_add, fn)
    except Exception:
        return fn()


def _run_on_windows_ui_thread(native_form: Any, fn: Callable[[], Any]) -> Any:
    """Marshal Win32 HWND mutations onto WinForms' UI thread when off-thread."""
    if not _IS_WINDOWS or native_form is None or not getattr(native_form, "InvokeRequired", False):
        return fn()

    from System import Action  # type: ignore

    box: dict[str, Any] = {}

    def _wrapped() -> None:
        try:
            box["res"] = fn()
        except Exception as exc:
            box["err"] = exc

    native_form.Invoke(Action(_wrapped))
    if "err" in box:
        raise box["err"]
    return box.get("res")


def _run_ui_bool(runner: Callable[[Callable[[], Any]], Any], fn: Callable[[], bool]) -> bool:
    try:
        return bool(runner(fn))
    except Exception:
        return False


def _unwrap_native_window(handle: Any, *required_attrs: str) -> Any:
    if handle is None or isinstance(handle, int):
        return None
    if all(hasattr(handle, attr) for attr in required_attrs):
        return handle
    win = getattr(handle, "window", None)
    if win is not None and hasattr(win, required_attrs[0]):
        return win
    return None


class WallpaperHost:
    """Manages wallpaper attachment, display bounds, and window Z-order across OSes."""

    def __init__(self) -> None:
        self._saved_states: dict[int, dict] = {}
        self._linux_overlay_css: dict[int, Any] = {}

    @classmethod
    def prepare_linux_window(cls, handle: Any) -> bool:
        """Assign an RGBA visual before realization so GTK3 Mini Overlay supports alpha transparency."""
        if not _IS_LINUX:
            return False
        gtk_win = _unwrap_native_window(handle, "get_screen", "set_visual")
        if gtk_win is None:
            return False

        def _do() -> bool:
            if not gtk_win.get_realized():
                screen = gtk_win.get_screen()
                visual = screen.get_rgba_visual() if screen is not None else None
                if visual is not None:
                    gtk_win.set_visual(visual)
                gtk_win.set_app_paintable(True)
            return True

        return _run_ui_bool(_run_on_gtk_main_thread, _do)

    @classmethod
    def present_linux_window(cls, handle: Any) -> bool:
        """Un-iconify, show, and raise a GTK3 window."""
        if not _IS_LINUX:
            return False
        gtk_win = _unwrap_native_window(handle, "present", "show_all")
        if gtk_win is None:
            return False

        def _do() -> bool:
            with suppress(Exception):
                gtk_win.deiconify()
            gtk_win.show_all()
            gtk_win.present()
            return True

        return _run_ui_bool(_run_on_gtk_main_thread, _do)

    @staticmethod
    def _is_valid_hwnd(hwnd: object) -> bool:
        if not _IS_WINDOWS or _user32 is None or not isinstance(hwnd, int) or hwnd <= 0:
            return False
        try:
            return bool(_user32.IsWindow(hwnd))
        except Exception:
            return False

    @staticmethod
    def get_primary_screen_rect() -> tuple[int, int, int, int]:
        if _IS_WINDOWS and _user32 is not None:
            with suppress(Exception):
                w = int(_user32.GetSystemMetrics(_SM_CXSCREEN)) or 1920
                h = int(_user32.GetSystemMetrics(_SM_CYSCREEN)) or 1080
                return (0, 0, w, h)
            return (0, 0, 1920, 1080)

        if _IS_MACOS:
            with suppress(Exception):
                import AppKit  # type: ignore

                screen = AppKit.NSScreen.mainScreen() or (AppKit.NSScreen.screens() or [None])[0]
                if screen is not None:
                    frame = screen.frame()
                    return (0, 0, max(1, int(frame.size.width)), max(1, int(frame.size.height)))

        if _IS_LINUX:
            mons = _query_linux_monitors()
            if mons:
                primary = next((m for m in mons if m.get("is_primary")), mons[0])
                _, _, w, h = primary["rect"]
                return (0, 0, int(w), int(h))

        return (0, 0, 1920, 1080)

    @classmethod
    def get_virtual_screen_bounds(cls) -> tuple[int, int, int, int]:
        if _IS_WINDOWS and _user32 is not None:
            with suppress(Exception):
                vx = int(_user32.GetSystemMetrics(_SM_XVIRTUALSCREEN))
                vy = int(_user32.GetSystemMetrics(_SM_YVIRTUALSCREEN))
                vw = int(_user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN))
                vh = int(_user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
                if vw > 0 and vh > 0:
                    return (vx, vy, vw, vh)
            return cls.get_primary_screen_rect()

        if _IS_MACOS:
            with suppress(Exception):
                import AppKit  # type: ignore

                screens = list(AppKit.NSScreen.screens() or [])
                if screens:
                    min_x = min(int(s.frame().origin.x) for s in screens)
                    min_y = min(int(s.frame().origin.y) for s in screens)
                    max_x = max(int(s.frame().origin.x + s.frame().size.width) for s in screens)
                    max_y = max(int(s.frame().origin.y + s.frame().size.height) for s in screens)
                    return (min_x, min_y, max(1, max_x - min_x), max(1, max_y - min_y))

        if _IS_LINUX:
            mons = _query_linux_monitors()
            if mons:
                min_x = min(m["rect"][0] for m in mons)
                min_y = min(m["rect"][1] for m in mons)
                max_x = max(m["rect"][0] + m["rect"][2] for m in mons)
                max_y = max(m["rect"][1] + m["rect"][3] for m in mons)
                return (min_x, min_y, max(1, max_x - min_x), max(1, max_y - min_y))

        return cls.get_primary_screen_rect()

    @classmethod
    def get_virtual_screen_rect(cls) -> tuple[int, int, int, int]:
        vx, vy, vw, vh = cls.get_virtual_screen_bounds()
        if _IS_WINDOWS:
            return (0, 0, max(1, vw), max(1, vh))
        return (int(vx), int(vy), max(1, vw), max(1, vh))

    @classmethod
    def list_monitors(cls) -> list[dict]:
        vx, vy, _, _ = cls.get_virtual_screen_bounds()
        monitors: list[dict] = []

        if _IS_WINDOWS and _user32 is not None:
            try:
                @_MONITORENUMPROC
                def _enum_mon_cb(hmon: int, _hdc: int, _lprect: object, _lparam: int) -> bool:
                    info = _MONITORINFOEXW()
                    info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
                    if _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                        rc = info.rcMonitor
                        x, y = int(rc.left), int(rc.top)
                        w = max(1, int(rc.right - rc.left))
                        h = max(1, int(rc.bottom - rc.top))
                        idx = len(monitors)
                        monitors.append(
                            {
                                "id": str(idx),
                                "name": str(info.szDevice or f"DISPLAY{idx + 1}"),
                                "label": f"Monitor {idx + 1} ({w}x{h})",
                                "rect": (x, y, w, h),
                                "workerw_rect": (x - vx, y - vy, w, h),
                                "is_primary": bool(info.dwFlags & _MONITORINFOF_PRIMARY),
                            }
                        )
                    return True

                _user32.EnumDisplayMonitors(None, None, _enum_mon_cb, 0)
            except Exception:
                monitors = []
        elif _IS_MACOS:
            try:
                import AppKit  # type: ignore

                for idx, screen in enumerate(list(AppKit.NSScreen.screens() or [])):
                    f = screen.frame()
                    x, y = int(f.origin.x), int(f.origin.y)
                    w, h = max(1, int(f.size.width)), max(1, int(f.size.height))
                    monitors.append(
                        {
                            "id": str(idx),
                            "name": f"Display {idx + 1}",
                            "label": f"Monitor {idx + 1} ({w}x{h})",
                            "rect": (x, y, w, h),
                            "workerw_rect": (x, y, w, h),
                            "is_primary": idx == 0,
                        }
                    )
            except Exception:
                monitors = []
        elif _IS_LINUX:
            monitors = _query_linux_monitors()

        if not monitors:
            _, _, pw, ph = cls.get_primary_screen_rect()
            monitors.append(
                {
                    "id": "0",
                    "name": "DISPLAY1",
                    "label": f"Monitor 1 ({pw}x{ph})",
                    "rect": (0, 0, pw, ph),
                    "workerw_rect": (-vx, -vy, pw, ph) if _IS_WINDOWS else (0, 0, pw, ph),
                    "is_primary": True,
                }
            )

        return monitors

    @classmethod
    def resolve_monitor_rect(cls, target: str = "primary") -> tuple[int, int, int, int]:
        """Resolve 'primary', 'all', monitor index/id, or device name to a wallpaper rect."""
        lowered = str(target or "primary").strip().lower()
        if lowered == "all":
            return cls.get_virtual_screen_rect()

        monitors = cls.list_monitors()
        if lowered and lowered != "primary":
            for mon in monitors:
                if lowered in (str(mon.get("id", "")).lower(), str(mon.get("name", "")).lower()):
                    return tuple(int(v) for v in mon["workerw_rect"])  # type: ignore[return-value]
            if lowered.isdigit():
                idx = int(lowered)
                if 0 <= idx < len(monitors):
                    return tuple(int(v) for v in monitors[idx]["workerw_rect"])  # type: ignore[return-value]

        chosen = next((m for m in monitors if m.get("is_primary")), monitors[0])
        return tuple(int(v) for v in chosen["workerw_rect"])  # type: ignore[return-value]

    @classmethod
    def get_window_rect(cls, hwnd: int) -> Optional[tuple[int, int, int, int]]:
        if not cls._is_valid_hwnd(hwnd) or _user32 is None:
            return None
        with suppress(Exception):
            rect = wintypes.RECT()
            if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                w, h = int(rect.right - rect.left), int(rect.bottom - rect.top)
                if w > 0 and h > 0:
                    return (int(rect.left), int(rect.top), w, h)
        return None

    @classmethod
    def set_window_rect(
        cls,
        hwnd: int,
        rect: tuple[int, int, int, int],
        *,
        native_form: Any = None,
    ) -> bool:
        """Apply physical-pixel bounds on Windows without pywebview DPI double-scaling."""
        if not cls._is_valid_hwnd(hwnd) or _user32 is None:
            return False

        def _do() -> bool:
            x, y, w, h = (int(v) for v in rect)
            return bool(
                _user32.SetWindowPos(
                    hwnd,
                    0,
                    x,
                    y,
                    max(280, w),
                    max(140, h),
                    _SWP_NOZORDER | _SWP_NOACTIVATE,
                )
            )

        try:
            return bool(_run_on_windows_ui_thread(native_form, _do))
        except Exception:
            return False

    @classmethod
    def clamp_rect_to_virtual_screen(
        cls,
        rect: tuple[int, int, int, int],
        min_size: tuple[int, int] = (280, 140),
    ) -> tuple[int, int, int, int]:
        vx, vy, vw, vh = cls.get_virtual_screen_bounds()
        x, y, w, h = (int(v) for v in rect)
        min_w, min_h = max(1, int(min_size[0])), max(1, int(min_size[1]))

        w = max(min_w, min(w, max(min_w, vw)))
        h = max(min_h, min(h, max(min_h, vh)))
        x = max(vx, min(x, vx + vw - w))
        y = max(vy, min(y, vy + vh - h))
        return (x, y, w, h)

    def _find_workerw(self) -> int:
        if not _IS_WINDOWS or _user32 is None:
            return 0
        try:
            progman = _user32.FindWindowW("Progman", None)
            if not progman:
                return 0

            result = ctypes.c_size_t(0)
            _user32.SendMessageTimeoutW(
                progman, _WM_SPAWN_WORKER, 0xD, 0x1, _SMTO_NORMAL, 1000, ctypes.byref(result)
            )

            workerw_hwnd = 0

            @_WNDENUMPROC
            def _enum_cb(top_hwnd: int, _lparam: int) -> bool:
                nonlocal workerw_hwnd
                if _user32.FindWindowExW(top_hwnd, 0, "SHELLDLL_DefView", None):
                    sibling = _user32.FindWindowExW(0, top_hwnd, "WorkerW", None)
                    if sibling:
                        workerw_hwnd = int(sibling)
                        return False
                return True

            _user32.EnumWindows(_enum_cb, 0)

            # Windows 11 24H2+ can host WorkerW directly as a child of Progman.
            if not workerw_hwnd:
                child_worker = _user32.FindWindowExW(progman, 0, "WorkerW", None)
                if child_worker:
                    workerw_hwnd = int(child_worker)

            return workerw_hwnd
        except Exception:
            return 0

    @staticmethod
    def _state_key(handle: Any) -> int:
        return handle if isinstance(handle, int) else id(handle)

    def is_attached(self, hwnd: Any) -> bool:
        state = self._saved_states.get(self._state_key(hwnd))
        return bool(state and state.get("attached"))

    def attach_to_wallpaper(
        self,
        hwnd: Any,
        monitor_rect: Optional[tuple[int, int, int, int]] = None,
        *,
        native_form: Any = None,
    ) -> bool:
        if _IS_MACOS:
            return self._attach_macos(hwnd, monitor_rect)
        if _IS_LINUX:
            return self._attach_linux(hwnd, monitor_rect)
        if not self._is_valid_hwnd(hwnd):
            return False

        workerw = self._find_workerw()
        if not workerw:
            return False

        def _do() -> bool:
            if hwnd not in self._saved_states:
                rect = wintypes.RECT()
                _user32.GetWindowRect(hwnd, ctypes.byref(rect))
                self._saved_states[hwnd] = {
                    "style": int(_get_window_long(hwnd, _GWL_STYLE)),
                    "ex_style": int(_get_window_long(hwnd, _GWL_EXSTYLE)),
                    "rect": (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top),
                    "parent": int(_user32.GetParent(hwnd) or 0),
                    "attached": False,
                }

            saved = self._saved_states[hwnd]
            new_style = (saved["style"] & ~_WS_CHROME_MASK) | _WS_CHILD | _WS_VISIBLE
            new_ex_style = (saved["ex_style"] & ~_WS_EX_APPWINDOW) | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE

            _user32.ShowWindow(hwnd, _SW_HIDE)
            _set_window_long(hwnd, _GWL_STYLE, new_style)
            _set_window_long(hwnd, _GWL_EXSTYLE, new_ex_style)
            _user32.SetParent(hwnd, workerw)

            if monitor_rect is not None and len(monitor_rect) == 4:
                x, y, w, h = (int(v) for v in monitor_rect)
            else:
                _, _, w, h = self.get_primary_screen_rect()
                # WorkerW (0, 0) maps to the virtual desktop origin, so offset primary by (-vx, -vy).
                x = -int(_user32.GetSystemMetrics(_SM_XVIRTUALSCREEN))
                y = -int(_user32.GetSystemMetrics(_SM_YVIRTUALSCREEN))

            _user32.SetWindowPos(
                hwnd,
                _HWND_TOP,
                x,
                y,
                max(1, w),
                max(1, h),
                _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED | _SWP_SHOWWINDOW,
            )
            _user32.ShowWindow(hwnd, _SW_SHOW)
            _user32.UpdateWindow(hwnd)
            saved["attached"] = True
            return True

        try:
            return bool(_run_on_windows_ui_thread(native_form, _do))
        except Exception:
            return False

    def detach_from_wallpaper(self, hwnd: Any, *, native_form: Any = None) -> bool:
        if _IS_MACOS:
            return self._detach_macos(hwnd)
        if _IS_LINUX:
            return self._detach_linux(hwnd)
        if not self._is_valid_hwnd(hwnd):
            self._saved_states.pop(hwnd, None)
            return False

        def _do() -> bool:
            saved = self._saved_states.pop(hwnd, None)
            # Hide before detaching from explorer.exe's WorkerW to avoid a cross-process DWM deadlock.
            _user32.ShowWindow(hwnd, _SW_HIDE)
            _user32.SetParent(hwnd, 0)

            if saved is not None:
                _set_window_long(hwnd, _GWL_STYLE, int(saved["style"]) & ~_WS_CHILD)
                _set_window_long(hwnd, _GWL_EXSTYLE, int(saved["ex_style"]) & ~_WS_EX_NOACTIVATE)
                x, y, w, h = saved["rect"]
                _user32.SetWindowPos(
                    hwnd,
                    _HWND_NOTOPMOST,
                    int(x),
                    int(y),
                    max(320, int(w)),
                    max(240, int(h)),
                    _SWP_FRAMECHANGED | _SWP_NOACTIVATE,
                )
            else:
                cur_style = int(_get_window_long(hwnd, _GWL_STYLE))
                restored_style = (
                    (cur_style & ~_WS_CHILD)
                    | _WS_CAPTION
                    | _WS_SYSMENU
                    | _WS_THICKFRAME
                    | _WS_MINIMIZEBOX
                    | _WS_MAXIMIZEBOX
                )
                cur_ex = int(_get_window_long(hwnd, _GWL_EXSTYLE))
                _set_window_long(hwnd, _GWL_STYLE, restored_style)
                _set_window_long(hwnd, _GWL_EXSTYLE, cur_ex & ~_WS_EX_NOACTIVATE)
                _user32.SetWindowPos(
                    hwnd,
                    _HWND_NOTOPMOST,
                    0,
                    0,
                    0,
                    0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_FRAMECHANGED | _SWP_NOACTIVATE,
                )

            _user32.ShowWindow(hwnd, _SW_SHOW)
            _user32.UpdateWindow(hwnd)
            return True

        try:
            return bool(_run_on_windows_ui_thread(native_form, _do))
        except Exception:
            return False

    def set_taskbar_visible(self, hwnd: Any, visible: bool, *, native_form: Any = None) -> bool:
        if _IS_MACOS:
            return self._set_macos_dock_visible(visible)
        if _IS_LINUX:
            return self._set_linux_taskbar_visible(hwnd, visible)
        if not self._is_valid_hwnd(hwnd):
            return False

        def _do() -> bool:
            ex_style = int(_get_window_long(hwnd, _GWL_EXSTYLE))
            if visible:
                new_ex_style = (ex_style & ~_WS_EX_TOOLWINDOW) | _WS_EX_APPWINDOW
            else:
                new_ex_style = (ex_style & ~_WS_EX_APPWINDOW) | _WS_EX_TOOLWINDOW

            if new_ex_style != ex_style:
                _set_window_long(hwnd, _GWL_EXSTYLE, new_ex_style)
                _user32.SetWindowPos(
                    hwnd,
                    0,
                    0,
                    0,
                    0,
                    0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED,
                )
            return True

        try:
            return bool(_run_on_windows_ui_thread(native_form, _do))
        except Exception:
            return False

    def set_always_on_top(self, hwnd: Any, enabled: bool, *, native_form: Any = None) -> bool:
        if _IS_MACOS:
            return self._set_macos_on_top(hwnd, enabled)
        if _IS_LINUX:
            return self._set_linux_on_top(hwnd, enabled)
        if not self._is_valid_hwnd(hwnd):
            return False

        def _do() -> bool:
            z_order = _HWND_TOPMOST if enabled else _HWND_NOTOPMOST
            return bool(
                _user32.SetWindowPos(
                    hwnd,
                    z_order,
                    0,
                    0,
                    0,
                    0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
                )
            )

        try:
            return bool(_run_on_windows_ui_thread(native_form, _do))
        except Exception:
            return False

    def set_overlay_appearance(self, hwnd: Any, enabled: bool, *, native_form: Any = None) -> bool:
        """Switch between frameless transparent floating overlay and opaque framed window."""
        if _IS_MACOS:
            nswin = _unwrap_native_window(hwnd, "setStyleMask_")
            if nswin is None:
                return False

            def _do_mac() -> bool:
                import AppKit  # type: ignore

                if enabled:
                    nswin.setStyleMask_(AppKit.NSWindowStyleMaskBorderless)
                    nswin.setOpaque_(False)
                    nswin.setBackgroundColor_(AppKit.NSColor.clearColor())
                    nswin.setHasShadow_(False)
                    with suppress(Exception):
                        nswin.setMovableByWindowBackground_(True)
                    _set_macos_webview_transparent(nswin, True)
                else:
                    if not self.is_attached(nswin):
                        nswin.setStyleMask_(
                            AppKit.NSWindowStyleMaskTitled
                            | AppKit.NSWindowStyleMaskClosable
                            | AppKit.NSWindowStyleMaskMiniaturizable
                            | AppKit.NSWindowStyleMaskResizable
                        )
                        nswin.setHasShadow_(True)
                    nswin.setOpaque_(True)
                    nswin.setBackgroundColor_(
                        AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
                            11.0 / 255.0, 13.0 / 255.0, 20.0 / 255.0, 1.0
                        )
                    )
                    with suppress(Exception):
                        nswin.setMovableByWindowBackground_(False)
                    _set_macos_webview_transparent(nswin, False)
                return True

            return _run_ui_bool(_run_on_macos_main_thread, _do_mac)

        if _IS_LINUX:
            gtk_win = _unwrap_native_window(hwnd, "set_keep_below", "set_decorated")
            if gtk_win is None:
                return False

            def _do_gtk() -> bool:
                from gi.repository import Gdk, Gtk  # type: ignore

                key = self._state_key(gtk_win)
                wv = _find_gtk_webview(gtk_win)
                scrolled = None
                with suppress(Exception):
                    scrolled = gtk_win.get_child()

                if enabled:
                    gtk_win.set_decorated(False)
                    with suppress(Exception):
                        if not gtk_win.get_realized():
                            screen = gtk_win.get_screen()
                            visual = screen.get_rgba_visual() if screen is not None else None
                            if visual is not None:
                                gtk_win.set_visual(visual)
                        gtk_win.set_app_paintable(True)

                    provider = self._linux_overlay_css.get(key)
                    if provider is None:
                        provider = Gtk.CssProvider()
                        provider.load_from_data(
                            b"window, scrolledwindow, viewport, * { background-color: rgba(0, 0, 0, 0); background-image: none; }"
                        )
                        self._linux_overlay_css[key] = provider
                    with suppress(Exception):
                        gtk_win.get_style_context().add_provider(
                            provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
                        )
                    if scrolled is not None:
                        with suppress(Exception):
                            scrolled.get_style_context().add_provider(
                                provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
                            )
                    if wv is not None:
                        with suppress(Exception):
                            wv.set_background_color(Gdk.RGBA(0.0, 0.0, 0.0, 0.0))
                else:
                    provider = self._linux_overlay_css.pop(key, None)
                    if provider is not None:
                        with suppress(Exception):
                            gtk_win.get_style_context().remove_provider(provider)
                        if scrolled is not None:
                            with suppress(Exception):
                                scrolled.get_style_context().remove_provider(provider)
                    if wv is not None:
                        with suppress(Exception):
                            wv.set_background_color(
                                Gdk.RGBA(11.0 / 255.0, 13.0 / 255.0, 20.0 / 255.0, 1.0)
                            )
                    if not self.is_attached(gtk_win):
                        gtk_win.set_decorated(True)
                return True

            return _run_ui_bool(_run_on_gtk_main_thread, _do_gtk)

        if not self._is_valid_hwnd(hwnd) or native_form is None:
            return False

        def _do_win() -> bool:
            from System.Drawing import Color  # type: ignore
            import System.Windows.Forms as WinForms  # type: ignore

            _apply_windows_dwm_overlay_attrs(hwnd, bool(enabled))
            if enabled:
                with suppress(Exception):
                    native_form.SetStyle(WinForms.ControlStyles.SupportsTransparentBackColor, True)
                native_form.FormBorderStyle = getattr(WinForms.FormBorderStyle, "None")
                key = Color.FromArgb(255, 1, 2, 3)
                native_form.BackColor = key
                native_form.TransparencyKey = key
                wv = getattr(native_form, "webview", None)
                if wv is not None:
                    wv.DefaultBackgroundColor = Color.Transparent
                    with suppress(Exception):
                        wv.BackColor = Color.Transparent
                _apply_windows_dwm_overlay_attrs(hwnd, True)
                with suppress(Exception):
                    native_form.Invalidate(True)
                    native_form.Update()
            else:
                native_form.TransparencyKey = Color.Empty
                opaque = Color.FromArgb(255, 11, 13, 20)
                native_form.BackColor = opaque
                wv = getattr(native_form, "webview", None)
                if wv is not None:
                    wv.DefaultBackgroundColor = opaque
                    with suppress(Exception):
                        wv.BackColor = opaque
                if not self.is_attached(hwnd):
                    native_form.FormBorderStyle = WinForms.FormBorderStyle.Sizable
                _apply_windows_dwm_overlay_attrs(hwnd, False)
                with suppress(Exception):
                    native_form.update_title_bar_theme()
            return True

        try:
            return bool(_run_on_windows_ui_thread(native_form, _do_win))
        except Exception:
            return False

    def set_window_z_layer(self, hwnd: Any, layer: str, *, native_form: Any = None) -> bool:
        """Place window at 'top', 'normal', or 'bottom' (desktop overlay layer)."""
        norm = str(layer or "normal").strip().lower()
        if norm not in ("top", "normal", "bottom"):
            norm = "normal"

        if _IS_MACOS:
            nswin = _unwrap_native_window(hwnd, "setLevel_")
            if nswin is None:
                return False

            def _do_mac() -> bool:
                import AppKit  # type: ignore

                if norm == "top":
                    nswin.setLevel_(_MACOS_FLOATING_WINDOW_LEVEL)
                    with suppress(Exception):
                        nswin.setCollectionBehavior_(
                            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                        )
                elif norm == "bottom":
                    nswin.setLevel_(_get_macos_wallpaper_level() + 1)
                else:
                    nswin.setLevel_(_MACOS_NORMAL_WINDOW_LEVEL)
                    with suppress(Exception):
                        nswin.setCollectionBehavior_(0)
                return True

            return _run_ui_bool(_run_on_macos_main_thread, _do_mac)

        if _IS_LINUX:
            gtk_win = _unwrap_native_window(hwnd, "set_keep_below", "set_decorated")
            if gtk_win is None:
                return False

            def _do_gtk() -> bool:
                gtk_win.set_keep_above(norm == "top")
                gtk_win.set_keep_below(norm == "bottom")
                return True

            return _run_ui_bool(_run_on_gtk_main_thread, _do_gtk)

        if not self._is_valid_hwnd(hwnd) or _user32 is None:
            return False

        def _do_win() -> bool:
            z_order = _HWND_TOPMOST if norm == "top" else (_HWND_BOTTOM if norm == "bottom" else _HWND_NOTOPMOST)
            return bool(
                _user32.SetWindowPos(
                    hwnd,
                    z_order,
                    0,
                    0,
                    0,
                    0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
                )
            )

        try:
            return bool(_run_on_windows_ui_thread(native_form, _do_win))
        except Exception:
            return False

    @classmethod
    def start_window_drag(cls, hwnd: Any, *, native_form: Any = None) -> bool:
        """Initiate a native OS borderless window drag from a WebView header."""
        if _IS_MACOS:
            nswin = _unwrap_native_window(hwnd, "performWindowDragWithEvent_")
            if nswin is None:
                return False

            def _do_mac() -> bool:
                import AppKit  # type: ignore

                event = AppKit.NSApp.currentEvent()
                if event is None:
                    return False
                nswin.performWindowDragWithEvent_(event)
                return True

            return _run_ui_bool(_run_on_macos_main_thread, _do_mac)

        if _IS_LINUX:
            gtk_win = _unwrap_native_window(hwnd, "begin_move_drag")
            if gtk_win is None:
                return False

            def _do_gtk() -> bool:
                from gi.repository import Gtk  # type: ignore

                display = gtk_win.get_display()
                if display is None:
                    return False
                seat = display.get_default_seat()
                pointer = seat.get_pointer() if seat is not None else None
                if pointer is None:
                    return False
                _, rx, ry = pointer.get_position()
                ts = int(Gtk.get_current_event_time() or 0)
                gtk_win.begin_move_drag(1, int(rx), int(ry), ts)
                return True

            return _run_ui_bool(_run_on_gtk_main_thread, _do_gtk)

        if not cls._is_valid_hwnd(hwnd) or _user32 is None:
            return False

        def _do() -> bool:
            _user32.ReleaseCapture()
            _user32.SendMessageW(hwnd, _WM_NCLBUTTONDOWN, _HTCAPTION, 0)
            return True

        try:
            return bool(_run_on_windows_ui_thread(native_form, _do))
        except Exception:
            return False

    @classmethod
    def set_click_through(cls, hwnd: Any, enabled: bool, *, native_form: Any = None) -> bool:
        """Toggle OS mouse-event pass-through ('ghost mode') for the overlay window."""
        want = bool(enabled)

        if _IS_MACOS:
            nswin = _unwrap_native_window(hwnd, "setIgnoresMouseEvents_")
            if nswin is None:
                return False

            def _do_mac() -> bool:
                nswin.setIgnoresMouseEvents_(want)
                return True

            return _run_ui_bool(_run_on_macos_main_thread, _do_mac)

        if _IS_LINUX:
            gtk_win = _unwrap_native_window(hwnd, "input_shape_combine_region")
            if gtk_win is None:
                return False

            def _do_gtk() -> bool:
                if want:
                    import cairo  # type: ignore

                    region = cairo.Region()
                    gtk_win.input_shape_combine_region(region)
                    with suppress(Exception):
                        gdk_win = gtk_win.get_window()
                        if gdk_win is not None:
                            gdk_win.input_shape_combine_region(region, 0, 0)
                else:
                    gtk_win.input_shape_combine_region(None)
                    with suppress(Exception):
                        gdk_win = gtk_win.get_window()
                        if gdk_win is not None:
                            gdk_win.input_shape_combine_region(None, 0, 0)
                return True

            return _run_ui_bool(_run_on_gtk_main_thread, _do_gtk)

        if not cls._is_valid_hwnd(hwnd) or _user32 is None or _get_window_long is None or _set_window_long is None:
            return False

        def _do_win() -> bool:
            ex_style = int(_get_window_long(hwnd, _GWL_EXSTYLE))
            if want:
                new_ex_style = ex_style | _WS_EX_TRANSPARENT | _WS_EX_NOACTIVATE
            else:
                new_ex_style = ex_style & ~(_WS_EX_TRANSPARENT | _WS_EX_NOACTIVATE)
            if new_ex_style != ex_style:
                _set_window_long(hwnd, _GWL_EXSTYLE, new_ex_style)
                _user32.SetWindowPos(
                    hwnd,
                    0,
                    0,
                    0,
                    0,
                    0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED,
                )
            return True

        try:
            return bool(_run_on_windows_ui_thread(native_form, _do_win))
        except Exception:
            return False

    @classmethod
    def set_window_opacity(cls, hwnd: Any, opacity: float, *, native_form: Any = None) -> bool:
        """Apply native window opacity clamped to [0.25, 1.0]."""
        clamped = clamp_overlay_opacity(opacity)

        if _IS_MACOS:
            nswin = _unwrap_native_window(hwnd, "setAlphaValue_")
            if nswin is None:
                return False

            def _do_mac() -> bool:
                nswin.setAlphaValue_(clamped)
                return True

            return _run_ui_bool(_run_on_macos_main_thread, _do_mac)

        if _IS_LINUX:
            gtk_win = _unwrap_native_window(hwnd, "set_opacity")
            if gtk_win is None:
                return False

            def _do_gtk() -> bool:
                gtk_win.set_opacity(clamped)
                return True

            return _run_ui_bool(_run_on_gtk_main_thread, _do_gtk)

        if not cls._is_valid_hwnd(hwnd) or native_form is None:
            return False

        def _do_win() -> bool:
            native_form.Opacity = clamped
            return True

        try:
            return bool(_run_on_windows_ui_thread(native_form, _do_win))
        except Exception:
            return False

    @staticmethod
    def evaluate_yield_state(
        policy: str,
        *,
        is_locked: bool,
        is_fullscreen: bool,
        is_battery_saver: bool,
    ) -> dict[str, Any]:
        """Evaluate whether desktop work should yield based on policy and OS signals."""
        norm_policy = str(policy or "auto").strip().lower()
        if norm_policy == "off":
            return {"paused": False, "reason": "active"}
        if norm_policy == "battery_only":
            if bool(is_battery_saver):
                return {"paused": True, "reason": "battery"}
            return {"paused": False, "reason": "active"}
        if bool(is_locked):
            return {"paused": True, "reason": "locked"}
        if bool(is_fullscreen):
            return {"paused": True, "reason": "fullscreen"}
        if bool(is_battery_saver):
            return {"paused": True, "reason": "battery"}
        return {"paused": False, "reason": "active"}

    @classmethod
    def detect_desktop_yield_state(
        cls,
        own_hwnd: int = 0,
        target_monitor_rect: Optional[tuple[int, int, int, int]] = None,
    ) -> dict[str, Any]:
        """Inspect OS lock screen, fullscreen foreground window, and battery saver state."""
        is_locked = False
        is_fullscreen = False
        is_battery_saver = False

        if _IS_WINDOWS:
            quns_val = 0
            if _shell32 is not None:
                with suppress(Exception):
                    quns = ctypes.c_int(0)
                    if _shell32.SHQueryUserNotificationState(ctypes.byref(quns)) == 0:
                        quns_val = int(quns.value)
                        if quns_val == _QUNS_NOT_PRESENT:
                            is_locked = True

            if _user32 is not None:
                with suppress(Exception):
                    hdesk = _user32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
                    if hdesk:
                        _user32.CloseDesktop(hdesk)
                    else:
                        is_locked = True

                with suppress(Exception):
                    fg = int(_user32.GetForegroundWindow() or 0)
                    own = int(own_hwnd or 0)
                    fg_root = int(_user32.GetAncestor(fg, _GA_ROOT) or 0) if fg else 0
                    if fg and fg != own and fg_root != own:
                        if _user32.IsWindowVisible(fg) and not _user32.IsIconic(fg):
                            cls_buf = ctypes.create_unicode_buffer(256)
                            _user32.GetClassNameW(fg, cls_buf, 256)
                            if cls_buf.value not in _SHELL_WINDOW_CLASSES:
                                rect = wintypes.RECT()
                                if _user32.GetWindowRect(fg, ctypes.byref(rect)):
                                    fw = int(rect.right - rect.left)
                                    fh = int(rect.bottom - rect.top)
                                    mon_rect: Optional[tuple[int, int, int, int]] = None
                                    if target_monitor_rect is not None and len(target_monitor_rect) == 4:
                                        tx, ty, tw, th = (int(v) for v in target_monitor_rect)
                                        if tw > 0 and th > 0:
                                            mon_rect = (tx, ty, tw, th)
                                    else:
                                        hmon = _user32.MonitorFromWindow(fg, _MONITOR_DEFAULTTONEAREST)
                                        if hmon:
                                            info = _MONITORINFOEXW()
                                            info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
                                            if _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                                                rc = info.rcMonitor
                                                mon_rect = (
                                                    int(rc.left),
                                                    int(rc.top),
                                                    int(rc.right - rc.left),
                                                    int(rc.bottom - rc.top),
                                                )
                                    if mon_rect is not None:
                                        mx, my, mw, mh = mon_rect
                                        if (
                                            fw >= mw - 4
                                            and fh >= mh - 4
                                            and rect.left <= mx + 2
                                            and rect.top <= my + 2
                                            and rect.right >= mx + mw - 2
                                            and rect.bottom >= my + mh - 2
                                        ):
                                            is_fullscreen = True
                                    elif quns_val in (_QUNS_RUNNING_D3D_FULL_SCREEN, _QUNS_PRESENTATION_MODE):
                                        is_fullscreen = True

            if _kernel32 is not None:
                with suppress(Exception):
                    pwr = _SYSTEM_POWER_STATUS()
                    if _kernel32.GetSystemPowerStatus(ctypes.byref(pwr)):
                        is_battery_saver = bool(pwr.SystemStatusFlag == 1)

        elif _IS_MACOS:
            with suppress(Exception):
                import Quartz  # type: ignore

                sess = Quartz.CGSessionCopyCurrentDictionary()
                if sess:
                    is_locked = bool(sess.get("CGSSessionScreenIsLocked", False))
            with suppress(Exception):
                from Foundation import NSProcessInfo  # type: ignore

                info = NSProcessInfo.processInfo()
                if hasattr(info, "isLowPowerModeEnabled"):
                    is_battery_saver = bool(info.isLowPowerModeEnabled())

        elif _IS_LINUX:
            with suppress(Exception):
                pp = shutil.which("powerprofilesctl")
                if pp:
                    proc = subprocess.run([pp, "get"], capture_output=True, text=True, timeout=0.5, check=False)
                    if proc.returncode == 0 and proc.stdout.strip().lower() == "power-saver":
                        is_battery_saver = True
            with suppress(Exception):
                gdbus = shutil.which("gdbus")
                if gdbus:
                    proc = subprocess.run(
                        [
                            gdbus,
                            "call",
                            "--session",
                            "--dest",
                            "org.freedesktop.ScreenSaver",
                            "--object-path",
                            "/org/freedesktop/ScreenSaver",
                            "--method",
                            "org.freedesktop.ScreenSaver.GetActive",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=0.5,
                        check=False,
                    )
                    if proc.returncode == 0 and "true" in proc.stdout.lower():
                        is_locked = True

        return {
            "is_locked": bool(is_locked),
            "is_fullscreen": bool(is_fullscreen),
            "is_battery_saver": bool(is_battery_saver),
        }

    def _attach_macos(self, handle: Any, monitor_rect: Optional[tuple[int, int, int, int]]) -> bool:
        nswin = _unwrap_native_window(handle, "setLevel_")
        if nswin is None:
            return False

        def _do() -> bool:
            import AppKit  # type: ignore

            key = self._state_key(nswin)
            if key not in self._saved_states:
                self._saved_states[key] = {
                    "level": int(nswin.level()),
                    "style_mask": int(nswin.styleMask()),
                    "frame": nswin.frame(),
                    "collection": int(nswin.collectionBehavior()),
                    "attached": False,
                }

            if monitor_rect is not None and len(monitor_rect) == 4:
                x, y, w, h = (int(v) for v in monitor_rect)
                target_frame = AppKit.NSMakeRect(x, y, max(1, w), max(1, h))
            else:
                scr = AppKit.NSScreen.mainScreen() or (AppKit.NSScreen.screens() or [None])[0]
                target_frame = scr.frame() if scr is not None else AppKit.NSMakeRect(0, 0, 1920, 1080)

            nswin.setStyleMask_(AppKit.NSWindowStyleMaskBorderless)
            nswin.setOpaque_(True)
            nswin.setHasShadow_(False)
            with suppress(Exception):
                nswin.setCanHide_(False)
            with suppress(Exception):
                nswin.setMovableByWindowBackground_(False)
            _set_macos_webview_transparent(nswin, False)
            nswin.setLevel_(_get_macos_wallpaper_level())
            nswin.setCollectionBehavior_(
                AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                | AppKit.NSWindowCollectionBehaviorStationary
                | AppKit.NSWindowCollectionBehaviorIgnoresCycle
            )
            nswin.setIgnoresMouseEvents_(True)
            nswin.setFrame_display_(target_frame, True)
            nswin.orderFrontRegardless()
            self._saved_states[key]["attached"] = True
            return True

        return _run_ui_bool(_run_on_macos_main_thread, _do)

    def _detach_macos(self, handle: Any) -> bool:
        nswin = _unwrap_native_window(handle, "setLevel_")
        if nswin is None:
            return False

        def _do() -> bool:
            import AppKit  # type: ignore

            saved = self._saved_states.pop(self._state_key(nswin), None)
            nswin.setIgnoresMouseEvents_(False)
            with suppress(Exception):
                nswin.setCanHide_(True)
            if saved is not None:
                nswin.setStyleMask_(saved["style_mask"])
                nswin.setLevel_(saved["level"])
                nswin.setCollectionBehavior_(saved["collection"])
                nswin.setFrame_display_(saved["frame"], True)
            else:
                nswin.setStyleMask_(
                    AppKit.NSWindowStyleMaskTitled
                    | AppKit.NSWindowStyleMaskClosable
                    | AppKit.NSWindowStyleMaskMiniaturizable
                    | AppKit.NSWindowStyleMaskResizable
                )
                nswin.setLevel_(_MACOS_NORMAL_WINDOW_LEVEL)
            nswin.makeKeyAndOrderFront_(None)
            return True

        return _run_ui_bool(_run_on_macos_main_thread, _do)

    def _set_macos_on_top(self, handle: Any, enabled: bool) -> bool:
        nswin = _unwrap_native_window(handle, "setLevel_")
        if nswin is None:
            return False

        def _do() -> bool:
            nswin.setLevel_(_MACOS_FLOATING_WINDOW_LEVEL if enabled else _MACOS_NORMAL_WINDOW_LEVEL)
            return True

        return _run_ui_bool(_run_on_macos_main_thread, _do)

    @staticmethod
    def _set_macos_dock_visible(visible: bool) -> bool:
        def _do() -> bool:
            import AppKit  # type: ignore

            policy = (
                AppKit.NSApplicationActivationPolicyRegular
                if visible
                else AppKit.NSApplicationActivationPolicyAccessory
            )
            AppKit.NSApplication.sharedApplication().setActivationPolicy_(policy)
            return True

        return _run_ui_bool(_run_on_macos_main_thread, _do)

    def _attach_linux(self, handle: Any, monitor_rect: Optional[tuple[int, int, int, int]]) -> bool:
        gtk_win = _unwrap_native_window(handle, "set_keep_below", "set_decorated")
        if gtk_win is None:
            return False

        def _do() -> bool:
            from gi.repository import Gdk  # type: ignore

            key = self._state_key(gtk_win)
            if key not in self._saved_states:
                saved_rect = (100, 100, 1280, 800)
                with suppress(Exception):
                    pos, size = gtk_win.get_position(), gtk_win.get_size()
                    saved_rect = (int(pos[0]), int(pos[1]), int(size[0]), int(size[1]))
                self._saved_states[key] = {"rect": saved_rect, "attached": False}

            if monitor_rect is not None and len(monitor_rect) == 4:
                x, y, w, h = (int(v) for v in monitor_rect)
            else:
                x, y, w, h = self.get_primary_screen_rect()

            # X11/Wayland WMs ignore DESKTOP type hints while already mapped; unmap first.
            if gtk_win.get_visible():
                gtk_win.hide()
            with suppress(Exception):
                gtk_win.set_type_hint(Gdk.WindowTypeHint.DESKTOP)
            with suppress(Exception):
                gtk_win.set_accept_focus(False)
            gtk_win.set_decorated(False)
            gtk_win.set_skip_taskbar_hint(True)
            gtk_win.set_skip_pager_hint(True)
            gtk_win.set_keep_above(False)
            gtk_win.set_keep_below(True)
            with suppress(Exception):
                gtk_win.stick()
            with suppress(Exception):
                gtk_win.move(x, y)
                gtk_win.resize(max(1, w), max(1, h))
            gtk_win.show_all()
            with suppress(Exception):
                gtk_win.move(x, y)
                gtk_win.resize(max(1, w), max(1, h))
            self._saved_states[key]["attached"] = True
            return True

        return _run_ui_bool(_run_on_gtk_main_thread, _do)

    def _detach_linux(self, handle: Any) -> bool:
        gtk_win = _unwrap_native_window(handle, "set_keep_below", "set_decorated")
        if gtk_win is None:
            return False

        def _do() -> bool:
            from gi.repository import Gdk  # type: ignore

            saved = self._saved_states.pop(self._state_key(gtk_win), None)
            if gtk_win.get_visible():
                gtk_win.hide()
            with suppress(Exception):
                gtk_win.set_type_hint(Gdk.WindowTypeHint.NORMAL)
            with suppress(Exception):
                gtk_win.set_accept_focus(True)
            gtk_win.set_keep_below(False)
            with suppress(Exception):
                gtk_win.unstick()
            gtk_win.set_decorated(True)
            gtk_win.set_skip_taskbar_hint(False)
            gtk_win.set_skip_pager_hint(False)
            gtk_win.show_all()
            if saved and "rect" in saved:
                x, y, w, h = saved["rect"]
                with suppress(Exception):
                    gtk_win.resize(max(320, int(w)), max(240, int(h)))
                    gtk_win.move(int(x), int(y))
            gtk_win.present()
            return True

        return _run_ui_bool(_run_on_gtk_main_thread, _do)

    def _set_linux_on_top(self, handle: Any, enabled: bool) -> bool:
        gtk_win = _unwrap_native_window(handle, "set_keep_below", "set_decorated")
        if gtk_win is None:
            return False

        def _do() -> bool:
            gtk_win.set_keep_above(bool(enabled))
            return True

        return _run_ui_bool(_run_on_gtk_main_thread, _do)

    def _set_linux_taskbar_visible(self, handle: Any, visible: bool) -> bool:
        gtk_win = _unwrap_native_window(handle, "set_keep_below", "set_decorated")
        if gtk_win is None:
            return False

        def _do() -> bool:
            gtk_win.set_skip_taskbar_hint(not visible)
            gtk_win.set_skip_pager_hint(not visible)
            return True

        return _run_ui_bool(_run_on_gtk_main_thread, _do)
