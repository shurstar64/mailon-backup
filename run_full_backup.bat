@echo off
REM ---------------------------------------------------------------
REM  One-shot FULL backup of all inbox mails.
REM  Unlike run_sync.bat (hourly cron, 30-min limit), this script
REM  runs until completion and is intended for manual execution.
REM
REM  Usage (from any terminal):
REM    run_full_backup.bat
REM
REM  Output:
REM    logs\full-backup-YYYYMMDD-HHMMSS.log
REM ---------------------------------------------------------------

setlocal
set PROJECT_DIR=%~dp0
cd /d "%PROJECT_DIR%"

REM PATH for agent-browser / Node
set PATH=%APPDATA%\npm;C:\Program Files\nodejs;%PATH%

REM venv check
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python venv missing 1>&2
    exit /b 10
)

REM Timestamped log via PowerShell (more reliable than wmic locale parsing).
REM Python's stdout also gets captured here; module logger ALSO writes to
REM logs/sync-YYYY-MM-DD.log internally - this file is the full stdout/stderr.
for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "Get-Date -Format 'yyyyMMdd-HHmmss'"`) do set TS=%%T
set LOG=logs\full-backup-%TS%.log

echo Starting full backup; log: %LOG%
echo Start: %date% %time% > "%LOG%"

".venv\Scripts\python.exe" -u -m mailon.main sync >> "%LOG%" 2>&1
set EXITCODE=%ERRORLEVEL%

echo End: %date% %time% (exit %EXITCODE%) >> "%LOG%"
echo Finished with exit code %EXITCODE%; see %LOG%
echo Also see logs\sync-YYYY-MM-DD.log for structured module logs

endlocal & exit /b %EXITCODE%
