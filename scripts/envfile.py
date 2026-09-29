"""读取部署用的 .env（KEY=VALUE），供 mcp_http.py 与 elk.py 在导入 elk_core 之前调用（其 CONFIG_PATH 在导入时读取）。"""
import os
import sys

PATH_KEYS = ("ELK_CONFIG", "ELK_HTTP_TOKENS_FILE", "ELK_HTTP_CERT", "ELK_HTTP_KEY", "ELK_CODE_CACHE",
             "ELK_CODE_ROOT", "ELK_REPO_SYNC_DIR", "ELK_GITLAB_CA_CERT", "ELK_GITLAB_SSH_KEY",
             "ELK_GITLAB_TOKEN_FILE", "ELK_GITLAB_PASSWORD_FILE", "ELK_GITLAB_CLIENT_SECRET_FILE")


def load_env_file(path):
    """与 docker compose env_file 相同：不做引号/变量展开，# 开头为注释。已存在的环境变量优先；
    PATH_KEYS 中的相对路径按 .env 所在目录解析，便于各平台原生部署共用一份配置。"""
    base = os.path.dirname(os.path.abspath(path))
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = (x.strip() for x in line.split("=", 1))
            if key in PATH_KEYS and value and not value.startswith("~") and not os.path.isabs(value):
                value = os.path.join(base, value)
            if value:
                os.environ.setdefault(key, value)


def apply_from_argv():
    """处理命令行中的 --env-file <路径>（并从 sys.argv 移除），或环境变量 ELK_HTTP_ENV_FILE。"""
    if "--env-file" in sys.argv:
        i = sys.argv.index("--env-file")
        if i + 1 >= len(sys.argv):
            sys.exit("用法: --env-file <.env 路径>")
        load_env_file(sys.argv[i + 1])
        del sys.argv[i:i + 2]
    elif os.environ.get("ELK_HTTP_ENV_FILE"):
        load_env_file(os.environ["ELK_HTTP_ENV_FILE"])
