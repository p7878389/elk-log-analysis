"""GitLab 认证：为 API 请求和 git 进程提供凭据。供 repo_sync.py（列项目、clone）与 code_core.py（fetch、按需取文件）共用，仅标准库。

GitLab API（列出项目）只接受令牌或账号密码；SSH 只能用于拉代码。所以认证分两部分：

API 凭据 ELK_GITLAB_AUTH：
  token     访问令牌（个人 / 组 / 项目令牌），ELK_GITLAB_TOKEN，需 read_api + read_repository
  oauth     OAuth access token，ELK_GITLAB_TOKEN（过期需由外部更新）
  password  账号密码，ELK_GITLAB_USERNAME + ELK_GITLAB_PASSWORD。API 不接受密码直接访问，
            经 OAuth 密码模式（POST /oauth/token）换取临时令牌，过期前自动续期；
            GitLab 要求 OAuth 应用时再配 ELK_GITLAB_CLIENT_ID / ELK_GITLAB_CLIENT_SECRET。
            账号开启双因素认证或仅能 SSO 登录时不可用。
  auto      （默认，未配置上面任何凭据时）读取本机 git 凭据管理器里已保存的该 GitLab 账号
            （macOS 钥匙串 / Windows 凭据管理器 / Linux store、cache 等），依次当作访问令牌、OAuth 令牌、密码验证。
拉代码的协议 ELK_GITLAB_GIT_PROTOCOL：
  auto      （默认）先试 SSH（沿用本机 ~/.ssh 配置与 ssh-agent，或 ELK_GITLAB_SSH_KEY），不通再用 HTTPS
  ssh       使用 ssh_url_to_repo
  https     环境变量配置了凭据时注入给 git；否则沿用本机 git 凭据管理器
其他：ELK_GITLAB_CA_CERT 内部 CA 证书。每个凭据变量都可改用 <变量>_FILE 指向文件（兼容 Docker secrets）。

凭据只存在于本进程内存与子进程环境变量中：不写入磁盘、.git/config、remote URL 或命令行参数，也只发给 ELK_GITLAB_URL。
自动检测只读取本机凭据，不修改凭据管理器、~/.ssh 或 known_hosts，也不会弹出任何登录/授权窗口。
"""
import base64
import json
import os
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import elk_core as core

MODES = ("token", "oauth", "password")
PROTOCOLS = ("auto", "ssh", "https")
_CACHE = {}  # (url, username) → {"token", "expires"} 或 {"error", "until"}：密码模式换来的临时令牌 / 最近的失败
FAIL_TTL = 300  # 换取失败后 5 分钟内不再重试：git_env 在每条 git 命令前调用，GitLab 不可达时不能每次都等超时
_LOCK = threading.Lock()
_HELPER = {}  # url → (过期时间, (用户名, 密码) 或 None)：凭据管理器读取结果的进程内缓存
HELPER_TTL = 300
TOKEN_PREFIXES = ("glpat-", "gloas-", "gldt-", "glptt-", "glsoat-", "glft-")
# 探测凭据/连通性时禁止任何交互：终端提示、askpass、Git Credential Manager 的登录窗口、ssh 口令
NO_PROMPT = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
             "GIT_OPTIONAL_LOCKS": "0"}


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
           "protocol": (environ.get("ELK_GITLAB_GIT_PROTOCOL") or "auto").lower(),
           "ssh_key": os.path.expanduser(environ.get("ELK_GITLAB_SSH_KEY") or ""),
           "cache": os.path.expanduser(environ.get("ELK_CODE_CACHE") or "~/.cache/elk-log-analysis"),
           "source": "环境变量",   # API 凭据来源；自动检测后改为「git 凭据管理器」
           "transport": None,      # 实际拉取协议，resolve_transport 之后确定
           "detect": []}           # 自动检测过程，给人看
    mode = (environ.get("ELK_GITLAB_AUTH") or "").lower()
    if not mode:
        mode = "token" if cfg["token"] else ("password" if cfg["username"] and cfg["password"] else "auto")
    cfg["mode"] = mode
    return cfg


def problem(cfg):
    """静态配置检查；有问题返回说明，否则 None。auto 模式的凭据要到运行时检测。"""
    if cfg["mode"] not in MODES + ("auto",):
        return "ELK_GITLAB_AUTH 只能是 %s 或 auto，当前为 %s" % ("/".join(MODES), cfg["mode"])
    if cfg["mode"] in ("token", "oauth") and not cfg["token"]:
        return "认证方式为 %s，但未设置 ELK_GITLAB_TOKEN" % cfg["mode"]
    if cfg["mode"] == "password" and not (cfg["username"] and cfg["password"]):
        return "认证方式为 password，但 ELK_GITLAB_USERNAME / ELK_GITLAB_PASSWORD 不完整"
    if cfg["protocol"] not in PROTOCOLS:
        return "ELK_GITLAB_GIT_PROTOCOL 只能是 %s" % "/".join(PROTOCOLS)
    if cfg["ssh_key"] and not os.path.exists(cfg["ssh_key"]):
        return "ELK_GITLAB_SSH_KEY 指向的私钥不存在：%s" % cfg["ssh_key"]
    return None


def describe(cfg):
    """给人看的认证方式说明（不含任何凭据）。"""
    api = {"token": "访问令牌", "oauth": "OAuth 令牌",
           "password": "账号密码（%s，经 OAuth 换取临时令牌）" % cfg["username"],
           "auto": "自动检测（git 凭据管理器）"}.get(cfg["mode"], cfg["mode"])
    if cfg["mode"] != "auto" and cfg["source"] != "环境变量":
        api += "，来自%s" % cfg["source"]
    transport = cfg["transport"] or cfg["protocol"]
    transport = {"ssh": "SSH", "https": "HTTPS", "auto": "自动（优先 SSH）"}[transport]
    return "API：%s；代码经 %s 拉取" % (api, transport)


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


# ---------------------------------------------------------------- API

def api_headers(cfg):
    if cfg["mode"] == "token":
        return {"PRIVATE-TOKEN": cfg["token"]}
    if cfg["mode"] == "oauth":
        return {"Authorization": "Bearer " + cfg["token"]}
    if cfg["mode"] == "password":
        return {"Authorization": "Bearer " + _password_token(cfg)}
    raise core.ElkError("尚未确定 GitLab API 凭据（自动检测未完成）")


def api(cfg, path, params=None):
    """GET /api/v4<path>，返回 (数据, 下一页页码或 None)。凭据只在请求头中，错误信息不含凭据。"""
    qs = "?" + urllib.parse.urlencode(params) if params else ""
    for attempt in (1, 2):
        req = urllib.request.Request(cfg["url"] + "/api/v4" + path + qs,
                                     headers=dict(api_headers(cfg), Accept="application/json"))
        try:
            with urllib.request.urlopen(req, timeout=30, context=_ssl(cfg)) as r:
                return json.loads(r.read().decode("utf-8")), (r.headers.get("X-Next-Page") or None)
        except urllib.error.HTTPError as e:
            if e.code == 401 and cfg["mode"] == "password" and attempt == 1:
                invalidate(cfg)  # 临时令牌提前失效：重新换取后重试一次
                continue
            hint = {401: "凭据无效或已过期", 403: "凭据缺少 read_api 权限",
                    404: "组或路径不存在（或无权限）"}.get(e.code, "")
            raise core.ElkError("GitLab API %s 返回 %s %s" % (path, e.code, hint or e.reason))
        except (urllib.error.URLError, OSError) as e:
            raise core.ElkError("无法访问 GitLab（%s）：%s" % (cfg["url"], getattr(e, "reason", e)))


# ---------------------------------------------------------------- 自动检测：本机已有的凭据

def helper_credential(cfg):
    """从本机 git 凭据管理器读取该 GitLab 已保存的账号（git credential fill，禁止任何交互），进程内缓存 5 分钟。
    只读：之后 git 访问该 GitLab 时关闭凭据管理器调用、改由本模块注入，git 就不会在认证失败时
    自动删除（credential reject）或改写用户保存的账号。没有返回 None。"""
    hit = _HELPER.get(cfg["url"])
    if hit and hit[0] > time.time():
        return hit[1]
    cred = _read_helper(cfg)
    _HELPER[cfg["url"]] = (time.time() + HELPER_TTL, cred)
    return cred


def _read_helper(cfg):
    parts = urllib.parse.urlsplit(cfg["url"])
    query = "protocol=%s\nhost=%s\n\n" % (parts.scheme, parts.netloc)
    try:
        r = subprocess.run(["git", "credential", "fill"], input=query, capture_output=True, text=True,
                           timeout=20, env=dict(os.environ, **NO_PROMPT))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    kv = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    return (kv["username"], kv["password"]) if kv.get("username") and kv.get("password") else None


def resolve_api(cfg):
    """确定 API 凭据：环境变量配置了就直接用；auto 时从 git 凭据管理器取出已保存的账号，
    依次当作访问令牌、OAuth 令牌、密码（经 OAuth 换令牌）去调 /user，第一个成功的即采用。返回 GitLab 用户名。"""
    if cfg["mode"] != "auto":
        user, _ = api(cfg, "/user")
        return user.get("username") or user.get("name") or "?"
    cred = helper_credential(cfg)
    if not cred:
        raise core.ElkError(
            "未找到可用于 GitLab API 的凭据：环境变量未配置，git 凭据管理器里也没有 %s 的账号。"
            "请设置 ELK_GITLAB_TOKEN（访问令牌）或 ELK_GITLAB_USERNAME / ELK_GITLAB_PASSWORD，"
            "或先用 git 通过 HTTPS 访问一次该 GitLab 让凭据管理器保存账号。注意 SSH 只能拉代码，不能列出项目"
            % urllib.parse.urlsplit(cfg["url"]).netloc)
    username, secret = cred
    tokenish = secret.startswith(TOKEN_PREFIXES) or username in ("oauth2", "PRIVATE-TOKEN", "gitlab-ci-token")
    # 像令牌的先按令牌验证；否则先按密码验证，避免把密码放进令牌请求头
    tries = ([("token", "访问令牌"), ("oauth", "OAuth 令牌"), ("password", "密码")] if tokenish
             else [("password", "密码"), ("token", "访问令牌"), ("oauth", "OAuth 令牌")])
    errors = []
    for mode, label in tries:
        trial = dict(cfg, mode=mode, token=secret if mode != "password" else "",
                     username=username, password=secret if mode == "password" else "")
        try:
            user, _ = api(trial, "/user")
        except core.ElkError as e:
            errors.append("%s：%s" % (label, e))
            continue
        cfg.update(mode=trial["mode"], token=trial["token"], username=username, password=trial["password"],
                   source="git 凭据管理器")
        cfg["detect"].append("API：git 凭据管理器中的账号 %s，按%s验证通过" % (username, label))
        return user.get("username") or username
    raise core.ElkError("git 凭据管理器中 %s 的账号无法用于 GitLab API（%s）。请改为在 .env 中配置访问令牌"
                        % (username, "；".join(errors)))


def _ls_remote(url, env):
    try:
        r = subprocess.run(["git", "ls-remote", "--heads", url], capture_output=True, text=True, timeout=45,
                           env=env)
    except subprocess.TimeoutExpired:
        return False, "超时"
    if r.returncode == 0:
        return True, None
    lines = [ln.strip() for ln in (r.stderr or "").splitlines()
             if ln.strip().lower().startswith(("fatal:", "error:", "remote:", "permission denied", "ssh:"))]
    if lines:
        return False, "；".join(lines)[:200]
    tail = (r.stderr or "").strip().splitlines()
    return False, (tail[-1] if tail else "失败")[:200]


def probe_transports(cfg, http_url, ssh_url, stop_on_success=True):
    """用 git ls-remote 实测 SSH 与 HTTPS 能否访问某个项目，与 clone/fetch 走完全相同的路径
    （~/.ssh 配置、ssh-agent、凭据管理器或注入的凭据）。返回 [(协议, 是否成功, 原因)]。"""
    env = dict(os.environ, **NO_PROMPT)
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=10")
    env.update(git_env(env, cfg))
    order = [cfg["protocol"]] if cfg["protocol"] in ("ssh", "https") else ["ssh", "https"]
    out = []
    for proto in order:
        url = ssh_url if proto == "ssh" else http_url
        if not url:
            out.append((proto, False, "GitLab 未提供 %s 地址" % proto.upper()))
            continue
        ok, why = _ls_remote(url, env)
        out.append((proto, ok, why))
        if ok and stop_on_success:
            break
    return out


def resolve_transport(cfg, http_url, ssh_url):
    """确定拉代码的协议：显式指定的直接验证；auto 时 SSH 优先，不通再用 HTTPS。"""
    results = probe_transports(cfg, http_url, ssh_url)
    for proto, ok, why in results:
        cfg["detect"].append("拉代码：%s %s" % (proto.upper(), "可用" if ok else "不可用（%s）" % why))
        if ok:
            cfg["transport"] = proto
            return proto
    raise core.ElkError("SSH 与 HTTPS 都无法拉取代码：%s" % "；".join(
        "%s：%s" % (p.upper(), w) for p, _, w in results))


# ---------------------------------------------------------------- git 子进程环境

def _git_basic(cfg):
    """git over HTTPS 的 Basic 凭据。令牌类用 oauth2:<令牌>；密码模式优先用换来的临时令牌，换不到时回退为账号密码。"""
    if cfg["mode"] in ("token", "oauth"):
        return "oauth2", cfg["token"]
    try:
        return "oauth2", _password_token(cfg)
    except core.ElkError:
        return cfg["username"], cfg["password"]


def git_env(environ, cfg=None):
    """给 git 子进程的附加环境变量：
    - HTTPS：以 GIT_CONFIG_*（git 2.31+）注入限定在 GitLab 地址的认证头。凭据来自环境变量，或（auto 时）
      从 git 凭据管理器只读取出；同时对该地址关闭凭据管理器，git 不会在失败时删除/改写用户保存的账号
    - SSH：配置了 ELK_GITLAB_SSH_KEY 时指定私钥（known_hosts 放在缓存目录）；否则沿用本机 ~/.ssh 与 ssh-agent
    - 内部 CA 证书"""
    cfg = cfg or load(environ)
    if cfg is None or problem(cfg):
        return {}
    out, items = {}, []
    if cfg["mode"] in MODES and cfg["source"] == "环境变量":
        user, secret = _git_basic(cfg)
    else:
        user, secret = helper_credential(cfg) or (None, None)
    if user:
        basic = base64.b64encode(("%s:%s" % (user, secret)).encode()).decode()
        items.append(("http.%s/.extraHeader" % cfg["url"], "Authorization: Basic " + basic))
        items.append(("credential.%s.helper" % cfg["url"], ""))  # 空值：对该地址清空凭据管理器列表
    if cfg["ca"]:
        items.append(("http.%s/.sslCAInfo" % cfg["url"], cfg["ca"]))
    if cfg["ssh_key"]:
        os.makedirs(cfg["cache"], exist_ok=True)
        out["GIT_SSH_COMMAND"] = ("ssh -i '%s' -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 "
                                  "-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile='%s'"
                                  % (cfg["ssh_key"], os.path.join(cfg["cache"], "known_hosts")))
    if items:
        n = int(environ.get("GIT_CONFIG_COUNT") or 0)
        out["GIT_CONFIG_COUNT"] = str(n + len(items))
        for i, (k, v) in enumerate(items, n):
            out["GIT_CONFIG_KEY_%d" % i], out["GIT_CONFIG_VALUE_%d" % i] = k, v
    return out


def clone_url(cfg, project):
    transport = cfg["transport"] or ("ssh" if cfg["protocol"] == "ssh" else "https")
    if transport == "ssh":
        if not project.get("ssh_url"):
            raise core.ElkError("GitLab 未返回 %s 的 SSH 地址（实例可能关闭了 SSH）" % project["path_with_namespace"])
        return project["ssh_url"]
    return project["http_url"]
