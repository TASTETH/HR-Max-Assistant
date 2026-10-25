# Обёртка CLI:  scripts\korsik.ps1 search "можно ли дарить деньги"
Set-Location (Split-Path $PSScriptRoot -Parent)
docker compose exec app python -m korsik.cli @args
