@echo off
setlocal
REM Launch silently into the System Tray with zero lingering console window.
where pythonw >nul 2>&1
if %ERRORLEVEL%==0 (
    python -c "import webview" >nul 2>&1
    if %ERRORLEVEL%==0 (
        start "" /D "%~dp0" pythonw "%~dp0app.py" %*
        exit /b 0
    )
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [IDE Lyrics] Exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
