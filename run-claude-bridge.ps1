param(
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$port = 8765
$configPath = Join-Path $projectRoot "config.json"

function Write-Step {
    param([string]$Message)
    Write-Host "[Claude Bridge] $Message" -ForegroundColor Cyan
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[Claude Bridge] $Message" -ForegroundColor Yellow
}

function Find-Tailscale {
    $cmd = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $candidate = "C:\Program Files\Tailscale\tailscale.exe"
    if (Test-Path $candidate) {
        return $candidate
    }

    return $null
}

Set-Location $projectRoot

Write-Step "Preparing local config..."
if (-not (Test-Path $configPath)) {
    python -c "import app; print(app.CONFIG['token'])" | Out-Null
}

$token = (Get-Content $configPath | ConvertFrom-Json).token
$tailscaleExe = Find-Tailscale

if ($tailscaleExe) {
    Write-Step "Checking Tailscale..."
    try {
        $service = Get-Service -Name Tailscale -ErrorAction SilentlyContinue
        if ($service -and $service.Status -ne "Running") {
            Write-Step "Starting Tailscale service..."
            Start-Service -Name Tailscale
            Start-Sleep -Seconds 2
        }
    } catch {
        Write-Warn "Could not start the Tailscale service automatically: $($_.Exception.Message)"
    }

    Write-Step "Configuring Tailscale Serve for port $port..."
    try {
        $serveOutput = & $tailscaleExe serve --yes --http 80 --bg $port 2>&1
        $serveExitCode = $LASTEXITCODE
        if ($serveOutput) {
            $serveOutput | Out-Host
        }
        if ($serveExitCode -ne 0) {
            throw ($serveOutput -join [Environment]::NewLine)
        }
        Write-Step "Tailscale Serve is pointing to http://127.0.0.1:$port/"
    } catch {
        Write-Warn "Tailscale Serve could not be configured from this shell."
        if ($_.Exception.Message) {
            Write-Warn $_.Exception.Message
        }
        Write-Host "Run this once from an Administrator PowerShell if remote access does not work:"
        Write-Host "  .\setup-tailscale-serve.cmd"
    }
} else {
    Write-Warn "Tailscale was not found. Local access will still work."
    Write-Host "Install Tailscale when you want mobile/remote access:"
    Write-Host "  winget install --id Tailscale.Tailscale -e"
}

Write-Host ""
Write-Host "Open locally:"
Write-Host "  http://127.0.0.1:$port/"
Write-Host ""
Write-Host "Open from your phone through Tailscale:"
Write-Host "  http://YOUR-MACHINE.YOUR-TAILNET.ts.net/"
Write-Host ""
Write-Host "Login token:"
Write-Host "  $token"
Write-Host ""

if ($NoStart) {
    Write-Step "Setup check finished. The app was not started because -NoStart was used."
    exit 0
}

Write-Step "Starting Claude Bridge..."
python -u "$projectRoot\app.py" --host 0.0.0.0 --port $port
