@echo off
REM ============================================================================
REM Trading Agent — double-click launcher (Windows).
REM
REM Runs scripts\run.ps1 which orchestrates Docker, DB migrations, Kite login,
REM instrument catalog refresh, and finally starts the ingestor.
REM ============================================================================

setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1"

echo.
echo Press any key to close...
pause >nul
