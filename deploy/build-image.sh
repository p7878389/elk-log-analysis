#!/bin/sh
# 在开发机或 CI 中从源码构建 elk-mcp 镜像，并生成服务器用的部署包（服务器上不需要源码，也不在源码目录运行 compose）。
#   deploy/build-image.sh [--platform linux/amd64] [--image 名称] [--tag 标签] [--push | --save]
#       --platform  服务器的 CPU 架构，默认 linux/amd64；ARM 服务器用 linux/arm64
#       --image     镜像名，默认 elk-mcp；推送时写镜像仓库完整地址，如 registry.example.com/ops/elk-mcp
#       --tag       默认 <提交短哈希>，有未提交改动时加 -dirty；同时打 latest 标签
#       --push      构建后推送到镜像仓库（需先 docker login），服务器 docker compose up 时自动拉取
#       --save      导出镜像包 dist/elk-mcp-image-<标签>.tar.gz，拷到服务器用 docker-up.sh --load 载入
#   产物：dist/elk-mcp-deploy-<标签>.tar.gz（docker-compose.yml、.env.example、config 模板、Caddyfile、启动脚本）
#   镜像不含 node / gitnexus：由 docker-compose.yml 的 gitnexus-install 在服务器上用官方 node 镜像安装
set -eu
cd "$(dirname "$0")/.."

die() { echo "$*" >&2; exit 1; }

usage() { sed -n '2,10p' deploy/build-image.sh | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

PLATFORM=linux/amd64 IMAGE=elk-mcp TAG="" MODE=load
while [ $# -gt 0 ]; do
  case "$1" in
    --platform) [ $# -ge 2 ] || usage 1; PLATFORM=$2; shift ;;
    --image) [ $# -ge 2 ] || usage 1; IMAGE=$2; shift ;;
    --tag) [ $# -ge 2 ] || usage 1; TAG=$2; shift ;;
    --push) MODE=push ;;
    --save) MODE=save ;;
    -h|--help) usage ;;
    *) echo "未知参数：$1" >&2; usage 1 ;;
  esac
  shift
done
case "$PLATFORM" in *,*) die "--platform 只支持单一架构（与服务器一致）" ;; esac

command -v docker >/dev/null 2>&1 || die "未找到 docker"
docker buildx version >/dev/null 2>&1 || die "需要 docker buildx（Docker 23+ 自带）"
docker info >/dev/null 2>&1 || die "Docker 未运行"

# 版本号取自源码提交：镜像可追溯到具体代码，回滚时按标签切换
REV=$(git rev-parse --short HEAD 2>/dev/null) || REV=nogit
if [ "$REV" != nogit ] && [ -n "$(git status --porcelain -- Dockerfile .dockerignore scripts deploy)" ]; then
  REV="$REV-dirty"
fi
[ -n "$TAG" ] || TAG="$REV"
REF="$IMAGE:$TAG"

case "$MODE" in push) OUT=--push ;; *) OUT=--load ;; esac
# 同时打 latest 标签，方便在服务器上用固定名称引用最新版本
ALIAS=""
[ "$TAG" = latest ] || ALIAS="$IMAGE:latest"
echo "构建 $REF（platform=$PLATFORM）"
docker buildx build \
  --platform "$PLATFORM" \
  --build-arg VERSION="$TAG" \
  --build-arg VCS_REF="$REV" \
  -t "$REF" ${ALIAS:+-t "$ALIAS"} \
  $OUT \
  .

mkdir -p dist
if [ "$MODE" = save ]; then
  IMAGE_TAR="dist/elk-mcp-image-$TAG.tar.gz"
  echo "导出镜像包 $IMAGE_TAR"
  docker save "$REF" | gzip > "$IMAGE_TAR"
fi

# 部署包只含模板与脚本，不含 .env、envs.json、tokens.json：在服务器上覆盖解压不会冲掉已有配置
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
PKG="$STAGE/elk-mcp"
mkdir -p "$PKG/config"
cp deploy/docker-compose.yml deploy/Caddyfile deploy/docker-up.sh deploy/docker-up.ps1 "$PKG/"
cp deploy/config/envs.example.json deploy/config/tokens.example.json "$PKG/config/"
# 模板里预填本次镜像：首次部署复制 .env 后直接可用
sed "s|^# *ELK_IMAGE=.*|ELK_IMAGE=$REF|" deploy/.env.example > "$PKG/.env.example"
chmod 755 "$PKG/docker-up.sh"
DEPLOY_TAR="dist/elk-mcp-deploy-$TAG.tar.gz"
# 去掉 macOS 扩展属性与本机属主，Linux 上解压干净
if tar --version 2>/dev/null | grep -q GNU; then
  tar -C "$STAGE" --owner=0 --group=0 --numeric-owner -czf "$DEPLOY_TAR" elk-mcp
else
  COPYFILE_DISABLE=1 tar -C "$STAGE" --uid 0 --gid 0 --no-xattrs -czf "$DEPLOY_TAR" elk-mcp
fi

echo
echo "镜像：$REF"
echo "部署包：$DEPLOY_TAR"
[ "$MODE" = save ] && echo "镜像包：$IMAGE_TAR"
echo
echo "服务器上（部署目录与源码分离，如 /opt/elk-mcp）："
echo "  sudo tar -xzf elk-mcp-deploy-$TAG.tar.gz -C /opt      # 解压到 /opt/elk-mcp，已有 .env 与 config/*.json 不受影响"
case "$MODE" in
  save) echo "  sudo /opt/elk-mcp/docker-up.sh --load elk-mcp-image-$TAG.tar.gz" ;;
  push) echo "  sudo /opt/elk-mcp/docker-up.sh --image $REF" ;;
  *) echo "  镜像只在本机 Docker 中：本机就是服务器时运行 /opt/elk-mcp/docker-up.sh --image $REF，否则改用 --push 或 --save" ;;
esac
