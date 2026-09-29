# 构建上下文为 skill 根目录：docker compose -f deploy/docker-compose.yml build
# target=base：只提供 elk_* 日志查询（约 150MB）
# target=code：额外带 node + gitnexus，支持 code_* 源码定位、GitLab 仓库同步后的索引预建，
#              并在 /gitnexus/mcp 向成员提供服务端的 GitNexus 查询
FROM python:3.12-slim-bookworm AS base
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 10001 elk \
 && mkdir -p /data /config /repos && chown elk:elk /data /repos
COPY scripts/ /app/scripts/
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ELK_HTTP_HOST=0.0.0.0 \
    ELK_HTTP_PORT=8765 \
    ELK_CONFIG=/config/envs.json \
    ELK_HTTP_TOKENS_FILE=/config/tokens.json \
    ELK_CODE_CACHE=/data/cache
USER elk
EXPOSE 8765
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ['ELK_HTTP_PORT'],timeout=3)"
CMD ["python", "/app/scripts/mcp_http.py"]

FROM base AS code
USER root
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
ARG GITNEXUS_VERSION=1.6.12
# tree-sitter 等原生模块在部分平台（如 linux/arm64）没有预编译包，需要现场编译；编译完即卸载工具链
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && apt-get update && apt-get install -y --no-install-recommends build-essential \
 && npm install -g "gitnexus@${GITNEXUS_VERSION}" \
 && npm cache clean --force && rm -rf /root/.cache/node-gyp \
 && apt-get purge -y --auto-remove build-essential && rm -rf /var/lib/apt/lists/*
ENV ELK_CODE_ROOT=/repos \
    ELK_CODE_GITNEXUS=/usr/local/bin/gitnexus \
    GITNEXUS_HOME=/data/gitnexus \
    ELK_GITNEXUS_MCP=1
USER elk
