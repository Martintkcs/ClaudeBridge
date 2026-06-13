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
    Write-Host "Telepites: winget install --id Tailscale.Tailscale -e"
    exit 1
}

if (-not (Test-Path $configPath)) {
    python -c "import app; print(app.CONFIG['token'])" | Out-Null
}

$token = (Get-Content $configPath | ConvertFrom-Json).token

Write-Host "Tailscale Serve beallitasa a helyi http://127.0.0.1:$port appra..." -ForegroundColor Green
try {
    & $tailscaleExe serve reset
    & $tailscaleExe serve --yes --http 80 --bg $port
    Write-Host ""
    & $tailscaleExe serve status
} catch {
    Write-Host ""
    Write-Host "Nem sikerult elerni a Tailscale helyi vezerleset." -ForegroundColor Yellow
    Write-Host "Nyiss egy Administrator PowerShellt, es futtasd ujra:"
    Write-Host "  .\setup-tailscale-serve.ps1"
    throw
}

Write-Host ""
Write-Host "Ha a Tailscale Serve sikerult, telefonon ezt a format hasznald:"
Write-Host "  http://GEPNEV.tailnet-nev.ts.net/"
Write-Host "A tokent az elso megnyitaskor a bejelentkezo mezobe masold:"
Write-Host "  $token"
Write-Host ""
Write-Host "A Bridge appot kozben kulon futtatni kell:"
Write-Host "  .\start.ps1"
