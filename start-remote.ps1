$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$port = 8765
$configPath = Join-Path $projectRoot "config.json"
$tailscaleExe = (Get-Command tailscale -ErrorAction SilentlyContinue).Source
if (-not $tailscaleExe) {
    $candidate = "C:\Program Files\Tailscale\tailscale.exe"
    if (Test-Path $candidate) {
        $tailscaleExe = $candidate
    }
}

Set-Location $projectRoot

if (-not $tailscaleExe) {
    Write-Host "Tailscale nincs telepitve vagy nincs a PATH-ban." -ForegroundColor Yellow
    Write-Host "Telepites utan futtasd: tailscale up"
    Write-Host "Majd inditsd ujra ezt a scriptet."
    exit 1
}

$tailscaleIp = (& $tailscaleExe ip -4 2>$null | Select-Object -First 1)
if (-not $tailscaleIp) {
    Write-Host "Tailscale meg nincs bejelentkeztetve ezen a gepen." -ForegroundColor Yellow
    Write-Host "Futtasd: `"$tailscaleExe`" up"
    exit 1
}

if (-not (Test-Path $configPath)) {
    python -c "import app; print(app.CONFIG['token'])" | Out-Null
}

$token = (Get-Content $configPath | ConvertFrom-Json).token

Write-Host ""
Write-Host "Claude Bridge inditas..." -ForegroundColor Green
Write-Host "Helyi URL:           http://127.0.0.1:$port/"
Write-Host "Tailscale direkt IP: http://$tailscaleIp`:$port/"
Write-Host "Token:               $token"
Write-Host ""
Write-Host "Biztonsagosabb tavoli mod: .\setup-tailscale-serve.ps1"
Write-Host "A direkt IP modhoz Windows tuzfal engedely is kellhet, ezert alapbol a Serve ajanlott."
Write-Host ""

python -u "$projectRoot\app.py" --host 0.0.0.0 --port $port
