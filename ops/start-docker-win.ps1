$ErrorActionPreference = "Stop"

# 脚本在 ops/，项目根是 ops/ 的父目录
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "data") | Out-Null

function Convert-ToDockerPath {
    param([Parameter(Mandatory=$true)][string]$Path)
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    return $resolved -replace "\\", "/"
}

Write-Host ""
Write-Host "  ========================================"
Write-Host "   Buddy 2 API Docker for Windows"
Write-Host "  ========================================"
Write-Host ""

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "Docker was not found. Please start Docker Desktop first."
}

$defaultAuthDir = Join-Path $env:LOCALAPPDATA "CodeBuddyExtension\Data\Public\auth"
$authDir = if ($env:CB_HOST_AUTH_DIR) { $env:CB_HOST_AUTH_DIR } else { $defaultAuthDir }

$composeFiles = @("-f", "ops/docker-compose.yml")
if (Test-Path -LiteralPath $authDir -PathType Container) {
    $dockerAuthDir = Convert-ToDockerPath $authDir
    $env:CB_HOST_AUTH_DIR = $dockerAuthDir
    $composeFiles += @("-f", "ops/docker-compose.windows.yml")
    Write-Host "  [auth] $authDir"
    Write-Host "  [mount] $dockerAuthDir -> /auth:ro"
} else {
    Write-Host "  [hint] WorkBuddy auth directory was not found: $authDir" -ForegroundColor Yellow
    Write-Host "  Starting without the Windows auth overlay. Import accounts from the UI after login." -ForegroundColor Yellow
}

if (-not $env:CB_GATEWAY_ADMIN_TOKEN) {
    $env:CB_GATEWAY_ADMIN_TOKEN = "cb-admin-$([guid]::NewGuid().ToString('N'))"
    Write-Host "  [security] Generated a temporary admin token for this session." -ForegroundColor Green
}

Write-Host "  [start] http://127.0.0.1:8787"
Write-Host ""

docker compose @composeFiles up -d --build

Write-Host ""
Write-Host "  Started. Open http://127.0.0.1:8787, pick a channel on Accounts, then detect/import."
Write-Host "  QClaw / QwenWork Windows logins cannot be read inside Linux Docker; use python -m gateway.server for those."
