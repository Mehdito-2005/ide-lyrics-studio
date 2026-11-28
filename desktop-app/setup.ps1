#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$SkipUpgrade
)

$ErrorActionPreference = "Stop"

Write-Host "=== IDE Lyrics Desktop App Setup ===" -ForegroundColor Cyan

# 1. Locate Python 3
$pythonCmd = $null
$pythonArgs = @()

if (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonCmd = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonCmd = "py"
    $pythonArgs = @("-3")
} else {
    Write-Error "Python 3 was not found in PATH. Please install Python 3.10+ (recommended 3.13) from https://www.python.org/downloads/."
    exit 1
}

$versionOutput = & $pythonCmd @pythonArgs -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'); sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Python 3.9+ is required. Detected: $versionOutput"
    exit 1
}
Write-Host "[OK] Found Python $versionOutput" -ForegroundColor Green

# 2. Install requirements.txt
$reqFile = Join-Path $PSScriptRoot "requirements.txt"
if (-not (Test-Path $reqFile)) {
    Write-Error "requirements.txt not found at: $reqFile"
    exit 1
}

Write-Host "[..] Installing Python packages from $reqFile ..." -ForegroundColor Yellow
$pipArgs = @("-m", "pip", "install")
if (-not $SkipUpgrade) {
    $pipArgs += "--upgrade"
}
$pipArgs += @("-r", $reqFile)

& $pythonCmd @pythonArgs @pipArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "pip install failed with exit code $LASTEXITCODE."
    exit 1
}
Write-Host "[OK] Packages installed." -ForegroundColor Green

# 3. Verify core imports cleanly
Write-Host "[..] Verifying Python imports ..." -ForegroundColor Yellow
$verifyCode = @'
import sys
import importlib

required = [
    "webview",
    "pystray",
    "PIL",
    "PIL.Image",
    "winrt.windows.media.control",
    "winrt.windows.storage.streams",
]

failed = []
for mod in required:
    try:
        importlib.import_module(mod)
        print(f"  [OK] {mod}")
    except Exception as exc:
        print(f"  [FAIL] {mod}: {exc}", file=sys.stderr)
        failed.append(mod)

if failed:
    sys.exit(1)
'@

$verifyCode | & $pythonCmd @pythonArgs -
if ($LASTEXITCODE -ne 0) {
    Write-Error "One or more required Python modules failed to import."
    exit 1
}

Write-Host "=== Setup complete! All dependencies verified. ===" -ForegroundColor Green
exit 0
