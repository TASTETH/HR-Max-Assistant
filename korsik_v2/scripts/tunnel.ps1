# Запуск Cloudflare-туннеля (токен из .env) и подписка MAX на https://<домен>/webhook/max
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
docker compose --profile tunnel up -d cloudflared
Start-Sleep 5
$base = (Select-String -Path .env -Pattern '^PUBLIC_BASE_URL=(.+)$').Matches[0].Groups[1].Value.Trim()
try { $r = Invoke-WebRequest "$base/health" -UseBasicParsing -TimeoutSec 10; Write-Host "Публичный адрес отвечает: $($r.Content)" -ForegroundColor Green }
catch { Write-Host "Публичный адрес $base/health не отвечает. Проверьте Public Hostname туннеля в Cloudflare (service http://app:8000)." -ForegroundColor Yellow }
docker compose exec app python -m korsik.cli subscribe-webhook
docker compose exec app python -m korsik.cli subscriptions
