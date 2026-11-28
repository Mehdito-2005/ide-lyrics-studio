"""Cross-platform ecosystem integration helpers for IDE Lyrics Studio.

Provides detection and 1-click install/uninstall for:
- Cider Plugin (Mehdito-2005/cider-ide-lyrics)
- Spicetify Extension for Spotify (Mehdito-2005/spicetify-ide-lyrics)
"""

from __future__ import annotations

from contextlib import suppress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Iterable, Optional
import urllib.request

_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS = sys.platform == "darwin"
_IS_LINUX = sys.platform.startswith("linux")

CIDER_REPO = "Mehdito-2005/cider-ide-lyrics"
SPICETIFY_REPO = "Mehdito-2005/spicetify-ide-lyrics"

CIDER_PLUGIN_ID = "sh.cider.ide-lyric-visualizer"
CIDER_LEGACY_DIR_NAME = "cider-ide-lyrics-visualizer"
CIDER_PLUGIN_FILES: tuple[str, ...] = (
    "plugin.js",
    "plugin.yml",
    "index.js",
    "updater.js",
    "styles.css",
    "plugin.json",
    "plugin.config.ts",
)
CIDER_REQUIRED_FILES: tuple[str, ...] = ("index.js", "plugin.json")
SPICETIFY_EXT_FILE = "ideLyrics.js"

_HTTP_TIMEOUT = 12.0
_USER_AGENT = "IDE-Lyrics-Studio/1.2.0"
_SUBPROCESS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0


def _run_silent(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=_SUBPROCESS_FLAGS,
    )


def _which_exe(name: str, extra_candidates: Iterable[Path] = ()) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for candidate in extra_candidates:
        if candidate.is_file():
            if _IS_WINDOWS or os.access(candidate, os.X_OK):
                return str(candidate)
    if _IS_MACOS or _IS_LINUX:
        for base in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
            cand = Path(base) / name
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
    return None


def _download_bytes(url: str, timeout: float = _HTTP_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=max(1.0, float(timeout))) as resp:
        return resp.read()


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for p in paths:
        key = str(p).lower() if _IS_WINDOWS else str(p)
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


def get_cider_candidate_plugin_roots() -> list[Path]:
    """Return all known Cider plugin directories for the current OS."""
    candidates: list[Path] = []
    home = Path.home()

    if _IS_WINDOWS:
        appdata = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
        localappdata = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        candidates.extend(
            [
                appdata / "sh.cider.dotnet" / "plugins",
                appdata / "C2Windows" / "plugins",
                appdata / "Cider" / "plugins",
                appdata / "Cider2" / "plugins",
                appdata / "sh.cider.electron" / "plugins",
                localappdata / "Programs" / "Cider" / "resources" / "plugins",
            ]
        )
        packages_dir = localappdata / "Packages"
        if packages_dir.is_dir():
            with suppress(OSError):
                for pkg in packages_dir.iterdir():
                    if pkg.is_dir() and pkg.name.startswith("CiderCollective.Cider_"):
                        roaming = pkg / "LocalCache" / "Roaming"
                        candidates.extend(
                            [
                                roaming / "sh.cider.dotnet" / "plugins",
                                roaming / "Cider" / "plugins",
                                roaming / "sh.cider.electron" / "plugins",
                            ]
                        )
    elif _IS_MACOS:
        app_support = home / "Library" / "Application Support"
        candidates.extend(
            [
                app_support / "sh.cider.electron" / "plugins",
                app_support / "Cider" / "plugins",
                app_support / "Cider2" / "plugins",
                app_support / "sh.cider.dotnet" / "plugins",
            ]
        )
    else:
        xdg_cfg = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
        candidates.extend(
            [
                xdg_cfg / "sh.cider.electron" / "plugins",
                xdg_cfg / "Cider" / "plugins",
                xdg_cfg / "Cider2" / "plugins",
                home / ".var" / "app" / "sh.cider.Cider" / "config" / "Cider" / "plugins",
                home / ".var" / "app" / "sh.cider.Cider" / "config" / "sh.cider.electron" / "plugins",
                home / "snap" / "cider" / "current" / ".config" / "Cider" / "plugins",
            ]
        )

    return _dedupe_paths(candidates)


def find_cider_plugin_roots(
    candidate_roots: Optional[Iterable[Path]] = None,
) -> tuple[list[Path], list[Path]]:
    """Return (detected_roots, installed_plugin_dirs) for Cider."""
    roots = _dedupe_paths(
        [Path(p) for p in candidate_roots]
        if candidate_roots is not None
        else get_cider_candidate_plugin_roots()
    )
    detected: list[Path] = []
    installed: list[Path] = []
    for root in roots:
        if root.is_dir() or root.parent.is_dir():
            detected.append(root)
            plugin_dir = root / CIDER_PLUGIN_ID
            if (plugin_dir / "index.js").is_file() or (plugin_dir / "plugin.json").is_file():
                installed.append(plugin_dir)
    return detected, installed


def find_spicetify_env(
    *,
    spicetify_bin: Optional[str] = None,
    extensions_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Locate the Spicetify CLI binary and Extensions directory."""
    home = Path.home()
    if spicetify_bin is not None:
        cli = spicetify_bin
    else:
        extra: list[Path] = []
        if _IS_WINDOWS:
            localappdata = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
            extra.extend(
                [
                    localappdata / "spicetify" / "spicetify.exe",
                    home / "spicetify-cli" / "spicetify.exe",
                ]
            )
        else:
            extra.extend(
                [
                    home / ".spicetify" / "spicetify",
                    home / ".local" / "bin" / "spicetify",
                ]
            )
        cli = _which_exe("spicetify", extra) or ""

    ext_path: Optional[Path] = Path(extensions_dir) if extensions_dir is not None else None
    if ext_path is None and cli:
        with suppress(Exception):
            proc = _run_silent([cli, "-c"], timeout=4.0)
            cfg_line = (proc.stdout or "").strip().splitlines()
            if proc.returncode == 0 and cfg_line:
                cfg_file = Path(cfg_line[0].strip())
                if cfg_file.name:
                    ext_path = cfg_file.parent / "Extensions"

    if ext_path is None:
        if _IS_WINDOWS:
            appdata = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
            ext_path = appdata / "spicetify" / "Extensions"
        else:
            xdg_cfg = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
            ext_path = xdg_cfg / "spicetify" / "Extensions"

    installed = (ext_path / SPICETIFY_EXT_FILE).is_file()
    detected = bool(cli or ext_path.is_dir() or ext_path.parent.is_dir())
    return {
        "cli": cli,
        "cli_found": bool(cli),
        "detected": detected,
        "extensions_dir": str(ext_path),
        "installed": installed,
    }


def detect_integrations(
    repo_root: Optional[Path] = None,
    *,
    cider_roots: Optional[Iterable[Path]] = None,
    spicetify_bin: Optional[str] = None,
    spicetify_extensions_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Inspect system integration readiness for Cider and Spicetify."""
    detected_cider, installed_cider = find_cider_plugin_roots(cider_roots)
    sp_env = find_spicetify_env(
        spicetify_bin=spicetify_bin,
        extensions_dir=spicetify_extensions_dir,
    )
    return {
        "platform": sys.platform,
        "cider": {
            "detected": bool(detected_cider),
            "installed": bool(installed_cider),
            "roots": [str(p) for p in detected_cider],
            "installed_paths": [str(p) for p in installed_cider],
            "repo_url": f"https://github.com/{CIDER_REPO}",
        },
        "spicetify": {
            "detected": bool(sp_env["detected"]),
            "cli_found": bool(sp_env["cli_found"]),
            "installed": bool(sp_env["installed"]),
            "extensions_dir": sp_env["extensions_dir"],
            "repo_url": f"https://github.com/{SPICETIFY_REPO}",
        },
        "has_local_cider_source": bool(
            repo_root and (Path(repo_root) / "index.js").is_file() and (Path(repo_root) / "plugin.json").is_file()
        ),
        "has_local_spicetify_source": bool(
            repo_root
            and (
                (Path(repo_root) / "spicetify" / SPICETIFY_EXT_FILE).is_file()
                or (Path(repo_root) / SPICETIFY_EXT_FILE).is_file()
            )
        ),
    }


def _load_cider_payloads(
    repo_root: Optional[Path],
    allow_download: bool,
) -> tuple[dict[str, bytes], str]:
    payloads: dict[str, bytes] = {}
    if repo_root is not None:
        root_path = Path(repo_root)
        if all((root_path / req).is_file() for req in CIDER_REQUIRED_FILES):
            for fname in CIDER_PLUGIN_FILES:
                src = root_path / fname
                if src.is_file():
                    payloads[fname] = src.read_bytes()
            return payloads, "local"

    if not allow_download:
        return {}, "none"

    for fname in CIDER_PLUGIN_FILES:
        url = f"https://raw.githubusercontent.com/{CIDER_REPO}/main/{fname}"
        try:
            payloads[fname] = _download_bytes(url)
        except Exception:
            if fname in CIDER_REQUIRED_FILES:
                raise
    return payloads, "github"


def install_cider_plugin(
    repo_root: Optional[Path] = None,
    *,
    target_roots: Optional[Iterable[Path]] = None,
    allow_download: bool = True,
) -> dict[str, Any]:
    """Install or update the Cider IDE Lyrics plugin into detected Cider plugin folders."""
    try:
        payloads, source = _load_cider_payloads(repo_root, allow_download=allow_download)
    except Exception as exc:
        return {"ok": False, "error": f"Could not fetch Cider plugin files: {exc}"}

    if not all(req in payloads for req in CIDER_REQUIRED_FILES):
        return {"ok": False, "error": "Cider plugin files (index.js / plugin.json) are unavailable."}

    if target_roots is not None:
        roots = _dedupe_paths(Path(p) for p in target_roots)
    else:
        detected, _ = find_cider_plugin_roots()
        if detected:
            roots = detected
        else:
            candidates = get_cider_candidate_plugin_roots()
            roots = [candidates[0]] if candidates else []

    if not roots:
        return {"ok": False, "error": "No Cider plugin directory could be resolved."}

    installed_paths: list[str] = []
    try:
        for root in roots:
            target_dir = root / CIDER_PLUGIN_ID
            target_dir.mkdir(parents=True, exist_ok=True)
            legacy_dir = root / CIDER_LEGACY_DIR_NAME
            if legacy_dir.is_dir():
                with suppress(OSError):
                    shutil.rmtree(legacy_dir)
            for fname, content in payloads.items():
                (target_dir / fname).write_bytes(content)
            installed_paths.append(str(target_dir))
    except OSError as exc:
        return {"ok": False, "error": f"Failed writing Cider plugin files: {exc}"}

    return {
        "ok": True,
        "source": source,
        "installed_paths": installed_paths,
        "files_written": sorted(payloads.keys()),
        "message": f"Installed Cider plugin ({len(installed_paths)} location(s)). Restart Cider to load it.",
    }


def uninstall_cider_plugin(
    *,
    target_roots: Optional[Iterable[Path]] = None,
) -> dict[str, Any]:
    """Remove the Cider IDE Lyrics plugin from Cider plugin folders."""
    roots = (
        _dedupe_paths(Path(p) for p in target_roots)
        if target_roots is not None
        else get_cider_candidate_plugin_roots()
    )
    removed: list[str] = []
    for root in roots:
        for folder_name in (CIDER_PLUGIN_ID, CIDER_LEGACY_DIR_NAME):
            target_dir = root / folder_name
            if target_dir.is_dir():
                try:
                    shutil.rmtree(target_dir)
                    removed.append(str(target_dir))
                except OSError as exc:
                    return {"ok": False, "error": f"Failed removing {target_dir}: {exc}"}

    msg = (
        f"Removed Cider plugin from {len(removed)} location(s)."
        if removed
        else "Cider plugin was not installed."
    )
    return {"ok": True, "removed_paths": removed, "message": msg}


def _load_spicetify_payload(
    repo_root: Optional[Path],
    allow_download: bool,
) -> tuple[bytes, str]:
    if repo_root is not None:
        root_path = Path(repo_root)
        for candidate in (root_path / "spicetify" / SPICETIFY_EXT_FILE, root_path / SPICETIFY_EXT_FILE):
            if candidate.is_file():
                return candidate.read_bytes(), "local"

    if not allow_download:
        return b"", "none"

    url = f"https://raw.githubusercontent.com/{SPICETIFY_REPO}/main/{SPICETIFY_EXT_FILE}"
    return _download_bytes(url), "github"


def install_spicetify_extension(
    repo_root: Optional[Path] = None,
    *,
    extensions_dir: Optional[Path] = None,
    spicetify_bin: Optional[str] = None,
    allow_download: bool = True,
    run_cli: bool = True,
) -> dict[str, Any]:
    """Install or update ideLyrics.js into Spicetify and apply it if Spicetify CLI is installed."""
    try:
        payload, source = _load_spicetify_payload(repo_root, allow_download=allow_download)
    except Exception as exc:
        return {"ok": False, "error": f"Could not fetch Spicetify extension: {exc}"}

    if not payload:
        return {"ok": False, "error": "Spicetify extension (ideLyrics.js) is unavailable."}

    env = find_spicetify_env(spicetify_bin=spicetify_bin, extensions_dir=extensions_dir)
    ext_dir = Path(env["extensions_dir"])
    dest = ext_dir / SPICETIFY_EXT_FILE
    try:
        ext_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
    except OSError as exc:
        return {"ok": False, "error": f"Failed writing {dest}: {exc}"}

    applied = False
    cli_note = ""
    cli = env["cli"]
    if run_cli and cli:
        try:
            cfg_proc = _run_silent([cli, "config", "extensions", SPICETIFY_EXT_FILE], timeout=15.0)
            if cfg_proc.returncode == 0:
                apply_proc = _run_silent([cli, "apply"], timeout=35.0)
                if apply_proc.returncode == 0:
                    applied = True
                else:
                    cli_note = (apply_proc.stderr or apply_proc.stdout or "spicetify apply returned non-zero").strip()
            else:
                cli_note = (cfg_proc.stderr or cfg_proc.stdout or "spicetify config failed").strip()
        except Exception as exc:
            cli_note = str(exc)

    if applied:
        message = f"Installed and applied Spicetify extension at {dest}."
    elif cli:
        message = (
            f"Copied {SPICETIFY_EXT_FILE} to {ext_dir}. "
            f"Run `spicetify config extensions {SPICETIFY_EXT_FILE} && spicetify apply` if needed"
            + (f" ({cli_note})." if cli_note else ".")
        )
    else:
        message = (
            f"Copied {SPICETIFY_EXT_FILE} to {ext_dir}. "
            "Install Spicetify CLI (spicetify.app) then run `spicetify config extensions ideLyrics.js && spicetify apply`."
        )

    return {
        "ok": True,
        "source": source,
        "path": str(dest),
        "applied": applied,
        "cli_found": bool(cli),
        "message": message,
    }


def uninstall_spicetify_extension(
    *,
    extensions_dir: Optional[Path] = None,
    spicetify_bin: Optional[str] = None,
    run_cli: bool = True,
) -> dict[str, Any]:
    """Remove ideLyrics.js from Spicetify Extensions and deregister via Spicetify CLI."""
    env = find_spicetify_env(spicetify_bin=spicetify_bin, extensions_dir=extensions_dir)
    dest = Path(env["extensions_dir"]) / SPICETIFY_EXT_FILE
    removed = False
    if dest.is_file():
        try:
            dest.unlink()
            removed = True
        except OSError as exc:
            return {"ok": False, "error": f"Failed removing {dest}: {exc}"}

    applied = False
    cli = env["cli"]
    if run_cli and cli:
        with suppress(Exception):
            cfg_proc = _run_silent([cli, "config", "extensions", f"{SPICETIFY_EXT_FILE}-"], timeout=15.0)
            if cfg_proc.returncode == 0:
                apply_proc = _run_silent([cli, "apply"], timeout=35.0)
                applied = apply_proc.returncode == 0

    msg = "Removed Spicetify extension." if removed else "Spicetify extension was not installed."
    return {"ok": True, "removed": removed, "applied": applied, "message": msg}
