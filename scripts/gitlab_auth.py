"""GitLab 认证：为 API 请求和 git 进程提供凭据。供 repo_sync.py（列项目、clone）与 code_core.py（fetch、按需取文件）共用，仅标准库。

认证方式 ELK_GITLAB_AUTH（不设时按已配置的凭据自动判断：有令牌用 token，有账号密码用 password）：
  token     访问令牌（个人 / 组 / 项目令牌），ELK_GITLAB_TOKEN，需 read_api + read_repository
  oauth     OAuth access token，ELK_GITLAB_TOKEN（过期需由外部更新）
  password  账号密码，ELK_GITLAB_USERNAME + ELK_GITLAB_PASSWORD。
            API 不接受密码直接访问，需经 OAuth 密码模式（POST /oauth/token）换取临时令牌，过期前自动重新换取；
            GitLab 要求 OAuth 应用时再配 ELK_GITLAB_CLIENT_ID / ELK_GITLAB_CLIENT_SECRET。
            账号开启双因素认证或仅能 SSO 登录时不可用，请改用令牌。
拉取代码的协议 ELK_GITLAB_GIT_PROTOCOL：
  https（默认）凭据同上，经 git 的 http.extraHeader 注入
  ssh          使用 ssh_url_to_repo，私钥 ELK_GITLAB_SSH_KEY（API 仍需上面的令牌或账号密码）
其他：ELK_GITLAB_CA_CERT 内部 CA 证书。每个凭据变量都可改用 <变量>_FILE 指向文件（兼容 Docker secrets）。

凭据只存在于本进程内存与子进程环境变量中：不写入磁盘、.git/config、remote URL 或命令行参数，也只发给 ELK_GITLAB_URL。
"""
import base64
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import elk_core as core

MODES = ("token", "oauth", "password")
_CACHE = {}  # (url, username) → {"token", "expires"} 或 {"error", "until"}：密码模式换来的临时令牌 / 最近的失败
FAIL_TTL = 300  # 换取失败后 5 分钟内不再重试：git_env 在每条 git 命令前调用，GitLab 不可达时不能每次都等超时
_LOCK = threading.Lock()


def _secret(environ, name):
    """变量值，或 <变量>_FILE 指向的文件内容（去掉首尾空白）。"""
    if environ.get(name):
        return environ[name]
    path = environ.get(name + "_FILE")
    if path:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as f:
                return f.read().strip()
        except OSError as e:
            raise core.ElkError("读取 %s_FILE（%s）失败：%s" % (name, path, e))
    return ""


def load(environ=None):
    """GitLab 连接配置；未配置 ELK_GITLAB_URL 时返回 None。只做解析，不发请求。"""
    environ = os.environ if environ is None else environ
    url = (environ.get("ELK_GITLAB_URL") or "").rstrip("/")
    if not url:
        return None
    cfg = {"url": url,
           "token": _secret(environ, "ELK_GITLAB_TOKEN"),
           "username": environ.get("ELK_GITLAB_USERNAME") or "",
           "password": _secret(environ, "ELK_GITLAB_PASSWORD"),
           "client_id": environ.get("ELK_GITLAB_CLIENT_ID") or "",
           "client_secret": _secret(environ, "ELK_GITLAB_CLIENT_SECRET"),
           "ca": os.path.expanduser(environ.get("ELK_GITLAB_CA_CERT") or ""),
           "protocol": (environ.get("ELK_GITLAB_GIT_PROTOCOL") or "https").lower(),
           "ssh_key": os.path.expanduser(environ.get("ELK_GITLAB_SSH_KEY") or ""),
           "known_hosts": ""}
    mode = (environ.get("ELK_GITLAB_AUTH") or "").lower()
    if not mode:
        mode = "token" if cfg["token"] else ("password" if cfg["username"] and cfg["password"] else "")
    cfg["mode"] = mode
    return cfg


def problem(cfg):
    """配置是否完整；有问题返回说明，否则 None。"""
    if cfg["mode"] not in MODES:
        return ("未配置 GitLab 凭据：设置 ELK_GITLAB_TOKEN（访问令牌），或 ELK_GITLAB_USERNAME + ELK_GITLAB_PASSWORD"
                if not cfg["mode"] else "ELK_GITLAB_AUTH 只能是 %s，当前为 %s" % ("/".join(MODES), cfg["mode"]))
    if cfg["mode"] in ("token", "oauth") and not cfg["token"]:
        return "认证方式为 %s，但未设置 ELK_GITLAB_TOKEN" % cfg["mode"]
    if cfg["mode"] == "password" and not (cfg["username"] and cfg["password"]):
        return "认证方式为 password，但 ELK_GITLAB_USERNAME / ELK_GITLAB_PASSWORD 不完整"
    if cfg["protocol"] not in ("https", "ssh"):
        return "ELK_GITLAB_GIT_PROTOCOL 只能是 https 或 ssh"
    if cfg["protocol"] == "ssh" and cfg["ssh_key"] and not os.path.exists(cfg["ssh_key"]):
        return "ELK_GITLAB_SSH_KEY 指向的私钥不存在：%s" % cfg["ssh_key"]
    return None


def describe(cfg):
    """给人看的认证方式说明（不含任何凭据）。"""
    mode = {"token": "访问令牌", "oauth": "OAuth 令牌",
            "password": "账号密码（%s，经 OAuth 换取临时令牌）" % cfg["username"]}.get(cfg["mode"], "未配置")
    return "%s，代码经 %s 拉取" % (mode, "SSH" if cfg["protocol"] == "ssh" else "HTTPS")


def _ssl(cfg):
    return ssl.create_default_context(cafile=cfg["ca"] or None)


# ---------------------------------------------------------------- 密码模式：OAuth 密码授权换取临时令牌

def _password_token(cfg):
    key = (cfg["url"], cfg["username"])
    with _LOCK:
        hit = _CACHE.get(key)
        if hit:
            if hit.get("error") and time.time() < hit["until"]:
                raise core.ElkError(hit["error"])
            if hit.get("token") and hit["expires"] - time.time() > 120:
                return hit["token"]
        try:
            return _exchange(cfg, key)
        except core.ElkError as e:
            _CACHE[key] = {"error": str(e), "until": time.time() + FAIL_TTL}
            raise


def _exchange(cfg, key):
    """POST /oauth/token（密码模式）换取临时令牌并缓存。调用方已持有 _LOCK。"""
    form = {"grant_type": "password", "username": cfg["username"], "password": cfg["password"]}
    if cfg["client_id"]:
        form.update(client_id=cfg["client_id"], client_secret=cfg["client_secret"])
    req = urllib.request.Request(cfg["url"] + "/oauth/token", data=urllib.parse.urlencode(form).encode(),
                                 method="POST", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ssl(cfg)) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise core.ElkError(_oauth_error(e))
    except (urllib.error.URLError, OSError) as e:
        raise core.ElkError("无法访问 GitLab（%s）：%s" % (cfg["url"], getattr(e, "reason", e)))
    token = data.get("access_token")
    if not token:
        raise core.ElkError("GitLab 未返回 access_token，请改用访问令牌")
    # 未返回有效期时按 GitLab 默认的 2 小时处理
    _CACHE[key] = {"token": token, "expires": time.time() + int(data.get("expires_in") or 7200)}
    return token


def _oauth_error(e):
    try:
        body = json.loads(e.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        body = {}
    code = body.get("error", "")
    if code == "invalid_grant":
        return ("GitLab 账号密码认证失败：用户名或密码错误，或该账号开启了双因素认证 / 只能通过 SSO 登录。"
                "这类账号请改用访问令牌（ELK_GITLAB_TOKEN）")
    if code in ("unauthorized_client", "invalid_client", "unsupported_grant_type"):
        return ("该 GitLab 未开放账号密码授权（OAuth 密码模式），或要求提供 OAuth 应用："
                "请设置 ELK_GITLAB_CLIENT_ID / ELK_GITLAB_CLIENT_SECRET，或改用访问令牌（ELK_GITLAB_TOKEN）")
    return "GitLab 账号密码认证失败（HTTP %s %s）" % (e.code, code or e.reason)


def invalidate(cfg):
    """API 返回 401 时丢弃缓存的临时令牌，下次重新换取。"""
    with _LOCK:
        _CACHE.pop((cfg["url"], cfg["username"]), None)


# ---------------------------------------------------------------- 对外：API 请求头 / git 环境

def api_headers(cfg):
    if cfg["mode"] == "token":
        return {"PRIVATE-TOKEN": cfg["token"]}
    if cfg["mode"] == "oauth":
        return {"Authorization": "Bearer " + cfg["token"]}
    return {"Authorization": "Bearer " + _password_token(cfg)}


def _git_basic(cfg):
    """git over HTTPS 的 Basic 凭据。令牌类用 oauth2:<令牌>；密码模式优先用换来的临时令牌，换不到时回退为账号密码。"""
    if cfg["mode"] in ("token", "oauth"):
        return "oauth2", cfg["token"]
    try:
        return "oauth2", _password_token(cfg)
    except core.ElkError:
        return cfg["username"], cfg["password"]


def git_env(environ):
    """给 git 子进程的附加环境变量。HTTPS：以 GIT_CONFIG_*（git 2.31+）注入限定在 GitLab 地址的认证头与 CA；
    SSH：指定私钥，known_hosts 放在缓存目录（容器重建后仍保留，首次连接自动记录主机指纹）。"""
    cfg = load(environ)
    if cfg is None or problem(cfg):
        return {}
    out, items = {}, []
    if cfg["protocol"] == "https":
        user, secret = _git_basic(cfg)
        basic = base64.b64encode(("%s:%s" % (user, secret)).encode()).decode()
        items.append(("http.%s/.extraHeader" % cfg["url"], "Authorization: Basic " + basic))
    if cfg["ca"]:
        items.append(("http.%s/.sslCAInfo" % cfg["url"], cfg["ca"]))
    if cfg["protocol"] == "ssh" and cfg["ssh_key"]:
        cache = os.path.expanduser(environ.get("ELK_CODE_CACHE") or "~/.cache/elk-log-analysis")
        os.makedirs(cache, exist_ok=True)
        hosts = os.path.join(cache, "known_hosts")
        out["GIT_SSH_COMMAND"] = ("ssh -i '%s' -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 "
                                  "-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile='%s'"
                                  % (cfg["ssh_key"], hosts))
    if items:
        n = int(environ.get("GIT_CONFIG_COUNT") or 0)
        out["GIT_CONFIG_COUNT"] = str(n + len(items))
        for i, (k, v) in enumerate(items, n):
            out["GIT_CONFIG_KEY_%d" % i], out["GIT_CONFIG_VALUE_%d" % i] = k, v
    return out


def clone_url(cfg, project):
    if cfg["protocol"] == "ssh":
        if not project.get("ssh_url_to_repo"):
            raise core.ElkError("GitLab 未返回 %s 的 SSH 地址（实例可能关闭了 SSH）" % project.get("path_with_namespace"))
        return project["ssh_url_to_repo"]
    return project["http_url_to_repo"]
