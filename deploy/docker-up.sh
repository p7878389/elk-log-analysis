#!/bin/sh
# Docker 一键部署（Linux / macOS）。容器 restart: unless-stopped，Docker 开机自启后服务随之常驻。
# 在部署目录（build-image.sh 生成的部署包解压处，如 /opt/elk-mcp）运行，不在源码仓库中运行；本脚本不构建镜像。
#   ./docker-up.sh [up] [--image 镜像] [--load 镜像包.tar.gz] [--tls] [--publish 8765]
#       首次运行：从模板创建 .env 后退出；在 .env 中填好 ELK 环境、密码等（全部用环境变量配置）再运行
#       之后：载入或拉取镜像 → 用 node 镜像安装 gitnexus（版本没变时跳过）→ 未配置成员 token 时生成管理员 token
#             写入 .env → 后台启动 → 等待 /healthz。服务器上不需要预装 node / gitnexus
#       --load 载入 build-image.sh --save 导出的镜像包，并把 ELK_IMAGE 设为其中的镜像
#       --image/--tls/--publish 写入 .env（ELK_IMAGE、COMPOSE_PROFILES、ELK_HTTP_PUBLISH），之后直接 docker compose up -d 也沿用
#   ./docker-up.sh status | logs | down        查看状态 / 跟踪日志 / 停止（保留数据卷）
set -eu
CALLER_DIR=$(pwd)
cd "$(dirname "$0")"

die() { echo "$*" >&2; exit 1; }

usage() { sed -n '2,10p' "$(basename "$0")" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# 部署目录与源码分离：源码仓库只负责构建镜像，配置、token 与数据都放在服务器的部署目录
in_source_repo() { [ -f ../scripts/mcp_http.py ] && [ -f ../Dockerfile ]; }

# .env 中设置 KEY=VALUE：已有（含注释掉的）则替换该行，否则追加
set_env() {
  awk -v k="$1" -v v="$2" '
    !done && ($0 ~ "^" k "=" || $0 ~ "^# *" k "=") { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }' .env > .env.tmp
  cat .env.tmp > .env && rm -f .env.tmp   # 覆盖内容而非替换文件，保留 .env 的 600 权限
}

# .env 中 KEY 的值（取最后一个未注释的）
env_get() { sed -n "s/^$1=//p" .env 2>/dev/null | tail -n 1; }

# compose 实际使用的镜像：环境变量优先于 .env，与 compose 的变量替换规则一致
image_ref() {
  ref=${ELK_IMAGE:-}
  [ -n "$ref" ] || ref=$(env_get ELK_IMAGE)
  echo "${ref:-elk-mcp:latest}"
}

check_docker() {
  command -v docker >/dev/null 2>&1 || die "未找到 docker，请先安装 Docker Engine 或 Docker Desktop"
  docker compose version >/dev/null 2>&1 || die "需要 Docker Compose v2（docker compose 子命令）"
  docker info >/dev/null 2>&1 || die "Docker 未运行：sudo systemctl start docker，或启动 Docker Desktop"
}

# 首次运行只创建 .env，不启动：模板里没有真实的 ELK 地址和密码
init_files() {
  CREATED=""
  if [ ! -f .env ]; then
    (umask 077 && cp .env.example .env)
    CREATED=".env"
  fi
}

# ELK 环境：.env 的 ELK_ENVIRONMENTS / ELK_ENVS，或改用文件 config/envs.json
check_elk_config() {
  [ -n "$(env_get ELK_ENVIRONMENTS)" ] || [ -n "$(env_get ELK_ENVS)" ] || [ -f config/envs.json ] \
    || die "未配置 ELK 环境：在 .env 中填写 ELK_ENVS 与 ELK_<环境>_URL/INDEX/USERNAME/PASSWORD（见 .env.example），或提供 config/envs.json"
  if grep -q '^ELK_[A-Z0-9_]*_URL=.*\.example\.com' .env; then
    echo "提示：.env 中仍有 example.com 示例地址，请改成实际的 Kibana / ES 地址"
  fi
}

load_image() {
  [ -f "$1" ] || die "镜像包不存在：$1"
  out=$(docker load -i "$1") || die "载入镜像失败：$1"
  printf '%s\n' "$out"
  loaded=$(printf '%s\n' "$out" | sed -n 's/^Loaded image: //p' | head -n 1)
  [ -n "$loaded" ] || die "镜像包中没有带标签的镜像，请用 --image 指定"
  [ -n "$IMAGE" ] || IMAGE=$loaded
}

# 镜像不在本机时从镜像仓库拉取；本地构建、未推送的镜像需要先 --load
ensure_image() {
  ref=$(image_ref)
  docker image inspect "$ref" >/dev/null 2>&1 && return 0
  echo "本机没有镜像 $ref，尝试从镜像仓库拉取"
  docker compose pull elk-mcp || die "无法获取镜像 $ref：用 --load 载入 build-image.sh --save 导出的镜像包，或先 docker login 镜像仓库"
}

# docker-compose.yml 的 gitnexus-install：用官方 node 镜像把 node + gitnexus 装进数据卷。
# 单独先跑一次，让 npm 输出实时可见（首次需几分钟），并知道是否换了版本；版本没变时秒退
install_gitnexus() {
  v=${ELK_GITNEXUS_VERSION:-$(env_get ELK_GITNEXUS_VERSION)}
  echo "准备 gitnexus ${v:-1.6.12}（docker-compose.yml 的 gitnexus-install）"
  log=$(mktemp)
  { docker compose run --rm -T gitnexus-install 2>&1; echo "__RC=$?"; } | tee "$log" | grep -v '^__RC='
  rc=$(sed -n 's/^__RC=//p' "$log")
  changed=$(grep -c '^GITNEXUS_INSTALLED' "$log" || true)
  rm -f "$log"
  [ "$rc" = 0 ] || die "安装 gitnexus 失败（见上方输出）：确认服务器能访问 Docker Hub 与 npm 源，可在 .env 设置 ELK_NODE_IMAGE / ELK_NPM_REGISTRY 换成内部镜像源"
  [ "$changed" = 0 ] || GN_CHANGED=1
}

# 在 elk-mcp 容器里实际跑一次 gitnexus：安装不完整、原生模块不兼容、架构不一致时在这里暴露
verify_gitnexus() {
  docker compose run --rm --no-deps -T elk-mcp python -c '
import subprocess, sys
sys.path.insert(0, "/app/scripts")
import gitnexus_bin as g
info = g.locate()
print(g.describe(info))
if info.get("error"):
    sys.exit(1)
r = subprocess.run(info["argv"] + ["--version"], capture_output=True, text=True, env=g.run_env(info))
print((r.stdout or r.stderr).strip()[-800:])
sys.exit(r.returncode)' \
    || die "容器内无法运行安装的 gitnexus（见上方输出）：可执行 docker volume rm elk-mcp_gitnexus-tools 后重新运行本脚本"
}

# 在镜像内运行 gen-token：宿主机不需要 Python；token 只在终端显示这一次
gen_admin_token() {
  out=$(docker compose run --rm --no-deps -T elk-mcp python /app/scripts/mcp_http.py gen-token admin) \
    || die "生成 token 失败"
  token=$(printf '%s\n' "$out" | sed -n 's/^  \(elk_[A-Za-z0-9_-]*\)$/\1/p')
  line=$(printf '%s\n' "$out" | sed -n 's/^  \({.*}\)$/\1/p' \
    | sed 's/"prod": false, "admin": false/"prod": true, "admin": true/')
  [ -n "$token" ] && [ -n "$line" ] || die "无法解析 gen-token 输出：$out"
  set_env ELK_HTTP_TOKENS "[$line]"
  ADMIN_TOKEN=$token
}

# 存储目录写成宿主机路径时预先创建：否则 Docker 会以 root 自动创建，elk-mcp（uid 10001）无法写入。
# ELK_DATA_DIR / ELK_REPOS_DIR 由 elk-mcp 读写，需授权给 10001；ELK_GITNEXUS_TOOLS_DIR / ELK_CADDY_DIR
# 由以 root 运行的 gitnexus-install / caddy 写入，只需存在。config/ 下实际使用的 json 文件同样授权给 10001。
# Linux 上 bind mount 按宿主机权限判断；Docker Desktop 不受影响。
prepare_dirs() {
  dirs=""
  for v in ELK_DATA_DIR ELK_REPOS_DIR ELK_GITNEXUS_TOOLS_DIR ELK_CADDY_DIR; do
    d=$(env_get "$v")
    case "$d" in
      "") continue ;;
      /*) mkdir -p "$d" ;;
      *) die "$v 需要写宿主机绝对路径（当前：$d），或删掉这一行改用 Docker 数据卷" ;;
    esac
    case "$v" in ELK_DATA_DIR|ELK_REPOS_DIR) dirs="$dirs $d" ;; esac
  done
  files=""
  for f in config/envs.json config/tokens.json; do
    [ -f "$f" ] && files="$files $f"
  done
  [ "$(uname -s)" = Linux ] || return 0
  [ -n "$dirs$files" ] || return 0
  if [ "$(id -u)" = 0 ]; then
    # shellcheck disable=SC2086
    [ -z "$dirs" ] || chown 10001:10001 $dirs
    # shellcheck disable=SC2086
    [ -z "$files" ] || { chown 10001:10001 $files && chmod 600 $files; }
  else
    echo "提示：容器用户 uid 为 10001，请 sudo chown 10001:10001${dirs}${files}，否则容器可能无法读写"
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
  [ -n "$LOAD" ] && load_image "$LOAD"
  init_files
  [ -n "$IMAGE" ] && set_env ELK_IMAGE "$IMAGE"
  [ -n "$TLS" ] && set_env COMPOSE_PROFILES tls
  [ -n "$PUBLISH" ] && set_env ELK_HTTP_PUBLISH "$PUBLISH"
  if [ -n "$CREATED" ]; then
    echo "已从模板创建：$CREATED"
    echo "请在 .env 中填写 ELK 环境（ELK_ENVS、ELK_<环境>_URL/INDEX/USERNAME/PASSWORD）、GitLab 令牌等，再重新运行本脚本"
    exit 0
  fi
  check_elk_config
  prepare_dirs   # 在任何 docker compose run 之前：否则 Docker 会以 root 自动创建宿主机数据目录
  ensure_image
  GN_CHANGED=""
  install_gitnexus
  verify_gitnexus
  ADMIN_TOKEN=""
  [ -n "$(env_get ELK_HTTP_TOKENS)" ] || [ -f config/tokens.json ] || gen_admin_token
  docker compose up -d
  # 已在运行的 elk-mcp 配置没变时 up 不会重建它：gitnexus 换了版本要重启才会加载新文件
  [ -n "$GN_CHANGED" ] && docker compose restart elk-mcp
  wait_healthy
  addr=$(docker compose port elk-mcp 8765)
  echo
  echo "elk MCP 已在后台运行：http://$addr/mcp（健康检查 /healthz，镜像 $(image_ref)）"
  if [ -n "$ADMIN_TOKEN" ]; then
    echo "已生成管理员 token（prod、admin 权限，只显示这一次，请妥善保存）："
    echo
    echo "  $ADMIN_TOKEN"
    echo
    echo "已写入 .env 的 ELK_HTTP_TOKENS。添加成员：docker compose exec elk-mcp python /app/scripts/mcp_http.py gen-token <成员名>，"
    echo "把输出的 JSON 加进 ELK_HTTP_TOKENS 数组后运行 ./docker-up.sh"
  fi
  boot_hint
}

ACTION=up IMAGE="" LOAD="" TLS="" PUBLISH=""
while [ $# -gt 0 ]; do
  case "$1" in
    up|status|logs|down) ACTION=$1 ;;
    --image) [ $# -ge 2 ] || usage 1; IMAGE=$2; shift ;;
    --load) [ $# -ge 2 ] || usage 1; LOAD=$2; shift ;;
    --tls) TLS=1 ;;
    --publish) [ $# -ge 2 ] || usage 1; PUBLISH=$2; shift ;;
    -h|--help) usage ;;
    *) echo "未知参数：$1" >&2; usage 1 ;;
  esac
  shift
done

if in_source_repo; then
  die "当前在源码仓库中。请在源码仓库执行 deploy/build-image.sh 构建镜像并生成部署包，再到服务器的部署目录（如 /opt/elk-mcp）解压后运行 ./docker-up.sh"
fi
# --load 的相对路径按调用时的目录解析，而脚本已切换到部署目录
case "$LOAD" in ""|/*) ;; *) LOAD="$CALLER_DIR/$LOAD" ;; esac

check_docker
case "$ACTION" in
  up) cmd_up ;;
  status) docker compose ps ;;
  logs) docker compose logs -f --tail 200 ;;
  down) docker compose down ;;
esac
