$ErrorActionPreference = "Stop"

$tailscaleExe = (Get-Command tailscale -ErrorAction SilentlyContinue).Source
if (-not $tailscaleExe) {
    $candidate = "C:\Program Files\Tailscale\tailscale.exe"
    if (Test-Path $candidate) {
        $tailscaleExe = $candidate
    }
}

if (-not $tailscaleExe) {
    Write-Host "Tailscale nincs telepitve." -ForegroundColor Yellow
    Write-Host "Telepites: winget install --id Tailscale.Tailscale -e"
    exit 1
}

Write-Host "Tailscale bejelentkeztetes inditasa..." -ForegroundColor Green
& $tailscaleExe up
