# Docker 一键部署（Windows，Docker Desktop）。容器 restart: unless-stopped，Docker Desktop 启动后服务随之常驻。
# 在部署目录（build-image.sh 生成的部署包解压处）运行，不在源码仓库中运行；本脚本不构建镜像。
#   powershell -ExecutionPolicy Bypass -File docker-up.ps1 [up] [-Image 镜像] [-Load 镜像包.tar.gz] [-Tls] [-Publish 8765]
#       首次运行：从模板创建 .env 后退出；在 .env 中填好 ELK 环境、密码等（全部用环境变量配置）再运行
#       之后：载入或拉取镜像 → 用 node 镜像安装 gitnexus（版本没变时跳过）→ 未配置成员 token 时生成管理员 token
#             写入 .env → 后台启动 → 等待 /healthz。不需要预装 node / gitnexus
#       -Load 载入 build-image.sh --save 导出的镜像包，并把 ELK_IMAGE 设为其中的镜像
#       -Image/-Tls/-Publish 写入 .env，之后直接 docker compose up -d 也沿用
#   powershell -ExecutionPolicy Bypass -File docker-up.ps1 status | logs | down
# 常驻前提：Docker Desktop 设置中勾选 Start Docker Desktop when you sign in；Docker Desktop 随用户登录启动，
#           服务器无人登录时需要开机自动登录，或改用 deploy\service\install-windows.ps1 原生部署
param(
  [Parameter(Position = 0)][ValidateSet("up", "status", "logs", "down")][string]$Action = "up",
  [string]$Image,
  [string]$Load,
  [switch]$Tls,
  [string]$Publish
)
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
# -Load 的相对路径按调用时的目录解析，之后才切换到部署目录
if ($Load) { $Load = [IO.Path]::GetFullPath((Join-Path (Get-Location).Path $Load)) }
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

# 部署目录与源码分离：源码仓库只负责构建镜像，配置、token 与数据都放在部署目录
function Assert-NotSourceRepo {
  if ((Test-Path ..\scripts\mcp_http.py) -and (Test-Path ..\Dockerfile)) {
    Fail "当前在源码仓库中。请在源码仓库执行 deploy/build-image.sh 构建镜像并生成部署包，再到部署目录解压后运行 docker-up.ps1"
  }
}

# .env 中 KEY 的值（取最后一个未注释的），没有返回 $null
function Get-EnvValue($Key) {
  if (-not (Test-Path .env)) { return $null }
  $m = Select-String -Path .env -Pattern ("^" + [regex]::Escape($Key) + "=(.+)$") | Select-Object -Last 1
  if ($m) { return $m.Matches[0].Groups[1].Value.Trim() }
  return $null
}

# compose 实际使用的镜像：环境变量优先于 .env，与 compose 的变量替换规则一致
function Get-ImageRef {
  if ($env:ELK_IMAGE) { return $env:ELK_IMAGE }
  $v = Get-EnvValue "ELK_IMAGE"
  if ($v) { return $v }
  return "elk-mcp:latest"
}

# 首次运行只创建 .env，不启动：模板里没有真实的 ELK 地址和密码
function Initialize-Files {
  if (Test-Path .env) { return $null }
  Copy-Item .env.example .env
  Protect-File (Join-Path $PSScriptRoot ".env")
  return ".env"
}

# ELK 环境：.env 的 ELK_ENVIRONMENTS / ELK_ENVS，或改用文件 config\envs.json
function Assert-ElkConfig {
  if (-not ((Get-EnvValue "ELK_ENVIRONMENTS") -or (Get-EnvValue "ELK_ENVS") -or (Test-Path config\envs.json))) {
    Fail "未配置 ELK 环境：在 .env 中填写 ELK_ENVS 与 ELK_<环境>_URL/INDEX/USERNAME/PASSWORD（见 .env.example），或提供 config\envs.json"
  }
}

function Import-ImageFile($path) {
  if (-not (Test-Path $path)) { Fail "镜像包不存在：$path" }
  $out = & docker load -i $path
  if ($LASTEXITCODE -ne 0) { Fail "载入镜像失败：$path" }
  $out | Write-Host
  $loaded = $null
  foreach ($l in $out) { if (-not $loaded -and $l -match "^Loaded image: (.+)$") { $loaded = $Matches[1].Trim() } }
  if (-not $loaded) { Fail "镜像包中没有带标签的镜像，请用 -Image 指定" }
  return $loaded
}

# 镜像不在本机时从镜像仓库拉取；本地构建、未推送的镜像需要先 -Load
function Confirm-Image {
  $ref = Get-ImageRef
  if (Test-Docker image inspect $ref) { return }
  Write-Host "本机没有镜像 $ref，尝试从镜像仓库拉取"
  & docker compose pull elk-mcp
  if ($LASTEXITCODE -ne 0) { Fail "无法获取镜像 ${ref}：用 -Load 载入 build-image.sh --save 导出的镜像包，或先 docker login 镜像仓库" }
}

# docker-compose.yml 的 gitnexus-install：先单独跑一次，npm 输出实时可见（首次需几分钟）；返回是否换了版本
function Install-Gitnexus {
  Write-Host "准备 gitnexus（docker-compose.yml 的 gitnexus-install）"
  $ErrorActionPreference = "Continue"
  $changed = $false
  & docker compose run --rm -T gitnexus-install 2>&1 | ForEach-Object {
    $line = "$_"
    Write-Host $line
    if ($line -match "^GITNEXUS_INSTALLED") { $changed = $true }
  }
  if ($LASTEXITCODE -ne 0) {
    Fail "安装 gitnexus 失败（见上方输出）：确认能访问 Docker Hub 与 npm 源，可在 .env 设置 ELK_NODE_IMAGE / ELK_NPM_REGISTRY 换成内部镜像源"
  }
  return $changed
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
  Set-EnvValue "ELK_HTTP_TOKENS" "[$line]"
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
  $img = $Image
  if ($Load) { $loaded = Import-ImageFile $Load; if (-not $img) { $img = $loaded } }
  $created = Initialize-Files
  if ($img) { Set-EnvValue "ELK_IMAGE" $img }
  if ($Tls) { Set-EnvValue "COMPOSE_PROFILES" "tls" }
  if ($Publish) { Set-EnvValue "ELK_HTTP_PUBLISH" $Publish }
  # 函数返回空数组时得到 $null，单个元素时得到字符串，两种情况都按真假判断
  if ($created) {
    Write-Host "已从模板创建：$($created -join ', ')"
    Write-Host "请在 .env 中填写 ELK 环境（ELK_ENVS、ELK_<环境>_URL/INDEX/USERNAME/PASSWORD）、GitLab 令牌等，再重新运行本脚本"
    exit 0
  }
  Assert-ElkConfig
  Confirm-Image
  $gnChanged = Install-Gitnexus
  $token = $null
  if (-not (Get-EnvValue "ELK_HTTP_TOKENS") -and -not (Test-Path config\tokens.json)) { $token = New-AdminToken }
  Invoke-Docker compose up -d
  # 已在运行的 elk-mcp 配置没变时 up 不会重建它：gitnexus 换了版本要重启才会加载新文件
  if ($gnChanged) { Invoke-Docker compose restart elk-mcp }
  Wait-Healthy
  $addr = (& docker compose port elk-mcp 8765 | Select-Object -First 1)
  Write-Host ""
  Write-Host "elk MCP 已在后台运行：http://$addr/mcp（健康检查 /healthz，镜像 $(Get-ImageRef)）" -ForegroundColor Green
  if ($token) {
    Write-Host "已生成管理员 token（prod、admin 权限，只显示这一次，请妥善保存）："
    Write-Host ""
    Write-Host "  $token"
    Write-Host ""
    Write-Host "已写入 .env 的 ELK_HTTP_TOKENS。添加成员：docker compose exec elk-mcp python /app/scripts/mcp_http.py gen-token <成员名>，"
    Write-Host "把输出的 JSON 加进 ELK_HTTP_TOKENS 数组后重新运行本脚本"
  }
  Write-Host "提示：在 Docker Desktop 设置中勾选 Start Docker Desktop when you sign in，登录后服务自动恢复"
}

Assert-NotSourceRepo
Assert-Docker
switch ($Action) {
  "up" { Invoke-Up }
  "status" { Invoke-Docker compose ps }
  "logs" { Invoke-Docker compose logs -f --tail 200 }
  "down" { Invoke-Docker compose down }
}
