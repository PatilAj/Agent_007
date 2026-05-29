# =============================================================================
# Telegram bot launcher.
#
# Starts the bot as a long-running foreground process. The bot:
#   - Listens for Telegram slash commands (/status, /pnl, /kill, /start_agent, ...)
#   - Forwards stream:notifications events to your Telegram chat
#   - Can spawn/stop the ingestor as a subprocess when /start_agent is sent
#
# Usage:  double-click  start_bot.bat   OR   .\scripts\run_bot.ps1
#
# Requires:
#   - TELEGRAM_BOT_TOKEN set in .env
#   - TELEGRAM_CHAT_ID   set in .env
#   - Docker (Postgres + Redis) running — start_agent.bat handles that
# =============================================================================

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$dockerBin = "C:\Program Files\Docker\Docker\resources\bin"
if ((Test-Path $dockerBin) -and (-not ($env:PATH -like "*$dockerBin*"))) {
    $env:PATH = "$dockerBin;$env:PATH"
}
$env:PYTHONIOENCODING = "utf-8"

function Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

# --- 1. Ensure Docker stack is up (bot needs Redis) -------------------------

Step "Ensuring Postgres + Redis are running"
$dockerProc = Get-Process "Docker Desktop" -ErrorAction SilentlyContinue
if (-not $dockerProc) {
    $dockerExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dockerExe) {
        Write-Host "Starting Docker Desktop..."
        Start-Process $dockerExe
        for ($i = 0; $i -lt 60; $i++) {
            try { docker info *> $null; if ($LASTEXITCODE -eq 0) { break } } catch {}
            Start-Sleep -Seconds 2
        }
    }
}
# PS 5.1 treats native command stderr as an error record when
# ErrorActionPreference=Stop, even though docker compose's "Container ...
# Running" lines are informational. Suppress here without aborting.
$prevPref = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & cmd /c "docker compose up -d 1>nul 2>nul"
} catch {}
$ErrorActionPreference = $prevPref
Start-Sleep -Seconds 2

# --- 2. Start the bot -------------------------------------------------------

Step "Starting Telegram bot (Ctrl+C to stop)"
Write-Host "    - Read-only:  /status /signals /trades /pnl /health /help"
Write-Host "    - Control:    /kill /unkill /start_agent /stop_agent"
Write-Host ""
python -X utf8 -m src.workers.telegram_bot
