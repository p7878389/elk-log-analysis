# Native Windows deployment; no administrator or Docker required.
# Install login startup: .\deploy\native-up.ps1 -InstallStartup
param(
    [ValidateSet('start', 'status', 'stop')][string]$Action = 'start',
    [string]$Python = '',
    [switch]$InstallStartup
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$EnvFile = Join-Path $PSScriptRoot '.env'
$Data = Join-Path $PSScriptRoot 'data'
$PidFile = Join-Path $Data 'http.pid'
$Startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'elk-mcp.lnk'
if (-not (Test-Path -LiteralPath $EnvFile)) { throw "Missing $EnvFile; configure it before starting." }
New-Item -ItemType Directory -Force -Path $Data | Out-Null

function Get-ServiceProcess {
    if (Test-Path -LiteralPath $PidFile) {
        $ServicePid = [int](Get-Content -LiteralPath $PidFile -Raw).Trim()
        $Candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$ServicePid" -ErrorAction SilentlyContinue
        if ($Candidate -and $Candidate.CommandLine -like "*$Root\scripts\mcp_http.py*") { return $Candidate }
    }
}

$Running = Get-ServiceProcess
if ($Action -eq 'stop') {
    if ($Running) {
        # Stop the hosted GitNexus child too; its bearer token changes on restart.
        & taskkill.exe /PID $Running.ProcessId /T /F | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Failed to stop the MCP process tree.' }
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $Startup -Force -ErrorAction SilentlyContinue
    Write-Host 'Stopped; login startup removed.'
    exit
}
if ($Action -eq 'status') {
    if ($Running) { Write-Host "Running PID=$($Running.ProcessId)" }
    else { Write-Host 'Not running'; exit 1 }
    exit
}
if (-not $Running) {
    if (-not $Python) {
        $Python = (& py -3 -c 'import sys; print(sys.executable)').Trim()
        if ($LASTEXITCODE -ne 0) { throw 'Python 3 is required. Supply -Python with its full path.' }
    }
    $ProcessArgs = '"{0}" --env-file "{1}"' -f (Join-Path $Root 'scripts\mcp_http.py'), $EnvFile
    $Started = Start-Process -FilePath $Python -ArgumentList $ProcessArgs -WorkingDirectory $Root `
        -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Data 'http.stdout.log') `
        -RedirectStandardError (Join-Path $Data 'http.stderr.log') -PassThru
    Set-Content -LiteralPath $PidFile -Value $Started.Id -Encoding ascii
    $Ready = $false
    $BindPort = 8765
    foreach ($Line in Get-Content -LiteralPath $EnvFile) {
        if ($Line -match '^ELK_HTTP_PORT=(\d+)\s*$') { $BindPort = [int]$Matches[1] }
    }
    for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
        Start-Sleep -Milliseconds 500
        $Started.Refresh()
        if ($Started.HasExited) { throw "Server exited; see $Data\http.stderr.log" }
        try {
            $Health = Invoke-WebRequest "http://127.0.0.1:$BindPort/healthz" -UseBasicParsing -TimeoutSec 2
            if ($Health.StatusCode -eq 200) { $Ready = $true; break }
        } catch { }
    }
    if (-not $Ready) { throw "Health check failed; see $Data\http.stderr.log" }
    Write-Host "Started PID=$($Started.Id); health check passed on port $BindPort."
} else { Write-Host "Already running PID=$($Running.ProcessId)" }
if ($InstallStartup) {
    $Shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($Startup)
    $Shortcut.TargetPath = Join-Path $PSHOME 'powershell.exe'
    $Shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $PSCommandPath
    if ($Python) { $Shortcut.Arguments += ' -Python "{0}"' -f $Python }
    $Shortcut.WorkingDirectory = $Root
    $Shortcut.WindowStyle = 7
    $Shortcut.Save()
    Write-Host 'Current-user login startup installed (requires Windows login).'
}
Write-Host "Logs: $Data\http.stderr.log"
