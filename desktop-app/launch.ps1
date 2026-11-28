param(
    [ValidateSet("", "app", "wallpaper", "overlay")]
    [string]$Mode = "",

    [switch]$Choose,

    [switch]$Show,

    [switch]$Console
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppPy = Join-Path $ScriptDir "app.py"
$SetupPs1 = Join-Path $ScriptDir "setup.ps1"

function Resolve-PythonCommand {
    $pyCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pyCmd) {
        $pywPath = Join-Path (Split-Path -Parent $pyCmd.Source) "pythonw.exe"
        if (-not (Test-Path $pywPath)) {
            $pywCmd = Get-Command pythonw -ErrorAction SilentlyContinue
            $pywPath = if ($pywCmd) { $pywCmd.Source } else { $null }
        }
        return @{ Exe = $pyCmd.Source; WindowlessExe = $pywPath; Prefix = @() }
    }
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $pywLauncher = Get-Command pyw -ErrorAction SilentlyContinue
        $pywPath = if ($pywLauncher) { $pywLauncher.Source } else { $null }
        return @{ Exe = $pyLauncher.Source; WindowlessExe = $pywPath; Prefix = @("-3") }
    }
    return $null
}

$PythonInfo = Resolve-PythonCommand
if (-not $PythonInfo) {
    Write-Host "[ERROR] Python 3 was not found in PATH." -ForegroundColor Red
    Write-Host "Install Python 3.10+ from https://www.python.org/downloads/ and check 'Add Python to PATH'." -ForegroundColor Yellow
    exit 1
}

$PyExe = $PythonInfo.Exe
$PywExe = $PythonInfo.WindowlessExe
$PyPrefix = @($PythonInfo.Prefix)

# Auto-run setup.ps1 if pywebview is missing
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$checkArgs = @($PyPrefix) + @("-c", "import webview")
& $PyExe @checkArgs 2>$null
$hasWebview = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prevEap
if (-not $hasWebview) {
    Write-Host "[IDE Lyrics] pywebview not detected. Running setup.ps1..." -ForegroundColor Cyan
    if (-not (Test-Path $SetupPs1)) {
        Write-Host "[ERROR] setup.ps1 not found at $SetupPs1" -ForegroundColor Red
        exit 1
    }
    & powershell -NoProfile -ExecutionPolicy Bypass -File $SetupPs1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] setup.ps1 failed with exit code $LASTEXITCODE" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

$appArgs = @($PyPrefix) + @($AppPy)
if ($Mode -ne "") {
    $appArgs += @("--mode", $Mode)
}
if ($Choose) {
    $appArgs += "--choose"
}
if ($Show) {
    $appArgs += "--show"
}

if ($Console) {
    Write-Host "[IDE Lyrics] Launching IDE Lyrics Desktop App (console attached)..." -ForegroundColor Green
    & $PyExe @appArgs
    exit $LASTEXITCODE
}

$launchExe = if ($PywExe) { $PywExe } else { $PyExe }
$quotedArgs = ($appArgs | ForEach-Object {
    if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
}) -join ' '

Start-Process -FilePath $launchExe -ArgumentList $quotedArgs -WorkingDirectory $ScriptDir -WindowStyle Hidden
exit 0
