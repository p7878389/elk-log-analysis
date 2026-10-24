#!/bin/sh
# Docker 一键部署（macOS / Linux）。容器 restart: unless-stopped，Docker 开机自启后服务随之常驻。
#   deploy/docker-up.sh [up] [--target base|code] [--tls] [--publish 8765]
#       首次运行：从模板创建 .env、config/envs.json 后退出，填好再运行
#       之后：构建镜像 → tokens.json 不存在时生成管理员 token → 后台启动 → 等待 /healthz
#       --target/--tls/--publish 写入 .env，之后直接 docker compose up -d 也沿用
#   deploy/docker-up.sh status | logs | down        查看状态 / 跟踪日志 / 停止（保留数据卷）
set -eu
cd "$(dirname "$0")"

die() { echo "$*" >&2; exit 1; }

usage() { sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# .env 中设置 KEY=VALUE：已有（含注释掉的）则替换该行，否则追加
set_env() {
  awk -v k="$1" -v v="$2" '
    !done && ($0 ~ "^" k "=" || $0 ~ "^# *" k "=") { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }' .env > .env.tmp
  cat .env.tmp > .env && rm -f .env.tmp   # 覆盖内容而非替换文件，保留 .env 的 600 权限
}

check_docker() {
  command -v docker >/dev/null 2>&1 || die "未找到 docker，请先安装 Docker Engine 或 Docker Desktop"
  docker compose version >/dev/null 2>&1 || die "需要 Docker Compose v2（docker compose 子命令）"
  docker info >/dev/null 2>&1 || die "Docker 未运行：启动 Docker Desktop，或 sudo systemctl start docker"
}

# 首次运行只创建配置文件，不启动：模板里没有真实的 ELK 地址和密码
init_files() {
  created=""
  if [ ! -f .env ]; then
    (umask 077 && cp .env.example .env)
    created="$created .env"
  fi
  if [ ! -f config/envs.json ]; then
    cp config/envs.example.json config/envs.json
    created="$created config/envs.json"
  fi
  [ -z "$created" ] && return 0
  echo "已从模板创建：${created# }"
  echo "请填写后重新运行本脚本：config/envs.json 写 ELK 地址与索引（密码写 env:变量名），.env 写对应密码"
  exit 0
}

# 在镜像内运行 gen-token：宿主机不需要 Python；token 只在终端显示这一次
gen_admin_token() {
  out=$(docker compose run --rm --no-deps -T elk-mcp python /app/scripts/mcp_http.py gen-token admin) \
    || die "生成 token 失败"
  token=$(printf '%s\n' "$out" | sed -n 's/^  \(elk_[A-Za-z0-9_-]*\)$/\1/p')
  line=$(printf '%s\n' "$out" | sed -n 's/^  \({.*}\)$/\1/p' \
    | sed 's/"prod": false, "admin": false/"prod": true, "admin": true/')
  [ -n "$token" ] && [ -n "$line" ] || die "无法解析 gen-token 输出：$out"
  printf '{\n  "tokens": [\n    %s\n  ]\n}\n' "$line" > config/tokens.json
  ADMIN_TOKEN=$token
}

# 容器以 uid 10001 运行，Linux 上 bind mount 按宿主机权限判断；Docker Desktop 不受影响
config_perm_hint() {
  [ "$(uname -s)" = Linux ] || return 0
  if [ "$(id -u)" = 0 ]; then
    chown 10001:10001 config/*.json && chmod 600 config/*.json
  else
    echo "提示：建议 sudo chown 10001:10001 config/*.json && sudo chmod 600 config/*.json，只让容器用户可读"
  fi
}

wait_healthy() {
  i=0
  while [ $i -lt 30 ]; do
    if docker compose exec -T elk-mcp python -c \
      "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ['ELK_HTTP_PORT'],timeout=3)" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
    i=$((i + 1))
  done
  docker compose logs --tail 30 elk-mcp >&2
  die "服务 60 秒内未就绪，以上是最近日志"
}

boot_hint() {
  if command -v systemctl >/dev/null 2>&1 && [ "$(systemctl is-enabled docker 2>/dev/null)" != enabled ]; then
    echo "提示：Docker 未设为开机自启，重启后服务不会恢复；执行 sudo systemctl enable docker"
  elif [ "$(uname -s)" = Darwin ]; then
    echo "提示：需在 Docker Desktop 设置中勾选 Start Docker Desktop when you sign in，登录后服务自动恢复"
  fi
}

cmd_up() {
  init_files
  [ -n "$TARGET" ] && set_env ELK_IMAGE_TARGET "$TARGET"
  [ -n "$TLS" ] && set_env COMPOSE_PROFILES tls
  [ -n "$PUBLISH" ] && set_env ELK_HTTP_PUBLISH "$PUBLISH"
  docker compose build
  ADMIN_TOKEN=""
  [ -f config/tokens.json ] || gen_admin_token
  config_perm_hint
  docker compose up -d
  wait_healthy
  addr=$(docker compose port elk-mcp 8765)
  echo
  echo "elk MCP 已在后台运行：http://$addr/mcp（健康检查 /healthz）"
  if [ -n "$ADMIN_TOKEN" ]; then
    echo "已生成管理员 token（prod、admin 权限，只显示这一次，请妥善保存）："
    echo
    echo "  $ADMIN_TOKEN"
    echo
    echo "添加成员：docker compose exec elk-mcp python /app/scripts/mcp_http.py gen-token <成员名>，把输出的 JSON 加到 config/tokens.json"
  fi
  boot_hint
}

ACTION=up TARGET="" TLS="" PUBLISH=""
while [ $# -gt 0 ]; do
  case "$1" in
    up|status|logs|down) ACTION=$1 ;;
    --target) [ $# -ge 2 ] || usage 1; TARGET=$2; shift ;;
    --tls) TLS=1 ;;
    --publish) [ $# -ge 2 ] || usage 1; PUBLISH=$2; shift ;;
    -h|--help) usage ;;
    *) echo "未知参数：$1" >&2; usage 1 ;;
  esac
  shift
done
case "$TARGET" in ""|base|code) ;; *) die "--target 只能是 base 或 code" ;; esac

check_docker
case "$ACTION" in
  up) cmd_up ;;
  status) docker compose ps ;;
  logs) docker compose logs -f --tail 200 ;;
  down) docker compose down ;;
esac
