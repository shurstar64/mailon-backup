@echo off
REM Monitor the currently-running backup process.
REM Shows: process status + DB stats + log tail.
REM Refreshes every 10 seconds. Ctrl+C to exit.

setlocal
cd /d "%~dp0"

:loop
cls
echo ============================================================
echo  mailon.kr Backup Monitor   %date% %time%
echo ============================================================
echo.
echo [Python processes]
tasklist /FI "IMAGENAME eq python.exe"         2>nul | findstr /V "INFO:"
tasklist /FI "IMAGENAME eq python3.13.exe"     2>nul | findstr /V "INFO:"
echo.
echo [DB Status]
".venv\Scripts\python.exe" -m mailon.main status 2>&1
echo.
echo [Recent log lines]
powershell -NoProfile -Command "Get-Content logs\sync-*.log -Tail 8 -Encoding UTF8" 2>&1
echo.
echo ---- refreshing in 10s ... Ctrl+C to quit ----
timeout /t 10 /nobreak >nul
goto loop
