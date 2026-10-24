$ErrorActionPreference = 'Stop'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
Set-Location -LiteralPath $PSScriptRoot
python -m app.configure
if ($LASTEXITCODE -ne 0) { throw 'Cannot create local configuration' }
