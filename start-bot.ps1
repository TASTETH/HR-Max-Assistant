param([string]$PublicUrl = '', [ValidateSet('auto','cloudflare','localhost-run')][string]$Provider = 'auto')
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
$LogDirectory = Join-Path $PSScriptRoot 'data\logs'
New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
$Ready = $false
try {
    $Health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health/ready' -TimeoutSec 2
    $Ready = $Health.status -eq 'ready'
} catch { }
if (-not $Ready) {
    $ServerProcess = Start-Process -FilePath $PythonExe -ArgumentList @('-m','app.local') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDirectory 'api.stdout.log') -RedirectStandardError (Join-Path $LogDirectory 'api.stderr.log')
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot 'data\api.pid'), [string]$ServerProcess.Id)
    $Deadline = (Get-Date).AddSeconds(55)
    while ((Get-Date) -lt $Deadline) {
        if ($ServerProcess.HasExited) { throw 'Server exited. See data\logs\api.stderr.log' }
        try {
            $Health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health/ready' -TimeoutSec 2
            if ($Health.status -eq 'ready') { $Ready = $true; break }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $Ready) { throw 'Server did not become ready. See data\logs\api.stderr.log' }
}
& (Join-Path $PSScriptRoot 'start-tunnel.ps1') -PublicUrl $PublicUrl -Provider $Provider
Write-Output 'Bot: https://max.ru/id5021013384_2_bot'
Write-Output 'Local health: http://127.0.0.1:8000/health/ready'
