# Корсик: первичная установка на Windows (Docker Desktop + Ollama на хосте).
# Запуск из PowerShell в папке проекта:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Установите и запустите Docker Desktop" }
if (-not (Test-Path .env)) { Copy-Item .env.example .env; Write-Host "Создан .env из шаблона — заполните ключи и запустите скрипт снова." -ForegroundColor Yellow; exit 1 }

Write-Host "1/5 Сборка и запуск PostgreSQL, API, worker..." -ForegroundColor Cyan
docker compose up -d --build db app worker
$ok = $false
for ($i = 0; $i -lt 90; $i++) {
  try { if ((Invoke-WebRequest http://127.0.0.1:8000/health -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { $ok = $true; break } } catch {}
  Start-Sleep 2
}
if (-not $ok) { docker compose logs app --tail 50; throw "API не поднялся" }

Write-Host "2/5 Модели Ollama..." -ForegroundColor Cyan
$bm25 = ""
if (Get-Command ollama -ErrorAction SilentlyContinue) {
  ollama pull bge-m3
  $models = ollama list
  if (-not ($models -match "poliplastik-qwen")) { Write-Host "Модель poliplastik-qwen:latest не найдена. Укажите OLLAMA_MODEL в .env (например qwen2.5:7b-instruct) и выполните: ollama pull <модель>" -ForegroundColor Yellow }
} else {
  Write-Host "Ollama не найдена: корпус будет собран только по BM25, ответы Qwen недоступны до установки Ollama." -ForegroundColor Yellow
  $bm25 = "--bm25-only"
}

Write-Host "3/5 Единственный HR (введите пароль ≥10 символов)..." -ForegroundColor Cyan
docker compose exec app python -m korsik.cli create-hr --login hr --name "HR-специалист"

Write-Host "4/5 Корпоративные регламенты (OCR уже в кэше seed\extract_cache)..." -ForegroundColor Cyan
if ($bm25) { docker compose exec app python -m korsik.cli seed-corpus /app/seed/corpus --bm25-only }
else { docker compose exec app python -m korsik.cli seed-corpus /app/seed/corpus }

Write-Host "5/5 Проверка MAX-токена..." -ForegroundColor Cyan
docker compose exec app python -m korsik.cli max-check

Write-Host "`nГотово. Кабинет: http://127.0.0.1:8000  (логин hr)" -ForegroundColor Green
Write-Host "Туннель и вебхук: scripts\tunnel.ps1 (нужна публикация домена в Cloudflare, см. README)." -ForegroundColor Green
