# Docker 一键部署（Windows，Docker Desktop）。容器 restart: unless-stopped，Docker Desktop 启动后服务随之常驻。
#   powershell -ExecutionPolicy Bypass -File deploy\docker-up.ps1 [up] [-Target base|code] [-Tls] [-Publish 8765]
#       首次运行：从模板创建 .env、config\envs.json 后退出，填好再运行
#       之后：构建镜像 → tokens.json 不存在时生成管理员 token → 后台启动 → 等待 /healthz
#       -Target/-Tls/-Publish 写入 .env，之后直接 docker compose up -d 也沿用
#   powershell -ExecutionPolicy Bypass -File deploy\docker-up.ps1 status | logs | down
# 常驻前提：Docker Desktop 设置中勾选 Start Docker Desktop when you sign in；Docker Desktop 随用户登录启动，
#           服务器无人登录时需要开机自动登录，或改用 deploy\service\install-windows.ps1 原生部署
param(
  [Parameter(Position = 0)][ValidateSet("up", "status", "logs", "down")][string]$Action = "up",
  [ValidateSet("base", "code")][string]$Target,
  [switch]$Tls,
  [string]$Publish
)
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
Set-Location $PSScriptRoot
# .env 与 tokens.json 都写成无 BOM 的 UTF-8：compose 解析 .env、Python 读 tokens.json 都不认 BOM
$Utf8 = New-Object System.Text.UTF8Encoding $false

function Fail($msg) { Write-Host $msg -ForegroundColor Red; exit 1 }

function Invoke-Docker {
  & docker @args
  if ($LASTEXITCODE -ne 0) { Fail "命令失败：docker $($args -join ' ')" }
}

# 只看退出码、不显示输出；Windows PowerShell 5.1 下重定向原生命令的 stderr 会被 Stop 当成异常
function Test-Docker {
  $ErrorActionPreference = "Continue"
  & docker @args *> $null
  return $LASTEXITCODE -eq 0
}

# .env 中设置 KEY=VALUE：已有（含注释掉的）则替换该行，否则追加
function Set-EnvValue($Key, $Value) {
  $path = Join-Path $PSScriptRoot ".env"
  $text = [IO.File]::ReadAllText($path)
  $nl = if ($text -match "`r`n") { "`r`n" } else { "`n" }
  $done = $false
  $lines = foreach ($l in [regex]::Split($text.TrimEnd("`r", "`n"), "\r?\n")) {
    if (-not $done -and $l -match ("^(# *)?" + [regex]::Escape($Key) + "=")) { $done = $true; "$Key=$Value" } else { $l }
  }
  if (-not $done) { $lines = @($lines) + "$Key=$Value" }
  [IO.File]::WriteAllText($path, (($lines -join $nl) + $nl), $Utf8)
}

# 只授权当前用户和 SYSTEM，去掉继承的 ACL（与 install.py 一致）
function Protect-File($path) {
  $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  & icacls $path /inheritance:r /grant:r "*${sid}:F" /grant:r "*S-1-5-18:F" | Out-Null
  if ($LASTEXITCODE -ne 0) { Write-Host "设置 $path 权限失败，请手动限制为仅本人可读" -ForegroundColor Yellow }
}

function Assert-Docker {
  if (-not (Get-Command docker -CommandType Application -ErrorAction SilentlyContinue)) {
    Fail "未找到 docker，请先安装 Docker Desktop：winget install Docker.DockerDesktop"
  }
  if (-not (Test-Docker compose version)) { Fail "需要 Docker Compose v2（docker compose 子命令），请升级 Docker Desktop" }
  if (-not (Test-Docker info)) { Fail "Docker 未运行：请先启动 Docker Desktop，等待左下角显示 Engine running" }
}

# 首次运行只创建配置文件，不启动：模板里没有真实的 ELK 地址和密码
function Initialize-Files {
  $created = @()
  if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Protect-File (Join-Path $PSScriptRoot ".env")
    $created += ".env"
  }
  if (-not (Test-Path config\envs.json)) {
    Copy-Item config\envs.example.json config\envs.json
    $created += "config\envs.json"
  }
  if ($created.Count -eq 0) { return }
  Write-Host "已从模板创建：$($created -join ', ')"
  Write-Host "请填写后重新运行本脚本：config\envs.json 写 ELK 地址与索引（密码写 env:变量名），.env 写对应密码"
  exit 0
}

# 在镜像内运行 gen-token：宿主机不需要 Python；token 只在终端显示这一次
function New-AdminToken {
  $out = & docker compose run --rm --no-deps -T elk-mcp python /app/scripts/mcp_http.py gen-token admin
  if ($LASTEXITCODE -ne 0) { Fail "生成 token 失败" }
  $token = $null; $line = $null
  foreach ($l in $out) {
    if ($l -match "^  (elk_[A-Za-z0-9_-]+)$") { $token = $Matches[1] }
    elseif ($l -match "^  (\{.*\})$") { $line = $Matches[1] }
  }
  if (-not $token -or -not $line) { Fail "无法解析 gen-token 输出：$($out -join "`n")" }
  $line = $line.Replace('"prod": false, "admin": false', '"prod": true, "admin": true')
  $json = "{`n  `"tokens`": [`n    $line`n  ]`n}`n"
  [IO.File]::WriteAllText((Join-Path $PSScriptRoot "config\tokens.json"), $json, $Utf8)
  return $token
}

function Wait-Healthy {
  $probe = "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ['ELK_HTTP_PORT'],timeout=3)"
  for ($i = 0; $i -lt 30; $i++) {
    if (Test-Docker compose exec -T elk-mcp python -c $probe) { return }
    Start-Sleep -Seconds 2
  }
  & docker compose logs --tail 30 elk-mcp
  Fail "服务 60 秒内未就绪，以上是最近日志"
}

function Invoke-Up {
  Initialize-Files
  if ($Target) { Set-EnvValue "ELK_IMAGE_TARGET" $Target }
  if ($Tls) { Set-EnvValue "COMPOSE_PROFILES" "tls" }
  if ($Publish) { Set-EnvValue "ELK_HTTP_PUBLISH" $Publish }
  Invoke-Docker compose build
  $token = $null
  if (-not (Test-Path config\tokens.json)) { $token = New-AdminToken }
  Invoke-Docker compose up -d
  Wait-Healthy
  $addr = (& docker compose port elk-mcp 8765 | Select-Object -First 1)
  Write-Host ""
  Write-Host "elk MCP 已在后台运行：http://$addr/mcp（健康检查 /healthz）" -ForegroundColor Green
  if ($token) {
    Write-Host "已生成管理员 token（prod、admin 权限，只显示这一次，请妥善保存）："
    Write-Host ""
    Write-Host "  $token"
    Write-Host ""
    Write-Host "添加成员：docker compose exec elk-mcp python /app/scripts/mcp_http.py gen-token <成员名>，把输出的 JSON 加到 config\tokens.json"
  }
  Write-Host "提示：在 Docker Desktop 设置中勾选 Start Docker Desktop when you sign in，登录后服务自动恢复"
}

Assert-Docker
switch ($Action) {
  "up" { Invoke-Up }
  "status" { Invoke-Docker compose ps }
  "logs" { Invoke-Docker compose logs -f --tail 200 }
  "down" { Invoke-Docker compose down }
}
