@echo off
REM ============================================================================
REM Trading Agent - Telegram bot launcher (Windows double-click).
REM
REM Run THIS once after logging in, leave the window open in the background,
REM and you can control the agent from your phone via Telegram for the rest
REM of the day.
REM ============================================================================

setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_bot.ps1"

echo.
echo Bot stopped. Press any key to close...
pause >nul
