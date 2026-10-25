# Сценарные тесты на отдельной БД korsik_test (рабочая БД не затрагивается)
Set-Location (Split-Path $PSScriptRoot -Parent)
docker compose exec db psql -U korsik -d korsik -c "CREATE DATABASE korsik_test"
$pw = (Select-String -Path .env -Pattern '^POSTGRES_PASSWORD=(.+)$').Matches[0].Groups[1].Value.Trim()
docker compose run --rm -e TEST_DATABASE_URL="postgresql://korsik:$pw@db:5432/korsik_test" app sh -c "pip install -q pytest==9.1.1 && python -m pytest -q tests"
