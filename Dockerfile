# syntax=docker/dockerfile:1
# 从源码构建 elk-mcp 镜像，构建上下文为仓库根目录（.dockerignore 只放行 scripts/）。推荐用 deploy/build-image.sh，
# 它会打版本标签、推送或导出镜像包，并生成服务器用的部署包；也可以直接：docker build -t elk-mcp .
# 镜像只含 Python 服务（标准库）与 git，不含 node / gitnexus：部署时由 docker-compose.yml 中的 gitnexus-install
# 用官方 node 镜像把它们装进数据卷，挂载到 /opt/gitnexus/current（见下方 ELK_CODE_* 约定），供 code_* 与 /gitnexus/mcp 使用。
# 没有挂载时日志查询照常可用，code_* 与 /gitnexus/mcp 会提示未找到 gitnexus。
# 依赖层在前、源码层在后：只改 scripts/ 时复用已缓存的 apt 层。

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}
# procps：code_* 用 ps 判断后台索引进程是否仍在运行（slim 镜像默认没有）
# tini：作为 PID 1 转发 SIGTERM（docker stop 立即退出，不必等 10 秒被强杀），并回收 nohup 启动的后台索引进程
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client ca-certificates tzdata procps tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 10001 elk \
 && mkdir -p /data /config /repos /opt/gitnexus && chown elk:elk /data /repos
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ELK_HTTP_HOST=0.0.0.0 \
    ELK_HTTP_PORT=8765 \
    ELK_CONFIG=/config/envs.json \
    ELK_HTTP_TOKENS_FILE=/config/tokens.json \
    ELK_CODE_CACHE=/data/cache \
    ELK_CODE_ROOT=/repos \
    GITNEXUS_HOME=/data/gitnexus \
    ELK_CODE_NODE=/opt/gitnexus/current/bin/node \
    ELK_CODE_GITNEXUS=/opt/gitnexus/current/lib/node_modules/gitnexus \
    ELK_GITNEXUS_MCP=1 \
    PATH=/opt/gitnexus/current/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin
EXPOSE 8765
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ['ELK_HTTP_PORT'],timeout=3)"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/scripts/mcp_http.py"]

COPY scripts/ /app/scripts/
ARG VERSION=dev
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="elk-mcp" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"
USER elk
