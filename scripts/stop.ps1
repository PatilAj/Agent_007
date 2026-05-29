# =============================================================================
# Trading Agent - clean shutdown.
#
# 1. Detects any running ingestor (python -m src.workers.ingestor) and
#    terminates it cleanly first.
# 2. Then docker compose down to stop Postgres + Redis.
#
# Volumes (and therefore the DB and instrument catalog) are preserved, so
# the next start_agent.bat picks up right where you left off.
#
# Usage:  double-click  stop_agent.bat   OR
#         .\scripts\stop.ps1   from PowerShell
# =============================================================================

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$dockerBin = "C:\Program Files\Docker\Docker\resources\bin"
if ((Test-Path $dockerBin) -and (-not ($env:PATH -like "*$dockerBin*"))) {
    $env:PATH = "$dockerBin;$env:PATH"
}

function Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

# --- 1. find + kill any running ingestor -------------------------------------

Step "Looking for a running ingestor process"
$ingestorProcs = @()
try {
    $ingestorProcs = Get-CimInstance Win32_Process -ErrorAction Stop |
        Where-Object { $_.CommandLine -and ($_.CommandLine -match "src\.workers\.ingestor") }
} catch {
    Write-Host "(Could not query processes via CIM, skipping ingestor cleanup.)" -ForegroundColor Yellow
}

if ($ingestorProcs) {
    foreach ($p in $ingestorProcs) {
        Write-Host "Stopping ingestor PID $($p.ProcessId)..."
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        } catch {
            Write-Host "  (could not stop PID $($p.ProcessId): $_)" -ForegroundColor Yellow
        }
    }
    Write-Host "Giving Postgres a moment to roll back any open transactions..."
    Start-Sleep -Seconds 3
} else {
    Write-Host "No running ingestor found - safe to shut down Docker."
}

# --- 2. docker compose down --------------------------------------------------

Step "Stopping containers (data preserved in volumes)"
docker compose down
Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host ""
Write-Host "Data is preserved in the named volumes. Next start_agent.bat reuses everything."
Write-Host "To truly wipe the DB you would have to run 'docker compose down -v' - do NOT do that"
Write-Host "unless you really mean it." -ForegroundColor Yellow
