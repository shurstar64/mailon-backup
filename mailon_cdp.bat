@echo off
REM mailon-backup command wrapper with CDP mode
REM Usage: mailon_cdp.bat <command> [args...]
REM   Examples:
REM     mailon_cdp.bat totp
REM     mailon_cdp.bat login
REM     mailon_cdp.bat status
REM     mailon_cdp.bat sync --limit 10

setlocal enabledelayedexpansion

if "%1"=="" (
    echo Usage: mailon_cdp.bat ^<command^> [args...]
    echo.
    echo Commands:
    echo   totp     - Generate TOTP code
    echo   login    - Test login
    echo   status   - Show sync status
    echo   sync     - Sync mails (use --limit N)
    echo   probe    - Dump current page for debugging
    exit /b 1
)

set CDP_PORT=9222
set CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
set USER_DATA_DIR=%USERPROFILE%\.agent-browser\mailon-sync-profile

REM Check if Chrome is already running with CDP
curl -s http://localhost:%CDP_PORT%/json/version >nul 2>&1
if errorlevel 1 (
    echo [mailon] Starting Chrome with CDP on port %CDP_PORT%...
    start "" "%CHROME_PATH%" --remote-debugging-port=%CDP_PORT% --user-data-dir="%USER_DATA_DIR%" --no-first-run --no-default-browser-check "about:blank"

    :wait_chrome
    timeout /t 1 /nobreak >nul
    curl -s http://localhost:%CDP_PORT%/json/version >nul 2>&1
    if errorlevel 1 goto wait_chrome
    echo [mailon] Chrome ready.
) else (
    echo [mailon] Chrome already running on port %CDP_PORT%.
)

REM Run command
cd /d "%~dp0"
set AGENT_BROWSER_CDP_PORT=%CDP_PORT%
python -m mailon.main %*

endlocal
