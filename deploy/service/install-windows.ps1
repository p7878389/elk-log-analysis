# Windows：注册为开机自启的计划任务（以 SYSTEM 运行，无需登录）。在"管理员 PowerShell"中执行：
#   powershell -ExecutionPolicy Bypass -File deploy\service\install-windows.ps1
# 卸载：Unregister-ScheduledTask -TaskName elk-mcp -Confirm:$false
# 说明：原生 Windows 支持全部 elk_* 工具与 code_locate；code_prepare 的后台索引需要 Docker/WSL 部署。
#       .env 中请设置 ELK_CODE_CACHE（SYSTEM 账户的 home 在 C:\Windows\System32\config\systemprofile）。
param(
  [string]$Python = (Get-Command python -ErrorAction Stop).Source,
  [int]$Port = 8765
)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$EnvFile = Join-Path $Root "deploy\.env"
if (-not (Test-Path $EnvFile)) { throw "缺少 $EnvFile，请先按 deploy\.env.example 创建并打开「原生部署」段" }

$LogDir = Join-Path $Root "deploy\data"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "elk-mcp.log"

$Cmd = "`"$Python`" `"$Root\scripts\mcp_http.py`" --env-file `"$EnvFile`" >> `"$Log`" 2>&1"
$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $Cmd" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
Register-ScheduledTask -TaskName "elk-mcp" -Action $Action -Trigger $Trigger -Settings $Settings `
  -Principal $Principal -Force | Out-Null

New-NetFirewallRule -DisplayName "elk-mcp" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow `
  -ErrorAction SilentlyContinue | Out-Null
Start-ScheduledTask -TaskName "elk-mcp"
Write-Host "已启动，日志：$Log"
Write-Host "自检：Invoke-WebRequest http://127.0.0.1:$Port/healthz"
