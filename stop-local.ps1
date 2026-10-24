$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
foreach ($Name in @('tunnel-watch','api','tunnel','ssh-tunnel')) {
    $PidFile = Join-Path $PSScriptRoot ('data\' + $Name + '.pid')
    if (Test-Path -LiteralPath $PidFile) {
        $ProcessId = [int]([IO.File]::ReadAllText($PidFile).Trim())
        $ProcessInfo = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $ProcessId) -ErrorAction SilentlyContinue
        if ($ProcessInfo) {
            $IsPython = $Name -in @('api','tunnel-watch')
            $ModulePattern = if ($Name -eq 'api') { '\-m\s+app\.local' } else { '\-m\s+app\.tunnel_watch' }
            $ExpectedExe = if ($IsPython) { Join-Path $PSScriptRoot '.venv\Scripts\python.exe' } elseif ($Name -eq 'ssh-tunnel') { (Get-Command ssh.exe).Source } else { Join-Path $PSScriptRoot 'data\tools\cloudflared.exe' }
            $ExpectedModule = -not $IsPython -or $ProcessInfo.CommandLine -match $ModulePattern
            if ($Name -eq 'ssh-tunnel') { $ExpectedModule = $ProcessInfo.CommandLine -match 'nokey@localhost\.run' -and $ProcessInfo.CommandLine -match '80:127\.0\.0\.1:8000' }
            if ($ProcessInfo.ExecutablePath -eq $ExpectedExe -and $ExpectedModule) {
                if ($IsPython) {
                    $Children = Get-CimInstance Win32_Process -Filter ('ParentProcessId=' + $ProcessId)
                    foreach ($Child in $Children) {
                        if ($Child.Name -eq 'python.exe' -and $Child.CommandLine -match $ModulePattern) { Stop-Process -Id $Child.ProcessId -ErrorAction SilentlyContinue }
                    }
                }
                Stop-Process -Id $ProcessId -ErrorAction SilentlyContinue
            }
        }
    }
}
Write-Output 'API and tunnel stopped. PostgreSQL data is preserved.'
