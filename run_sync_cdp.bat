@echo off
REM mailon-backup sync script with CDP mode
REM Usage: run_sync_cdp.bat [limit]
REM   limit - optional, number of mails to sync (default: 50)

setlocal enabledelayedexpansion

set LIMIT=%1
if "%LIMIT%"=="" set LIMIT=50

set CDP_PORT=9222
set CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
set USER_DATA_DIR=%USERPROFILE%\.agent-browser\mailon-sync-profile

echo [mailon-backup] Starting Chrome with CDP on port %CDP_PORT%...

REM Kill any existing Chrome on this port
taskkill /F /IM chrome.exe >nul 2>&1

REM Start Chrome with remote debugging
start "" "%CHROME_PATH%" --remote-debugging-port=%CDP_PORT% --user-data-dir="%USER_DATA_DIR%" --no-first-run --no-default-browser-check "about:blank"

REM Wait for Chrome to start
echo [mailon-backup] Waiting for Chrome to be ready...
:wait_chrome
timeout /t 2 /nobreak >nul
curl -s http://localhost:%CDP_PORT%/json/version >nul 2>&1
if errorlevel 1 goto wait_chrome

echo [mailon-backup] Chrome ready. Running sync with limit=%LIMIT%...

REM Run sync
cd /d "%~dp0"
set AGENT_BROWSER_CDP_PORT=%CDP_PORT%
python -m mailon.main sync --limit %LIMIT%

echo.
echo [mailon-backup] Sync complete. Checking status...
python -m mailon.main status

echo.
echo [mailon-backup] Done. Chrome is still running for inspection.
echo [mailon-backup] Close Chrome manually when finished.

endlocal
