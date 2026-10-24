param(
    [string]$PublicUrl = '',
    [ValidateSet('auto','cloudflare','localhost-run')][string]$Provider = 'auto'
)
$ErrorActionPreference = 'Stop'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
$ExplicitPublicUrl = -not [string]::IsNullOrWhiteSpace($PublicUrl)
Set-Location -LiteralPath $PSScriptRoot
$PythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$LogDirectory = Join-Path $PSScriptRoot 'data\logs'
New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health/ready' -TimeoutSec 5 | Out-Null

function Read-SharedLog([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    $Stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    $Reader = [IO.StreamReader]::new($Stream)
    try { return $Reader.ReadToEnd() } finally { $Reader.Dispose() }
}

if (-not $PublicUrl -and $Provider -eq 'auto') {
    # Reuse a live fallback tunnel. Its key is stored only in this project's known-hosts file.
    $SshPidFile = Join-Path $PSScriptRoot 'data\ssh-tunnel.pid'
    if (Test-Path -LiteralPath $SshPidFile) {
        $ExistingSsh = Get-Process -Id ([int]([IO.File]::ReadAllText($SshPidFile).Trim())) -ErrorAction SilentlyContinue
        if ($ExistingSsh -and $ExistingSsh.ProcessName -eq 'ssh') { $Provider = 'localhost-run' }
    }
}

if (-not $PublicUrl -and $Provider -ne 'localhost-run') {
    $ToolDirectory = Join-Path $PSScriptRoot 'data\tools'
    New-Item -ItemType Directory -Path $ToolDirectory -Force | Out-Null
    $TunnelExe = Join-Path $ToolDirectory 'cloudflared.exe'
    if (-not (Test-Path -LiteralPath $TunnelExe)) {
        Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile $TunnelExe
    }
    $TunnelLog = Join-Path $LogDirectory 'tunnel.log'
    $TunnelProcess = $null
    $PidFile = Join-Path $PSScriptRoot 'data\tunnel.pid'
    if (Test-Path -LiteralPath $PidFile) {
        $PreviousId = [int]([IO.File]::ReadAllText($PidFile).Trim())
        $Candidate = Get-Process -Id $PreviousId -ErrorAction SilentlyContinue
        if ($Candidate -and $Candidate.Path -eq $TunnelExe) { $TunnelProcess = $Candidate }
    }
    if (-not $TunnelProcess) {
        $TunnelProcess = Start-Process -FilePath $TunnelExe -ArgumentList @('tunnel','--no-autoupdate','--protocol','http2','--url','http://127.0.0.1:8000') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDirectory 'tunnel.stdout.log') -RedirectStandardError $TunnelLog
    }
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot 'data\tunnel.pid'), [string]$TunnelProcess.Id)
    $Deadline = (Get-Date).AddSeconds(50)
    while ((Get-Date) -lt $Deadline) {
        if ($TunnelProcess.HasExited) {
            if ($Provider -eq 'cloudflare') { throw 'Tunnel exited. See data\logs\tunnel.log' }
            break
        }
        if (Test-Path -LiteralPath $TunnelLog) {
            $LogText = Read-SharedLog $TunnelLog
            $Address = [regex]::Match($LogText, 'https://[a-z0-9-]+\.trycloudflare\.com')
            if ($Address.Success) { $PublicUrl = $Address.Value; break }
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $PublicUrl -and $Provider -eq 'cloudflare') { throw 'No public tunnel URL received. See data\logs\tunnel.log' }
    if ($PublicUrl -and $Provider -eq 'auto') {
        try {
            $Probe = Invoke-RestMethod -Uri ($PublicUrl + '/health/ready') -TimeoutSec 5
            if ($Probe.status -ne 'ready') { $PublicUrl = '' }
        } catch { $PublicUrl = '' }
    }
    if (-not $PublicUrl -and $Provider -eq 'auto') {
        if (-not $TunnelProcess.HasExited -and $TunnelProcess.Path -eq $TunnelExe) { Stop-Process -Id $TunnelProcess.Id }
        $Provider = 'localhost-run'
    }
}

if (-not $PublicUrl -and $Provider -eq 'localhost-run') {
    $SshExe = (Get-Command ssh.exe -ErrorAction Stop).Source
    $SshLog = Join-Path $LogDirectory 'ssh-tunnel.stdout.log'
    $SshPidFile = Join-Path $PSScriptRoot 'data\ssh-tunnel.pid'
    $SshProcess = $null
    if (Test-Path -LiteralPath $SshPidFile) {
        $Candidate = Get-Process -Id ([int]([IO.File]::ReadAllText($SshPidFile).Trim())) -ErrorAction SilentlyContinue
        if ($Candidate -and $Candidate.Path -eq $SshExe) { $SshProcess = $Candidate }
    }
    if (-not $SshProcess) {
        $SshArguments = @('-T','-F','NUL','-o','BatchMode=yes','-o','StrictHostKeyChecking=accept-new','-o','UserKnownHostsFile=data/localhost-run-known-hosts','-o','ServerAliveInterval=30','-o','IdentitiesOnly=yes','-o','IdentityFile=none','-o','ExitOnForwardFailure=yes','-R','80:127.0.0.1:8000','nokey@localhost.run')
        $SshProcess = Start-Process -FilePath $SshExe -ArgumentList $SshArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $SshLog -RedirectStandardError (Join-Path $LogDirectory 'ssh-tunnel.stderr.log')
        [IO.File]::WriteAllText($SshPidFile, [string]$SshProcess.Id)
    }
    $Deadline = (Get-Date).AddSeconds(40)
    while ((Get-Date) -lt $Deadline) {
        if ($SshProcess.HasExited) { throw 'SSH tunnel exited. See data\logs\ssh-tunnel.stderr.log' }
        $Addresses = [regex]::Matches((Read-SharedLog $SshLog), 'https://[a-z0-9-]+\.(?:lhr\.life|localhost\.run)')
        if ($Addresses.Count) { $PublicUrl = $Addresses[$Addresses.Count - 1].Value; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $PublicUrl) { throw 'No public SSH tunnel URL received' }
}

$ParsedUrl = [Uri]$PublicUrl
if ($ParsedUrl.Scheme -ne 'https' -or -not $ParsedUrl.IsDefaultPort -or $ParsedUrl.Query -or $ParsedUrl.Fragment -or $ParsedUrl.UserInfo) {
    throw 'PublicUrl must be an HTTPS origin on port 443'
}
$PublicUrl = $PublicUrl.TrimEnd('/')
$Ready = $false
for ($Attempt = 0; $Attempt -lt 10; $Attempt++) {
    try {
        $Health = Invoke-RestMethod -Uri ($PublicUrl + '/health/ready') -TimeoutSec 4
        if ($Health.status -eq 'ready') { $Ready = $true; break }
    } catch { Start-Sleep -Milliseconds 500 }
}
if (-not $Ready) { throw 'The public address cannot reach this bot. Subscription was not changed.' }

# A permanent URL takes precedence over the rotating development tunnel.
if ($ExplicitPublicUrl) {
    $PreviousWatchPid = Join-Path $PSScriptRoot 'data\tunnel-watch.pid'
    if (Test-Path -LiteralPath $PreviousWatchPid) {
        $PreviousWatchId = [int]([IO.File]::ReadAllText($PreviousWatchPid).Trim())
        $PreviousWatch = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $PreviousWatchId) -ErrorAction SilentlyContinue
        if ($PreviousWatch -and $PreviousWatch.ExecutablePath -eq $PythonExe -and $PreviousWatch.CommandLine -match '\-m\s+app\.tunnel_watch') {
            Get-CimInstance Win32_Process -Filter ('ParentProcessId=' + $PreviousWatchId) | ForEach-Object {
                if ($_.Name -eq 'python.exe' -and $_.CommandLine -match '\-m\s+app\.tunnel_watch') { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
            }
            Stop-Process -Id $PreviousWatchId -ErrorAction SilentlyContinue
        }
    }
}

$EnvPath = Join-Path $PSScriptRoot '.env'
$EnvLines = [IO.File]::ReadAllLines($EnvPath)
$Found = $false
for ($Index = 0; $Index -lt $EnvLines.Length; $Index++) {
    if ($EnvLines[$Index] -match '^PUBLIC_BASE_URL=') {
        $EnvLines[$Index] = 'PUBLIC_BASE_URL=' + $PublicUrl
        $Found = $true
    }
}
if (-not $Found) { $EnvLines += 'PUBLIC_BASE_URL=' + $PublicUrl }
[IO.File]::WriteAllLines($EnvPath, $EnvLines, [Text.UTF8Encoding]::new($false))
$env:PUBLIC_BASE_URL = $PublicUrl
& $PythonExe -m app.cli subscribe
if ($LASTEXITCODE -ne 0) { throw 'MAX rejected the webhook subscription' }
if ($Provider -eq 'localhost-run' -and -not $ExplicitPublicUrl) {
    $WatchPidFile = Join-Path $PSScriptRoot 'data\tunnel-watch.pid'
    $Watcher = $null
    if (Test-Path -LiteralPath $WatchPidFile) {
        $WatchId = [int]([IO.File]::ReadAllText($WatchPidFile).Trim())
        $Watcher = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $WatchId) -ErrorAction SilentlyContinue
        if ($Watcher -and $Watcher.CommandLine -notmatch '\-m\s+app\.tunnel_watch') { $Watcher = $null }
    }
    if (-not $Watcher) {
        $Watcher = Start-Process -FilePath $PythonExe -ArgumentList @('-m','app.tunnel_watch') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDirectory 'tunnel-watch.stdout.log') -RedirectStandardError (Join-Path $LogDirectory 'tunnel-watch.stderr.log')
        [IO.File]::WriteAllText($WatchPidFile, [string]$Watcher.Id)
    }
}
Write-Output ('Webhook: ' + $PublicUrl + '/webhook/max')
