@echo off
REM ============================================================================
REM Trading Agent — double-click shutdown (Windows).
REM
REM Stops the Postgres + Redis containers. Data is preserved.
REM ============================================================================

setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1"

echo.
echo Press any key to close...
pause >nul
