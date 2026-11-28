param(
    [switch]$OneDir,
    [switch]$Installer
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$LiveWallpaper = Join-Path $RepoRoot "live-wallpaper.html"
$AppPy = Join-Path $ScriptDir "app.py"
$IconPath = Join-Path $ScriptDir "ide-lyrics.ico"
$VersionFile = Join-Path $ScriptDir "version_info.txt"
$InstallerScript = Join-Path $ScriptDir "installer.iss"
$DistDir = Join-Path $ScriptDir "dist"
$BuildDir = Join-Path ([System.IO.Path]::GetTempPath()) ("ide-lyrics-pyi-" + [System.Guid]::NewGuid().ToString("N").Substring(0, 8))

if (-not (Test-Path $LiveWallpaper)) {
    throw "Missing live-wallpaper.html at $LiveWallpaper"
}
if (-not (Test-Path $VersionFile)) {
    throw "Missing version_info.txt at $VersionFile"
}
if ($Installer -and $OneDir) {
    throw "-Installer packages the default single-file executable. Omit -OneDir."
}

$PyCmd = Get-Command python -ErrorAction SilentlyContinue
if ($PyCmd) {
    $PyExe = $PyCmd.Source
    $PyPrefix = @()
} else {
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $PyLauncher) {
        throw "Python 3 was not found in PATH."
    }
    $PyExe = $PyLauncher.Source
    $PyPrefix = @("-3")
}

Write-Host "=== Building IDE-Lyrics-Studio.exe ===" -ForegroundColor Cyan

try {
    # 1. Generate multi-resolution .ico from the Pillow tray icon renderer
    Write-Host "[1/3] Generating app icon ($IconPath)..." -ForegroundColor Cyan
    $iconScript = @'
import sys
script_dir, icon_path = sys.argv[1], sys.argv[2]
sys.path.insert(0, script_dir)
import app
img = app.create_tray_icon_image(256)
img.save(icon_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("Icon written:", icon_path)
'@
    $iconArgs = @($PyPrefix) + @("-", $ScriptDir, $IconPath)
    $iconScript | & $PyExe @iconArgs
    if ($LASTEXITCODE -ne 0) { throw "Failed to generate app icon." }

    # 2. Ensure PyInstaller is available
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $verArgs = @($PyPrefix) + @("-m", "PyInstaller", "--version")
    & $PyExe @verArgs *> $null
    $hasPyInstaller = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prevEap

    if (-not $hasPyInstaller) {
        Write-Host "[..] Installing PyInstaller..." -ForegroundColor Yellow
        $pipArgs = @($PyPrefix) + @("-m", "pip", "install", "pyinstaller")
        & $PyExe @pipArgs
        if ($LASTEXITCODE -ne 0) { throw "Failed to install PyInstaller." }
    }

    # 3. Build standalone Windows .exe (build scratch stays in %TEMP% to avoid OneDrive file locks)
    Write-Host "[2/3] Running PyInstaller..." -ForegroundColor Cyan
    $modeFlag = if ($OneDir) { "--onedir" } else { "--onefile" }
    $pyArgs = @($PyPrefix) + @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        $modeFlag,
        "--windowed",
        "--name", "IDE-Lyrics-Studio",
        "--icon", $IconPath,
        "--version-file", $VersionFile,
        "--distpath", $DistDir,
        "--workpath", $BuildDir,
        "--specpath", $BuildDir,
        "--paths", $ScriptDir,
        "--add-data", "$LiveWallpaper;.",
        "--hidden-import", "smtc_bridge",
        "--hidden-import", "wallpaper_host",
        "--hidden-import", "integrations",
        "--hidden-import", "pystray._win32",
        "--hidden-import", "winrt.windows.media.control",
        "--hidden-import", "winrt.windows.storage.streams",
        "--hidden-import", "winrt.windows.foundation",
        "--hidden-import", "winrt.windows.foundation.collections",
        "--collect-submodules", "webview",
        "--collect-data", "webview",
        $AppPy
    )

    & $PyExe @pyArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed with exit code $LASTEXITCODE" }

    $exeOut = if ($OneDir) {
        Join-Path (Join-Path $DistDir "IDE-Lyrics-Studio") "IDE-Lyrics-Studio.exe"
    } else {
        Join-Path $DistDir "IDE-Lyrics-Studio.exe"
    }

    if (-not (Test-Path $exeOut)) {
        throw "Expected output executable not found at $exeOut"
    }

    $exeItem = Get-Item $exeOut
    $sizeMB = [math]::Round($exeItem.Length / 1MB, 2)
    $verInfo = $exeItem.VersionInfo
    Write-Host "[3/3] Build complete!" -ForegroundColor Green
    Write-Host "      Output : $exeOut ($sizeMB MB)" -ForegroundColor Green
    Write-Host "      Meta   : $($verInfo.ProductName) $($verInfo.FileVersion)" -ForegroundColor Green

    if ($Installer) {
        if (-not (Test-Path $InstallerScript)) {
            throw "Missing installer.iss at $InstallerScript"
        }
        $iscc = Get-Command iscc -ErrorAction SilentlyContinue
        $isccPath = if ($iscc) {
            $iscc.Source
        } else {
            @(
                (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
                (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
                (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
            ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
        }
        if (-not $isccPath) {
            throw "Inno Setup 6 (ISCC.exe) not found. Install it with: winget install JRSoftware.InnoSetup"
        }
        Write-Host "[+] Compiling Windows installer ($InstallerScript)..." -ForegroundColor Cyan
        & $isccPath $InstallerScript
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup build failed with exit code $LASTEXITCODE" }
    }
} finally {
    if (Test-Path $BuildDir) {
        Remove-Item -Recurse -Force $BuildDir -ErrorAction SilentlyContinue
    }
}
