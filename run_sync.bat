@echo off
REM ---------------------------------------------------------------
REM  mailon.kr hourly sync - invoked by Windows Task Scheduler.
REM
REM  This script:
REM    1. CDs to the project directory (so relative paths work)
REM    2. Ensures agent-browser and Node are on PATH (npm global bin)
REM    3. Runs the sync command via the Python venv
REM    4. Propagates exit code so Task Scheduler sees success/failure
REM    5. Prunes logs older than 30 days
REM ---------------------------------------------------------------

setlocal
set PROJECT_DIR=%~dp0
cd /d "%PROJECT_DIR%"

REM --- PATH setup -------------------------------------------------
REM Task Scheduler may spawn us with a minimal PATH. Ensure the npm
REM global bin (where agent-browser lives) and Node.js are available.
set PATH=%APPDATA%\npm;C:\Program Files\nodejs;%PATH%

REM --- Sanity checks ----------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python venv missing. Run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt 1>&2
    exit /b 10
)

where agent-browser >nul 2>&1
if errorlevel 1 (
    echo [ERROR] agent-browser not found on PATH. Install: npm i -g agent-browser 1>&2
    exit /b 11
)

REM --- Run ---------------------------------------------------------
".venv\Scripts\python.exe" -m mailon.main sync
set EXITCODE=%ERRORLEVEL%

REM --- Housekeeping: prune logs older than 30 days -----------------
REM (silent; errors ignored)
forfiles /P "%PROJECT_DIR%logs" /M sync-*.log /D -30 /C "cmd /c del @path" >nul 2>&1

endlocal & exit /b %EXITCODE%
