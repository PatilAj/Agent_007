# =============================================================================
# Trading Agent - one-command launcher.
#
# Usage:  double-click  start_agent.bat   (at project root)   OR
#         from PowerShell at the project root:  .\scripts\run.ps1
#
# Steps (each one is idempotent - safe to re-run):
#   1. Make sure Docker Desktop is running
#   2. docker compose up -d  (Postgres + Redis)
#   3. Wait for Postgres to report healthy
#   4. alembic upgrade head  (no-op if schema already current)
#   5. If no active Kite token in DB - run token_refresh (prompts for OTP)
#   6. If instrument catalog is empty - refresh it (~30s, no OTP needed)
#   7. Start the ingestor (foreground - Ctrl+C to stop)
# =============================================================================

$ErrorActionPreference = "Stop"

# Move to project root regardless of where the script was invoked from
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# --- environment setup --------------------------------------------------------

$dockerBin = "C:\Program Files\Docker\Docker\resources\bin"
if ((Test-Path $dockerBin) -and (-not ($env:PATH -like "*$dockerBin*"))) {
    $env:PATH = "$dockerBin;$env:PATH"
}
$env:PYTHONIOENCODING = "utf-8"

function Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Fail($msg) {
    Write-Host ""
    Write-Host "FAILED: $msg" -ForegroundColor Red
    Write-Host "Press any key to close..."
    [void][System.Console]::ReadKey($true)
    exit 1
}

# --- 1. Docker Desktop --------------------------------------------------------

Step "Checking Docker Desktop"
$dockerProc = Get-Process "Docker Desktop" -ErrorAction SilentlyContinue
if (-not $dockerProc) {
    Write-Host "Docker Desktop is not running. Starting it now..."
    $dockerExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path $dockerExe)) {
        Fail "Docker Desktop not found at $dockerExe. Install it first."
    }
    Start-Process $dockerExe

    Write-Host "Waiting for the Docker engine to be ready (can take 30-90 s)..."
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        try { docker info *> $null; if ($LASTEXITCODE -eq 0) { $ready = $true; break } } catch {}
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { Fail "Docker engine did not become ready within 2 minutes." }
}
Write-Host "Docker engine is up."

# --- 2. compose up ------------------------------------------------------------

Step "Bringing up Postgres + Redis (docker compose up -d)"
docker compose up -d
if ($LASTEXITCODE -ne 0) { Fail "docker compose up failed." }

# --- 3. wait for postgres healthy --------------------------------------------

Step "Waiting for Postgres to be healthy"
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    $h = docker inspect --format='{{.State.Health.Status}}' trading_agent_pg 2>$null
    if ($h -eq "healthy") { $healthy = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $healthy) { Fail "Postgres did not become healthy within 30s. Check 'docker compose logs postgres'." }
Write-Host "Postgres is healthy."

# --- 4. alembic upgrade ------------------------------------------------------

Step "Applying database migrations (idempotent)"
python -X utf8 -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { Fail "alembic upgrade failed." }

# --- 5. token refresh (only if no active token) ------------------------------

Step "Checking Kite session token"
$tokenStatus = & python -X utf8 scripts/_check_token.py 2>$null
if ($tokenStatus -eq "YES") {
    Write-Host "Active Kite token found - skipping login."
} else {
    Write-Host "No active token. Running login (you will be prompted for the 6-digit OTP)..."
    python -X utf8 -m src.workers.token_refresh
    if ($LASTEXITCODE -ne 0) { Fail "Kite login failed. Check the OTP code or your .env credentials." }
}

# --- 6. instrument catalog (only if empty/sparse) ----------------------------

Step "Checking instrument catalog"
$instrCount = & python -X utf8 scripts/_check_instruments.py 2>$null
$n = 0
[int]::TryParse($instrCount, [ref]$n) | Out-Null
if ($n -lt 1000) {
    Write-Host "Instrument catalog has only $n rows - refreshing from Kite (~30s)..."
    python -X utf8 -m src.workers.refresh_instruments
    if ($LASTEXITCODE -ne 0) { Fail "Instrument refresh failed." }
} else {
    Write-Host "$n instruments already loaded - skipping refresh."
}

# --- 7. ingestor -------------------------------------------------------------

Step "Starting the ingestor (Ctrl+C to stop)"
Write-Host "    - WSS will subscribe to NIFTY 50, NIFTY BANK, NIFTY FIN SERVICE"
Write-Host "    - Strategy ema_regime_v2 is active (paper mode)"
Write-Host "    - Press Ctrl+C any time to stop cleanly"
Write-Host ""
python -X utf8 -m src.workers.ingestor

# When the ingestor exits, leave Docker containers running for fast restart.
Write-Host ""
Write-Host "Ingestor stopped. Docker containers are still running so the next run starts quickly."
Write-Host "Run stop_agent.bat (or .\scripts\stop.ps1) when you are truly done for the day."
