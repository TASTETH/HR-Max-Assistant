$ErrorActionPreference = 'Stop'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
Set-Location -LiteralPath $PSScriptRoot
$PythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create Python virtual environment' }
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.env'))) {
    & $PythonExe -m app.configure
    throw 'Local .env created. Set MAX_BOT_TOKEN, then run this script again.'
}
& $PythonExe -m pip install -r requirements.txt --disable-pip-version-check --quiet
if ($LASTEXITCODE -ne 0) { throw 'Cannot install dependencies' }
& $PythonExe -m app.local
exit $LASTEXITCODE
