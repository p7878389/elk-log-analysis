# Windows 安装入口：找到 Python 3.8+ 后运行 install.py，参数原样透传
#   powershell -ExecutionPolicy Bypass -File install.ps1            交互安装
#   powershell -ExecutionPolicy Bypass -File install.ps1 --help     查看选项
$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$script = Join-Path $PSScriptRoot "install.py"
# py 启动器优先；Microsoft Store 的 python.exe 占位程序无法运行代码，会在版本检查中被排除
foreach ($cand in @(@("py", "-3"), @("python"), @("python3"))) {
  $exe = $cand[0]
  $pre = @($cand | Select-Object -Skip 1)
  if (-not (Get-Command $exe -CommandType Application -ErrorAction SilentlyContinue)) { continue }
  & $exe @pre -c "import sys; sys.exit(sys.version_info < (3, 8))" 2>$null
  if ($LASTEXITCODE -eq 0) {
    & $exe @pre $script @args
    exit $LASTEXITCODE
  }
}
Write-Host "未找到 Python 3.8+。请从 https://www.python.org/downloads/ 安装（勾选 Add python.exe to PATH），或 winget install Python.Python.3.12" -ForegroundColor Red
exit 1
