"""ELK 日志 → 源码：按 host.name 识别服务与模块，按环境选分支（可按 pod 部署时间对齐 commit），
维护只读 worktree 与 GitNexus 索引，并把异常堆栈帧定位到源码。供 mcp_server.py / elk.py 共用，仅标准库。

安全约束：
- 绝不改动用户工作区：只执行 git fetch（更新 origin/* 远程跟踪引用），代码检出在缓存目录的独立 worktree（--detach）。
- 读取某个 commit 的源码一律用 git show / ls-tree，不依赖 worktree 当前检出状态。
"""
import difflib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

import elk_core as core
import gitlab_auth

# 环境名 → 候选分支（逗号分隔，按优先级；多个同时存在时优先 origin/HEAD，其次最近提交）
DEFAULT_BRANCHES = {"dev": "dev", "test": "beta", "beta": "beta",
                    "stag": "staging", "staging": "staging", "prod": "master,main"}
CONFIG_FILE = re.compile(r"(?:^|/)src/main/resources/(bootstrap|application)([\w-]*)\.(ya?ml|properties)$")
SOURCE_EXT = (".java", ".kt", ".groovy", ".scala")
# 这些包的帧不参与跨仓库查找（JDK / 常见框架）
LIB_PREFIX = ("java.", "javax.", "jdk.", "sun.", "com.sun.", "kotlin.", "scala.", "groovy.",
              "org.springframework.", "org.apache.", "org.mybatis.", "com.baomidou.", "io.netty.",
              "com.alibaba.", "com.fasterxml.", "com.google.", "io.undertow.", "org.hibernate.",
              "feign.", "reactor.", "io.grpc.", "okhttp3.", "ch.qos.", "lombok.", "net.sf.",
              "org.aspectj.", "io.micrometer.", "com.zaxxer.", "org.redisson.", "io.lettuce.",
              "com.mysql.", "org.postgresql.", "org.elasticsearch.", "io.seata.", "com.xxl.")

# k8s Deployment pod：<deployment>-<rs 哈希>-<pod 后缀>；StatefulSet pod：<name>-<序号>
POD_RS = re.compile(r"^(?P<dep>.+?)-(?P<rs>[a-z0-9]{6,10})-(?P<pod>[a-z0-9]{5})$")
POD_STS = re.compile(r"^(?P<dep>.+)-\d+$")

FRAME = re.compile(r"^\s*at\s+(?:[\w.$@-]*/)*(?P<cls>[\w$]+(?:\.[\w$]+)+)\.(?P<method>[\w$<>-]+)"
                   r"\((?P<file>[^():]+?)(?::(?P<line>\d+))?\)")
HEADER = re.compile(r"^\s*(?:(?P<kind>Caused by|Suppressed):\s*)?"
                    r"(?P<exc>(?:[A-Za-z_$][\w$]*\.)+[\w$]*(?:Exception|Error|Throwable|Fault)[\w$]*)"
                    r"(?::\s*(?P<msg>.*))?$")


# ---------------------------------------------------------------- config

def load_code_config(environ=None):
    """ELK_CODE_ROOT 代码根目录（多个用逗号或冒号分隔；每项可以是仓库的父目录，也可以直接是仓库）；ELK_CODE_BRANCHES 环境→分支(JSON)；ELK_CODE_SERVICE_MAP 服务→仓库[/模块](JSON)；
    ELK_CODE_CACHE 缓存目录；ELK_CODE_FETCH_TTL fetch 节流秒数；ELK_CODE_LEASE worktree 使用中不推进的秒数；
    ELK_CODE_GC_DAYS / ELK_CODE_GC_EXACT_DAYS 分支/按 commit 的 worktree 闲置多少天后清理（0 关闭）；ELK_CODE_AUTO_INDEX 是否自动建索引；
    ELK_CODE_GITNEXUS gitnexus 可执行文件。"""
    environ = os.environ if environ is None else environ
    # 分隔符：逗号、换行、系统路径分隔符（Windows 为 ; 以免拆开 C:\ 盘符）
    roots = [os.path.expanduser(r.strip())
             for r in re.split(r"[,\n%s]" % re.escape(os.pathsep), environ.get("ELK_CODE_ROOT") or "")
             if r.strip()]
    # 仓库同步目录（repo_sync.py）总是参与扫描；未单独指定时同步到第一个代码目录
    sync_dir = os.path.expanduser(environ.get("ELK_REPO_SYNC_DIR") or "") or (roots[0] if roots else "")
    if sync_dir and sync_dir not in roots:
        roots.append(sync_dir)
    legacy = core._json(environ.get("ELK_CODE_BRANCHES"), "ELK_CODE_BRANCHES")
    branches = dict(DEFAULT_BRANCHES)
    branches.update(legacy)
    return {
        "roots": roots,
        "sync_dir": sync_dir,
        "branches": branches,
        "branches_legacy": legacy,
        "cache": os.path.expanduser(environ.get("ELK_CODE_CACHE") or "~/.cache/elk-log-analysis"),
        "service_map": core._json(environ.get("ELK_CODE_SERVICE_MAP"), "ELK_CODE_SERVICE_MAP"),
        "fetch_ttl": int(environ.get("ELK_CODE_FETCH_TTL") or 600),
        "lease": int(environ.get("ELK_CODE_LEASE") or 1800),
        "gc_days": float(environ.get("ELK_CODE_GC_DAYS") or 10),
        "gc_exact_days": float(environ.get("ELK_CODE_GC_EXACT_DAYS") or 3),
        "auto_index": core._bool(environ.get("ELK_CODE_AUTO_INDEX"), True),
        "gitnexus": (environ.get("ELK_CODE_GITNEXUS") or shutil.which("gitnexus")
                     or "/opt/homebrew/bin/gitnexus"),
    }


def _require_root(cfg):
    if not cfg["roots"]:
        raise core.ElkError("未配置代码目录：请在 cc-switch 的 elk MCP env 中设置 ELK_CODE_ROOT"
                            "（如 ~/code，多个用逗号分隔）")
    missing = [r for r in cfg["roots"] if not os.path.isdir(r)]
    if len(missing) == len(cfg["roots"]):
        raise core.ElkError("ELK_CODE_ROOT 中的目录都不存在: %s" % ", ".join(missing))


def _roots_text(cfg):
    return ", ".join(cfg["roots"]) or "(未配置)"


def _cache_dir(cfg, *parts):
    path = os.path.join(cfg["cache"], *parts)
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------- helpers

_MEMO = {}


def _memo(key, ttl, fn):
    hit = _MEMO.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _MEMO[key] = (time.time(), val)
    return val


def git(cwd, *args, timeout=60, check=True):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=10")
    env.update(gitlab_auth.git_env(env))  # 配置了 GitLab 同步时注入凭据（仅发往 ELK_GITLAB_URL）
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0",
           "-c", "maintenance.auto=false"] + list(args)
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise core.ElkError("git %s 超时（%ss）: %s" % (args[0], timeout, cwd))
    if not check:
        return r
    if r.returncode != 0:
        raise core.ElkError("git %s 失败（%s）: %s" % (" ".join(args[:3]), cwd,
                                                    (r.stderr or r.stdout).strip()[:500]))
    return r.stdout


class _Lock:
    """跨进程文件锁：多个会话同时操作同一仓库/worktree 时串行化。"""

    def __init__(self, cfg, name):
        self.path = os.path.join(_cache_dir(cfg, "locks"), re.sub(r"[^\w@.-]", "_", name) + ".lock")

    def __enter__(self):
        self.fd = open(self.path, "w")
        if fcntl:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        else:
            while True:  # msvcrt.LK_LOCK 只重试 10 次，这里无限等待以对齐 flock 语义
                try:
                    msvcrt.locking(self.fd.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.2)
        return self

    def __exit__(self, *exc):
        if fcntl:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        else:
            self.fd.seek(0)
            msvcrt.locking(self.fd.fileno(), msvcrt.LK_UNLCK, 1)
        self.fd.close()


def int_arg(a, key, default):
    """整数参数：缺省用 default；无法解析时报参数错误（而不是内部错误）。"""
    v = a.get(key)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        raise core.ElkError("参数 %s 必须是整数，收到: %r" % (key, v))


def _fmt_epoch(sec, tz):
    return datetime.fromtimestamp(sec, timezone.utc).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- service catalog

def _is_repo(path):
    return os.path.exists(os.path.join(path, ".git"))


def repo_map(cfg):
    """仓库名 → 路径。遍历所有代码根目录（按配置顺序，同一真实路径只取一次）；
    不同根目录下有同名仓库时，重名的仓库改名为 <上级目录名>_<仓库名>，保证 worktree/GitNexus 名唯一。"""
    def build():
        _require_root(cfg)
        paths, seen = [], set()
        for root in cfg["roots"]:
            if not os.path.isdir(root):
                continue
            cands = [root] if _is_repo(root) else [os.path.join(root, n) for n in sorted(os.listdir(root))]
            for p in cands:
                real = os.path.realpath(p)
                if real not in seen and _is_repo(p):
                    seen.add(real)
                    paths.append(p)
        counts = {}
        for p in paths:
            counts[os.path.basename(p)] = counts.get(os.path.basename(p), 0) + 1
        out = {}
        for p in paths:
            base = os.path.basename(p)
            name = base if counts[base] == 1 else "%s_%s" % (os.path.basename(os.path.dirname(p)), base)
            out[name] = p
        return out
    return _memo(("repos", tuple(cfg["roots"])), 60, build)


def list_repos(cfg):
    return list(repo_map(cfg).values())


def repo_name(cfg, path):
    for name, p in repo_map(cfg).items():
        if p == path:
            return name
    return os.path.basename(path)


def _clean_value(v):
    v = v.split(" #", 1)[0].strip().strip("'\"").strip()
    m = re.match(r"^\$\{[\w.-]+:([^}]*)\}$", v)  # ${APP_NAME:foo} 取默认值
    if m:
        v = m.group(1).strip()
    if not v or "${" in v or "@" in v:
        return None
    return v


def parse_app_name(text, is_props):
    """从 bootstrap/application 配置中取 spring.application.name（不依赖 PyYAML 的简易解析）。"""
    if is_props:
        m = re.search(r"^\s*spring\.application\.name\s*[=:]\s*(.+)$", text, re.M)
        return _clean_value(m.group(1)) if m else None
    stack = []
    for raw in text.splitlines():
        if raw.startswith("---"):
            stack = []
            continue
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = re.match(r"^(\s*)([\w.-]+)\s*:(?:\s+(.*))?$", raw.rstrip())
        if not m:
            continue
        indent, key, value = len(m.group(1)), m.group(2), (m.group(3) or "").strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([k for _, k in stack] + [key])
        if value and not value.startswith("#"):
            if path == "spring.application.name":
                return _clean_value(value)
        else:
            stack.append((indent, key))
    return None


def _scan_repo(repo_key, repo_path):
    out = git(repo_path, "ls-files", "-z", "--", "*src/main/resources/bootstrap*",
              "*src/main/resources/application*", timeout=30)
    modules = {}
    for rel in filter(None, out.split("\0")):
        m = CONFIG_FILE.search(rel)
        if not m:
            continue
        module = rel[:m.start()].strip("/") or "."
        # bootstrap.yml > application.yml > 带 profile 的文件
        rank = (bool(m.group(2)), m.group(1) != "bootstrap")
        try:
            with open(os.path.join(repo_path, rel), encoding="utf-8", errors="replace") as f:
                name = parse_app_name(f.read(), m.group(3) == "properties")
        except OSError:
            continue
        cur = modules.get(module)
        if name and (cur is None or cur[1] is None or rank < cur[0]):
            modules[module] = (rank, name, rel)
        elif cur is None:
            modules[module] = (rank, None, rel)
    return [{"repo": repo_key, "repo_path": repo_path, "module": mod,
             "app": v[1], "source": v[2]} for mod, v in sorted(modules.items())]


def catalog(cfg):
    """代码根目录下所有仓库的「应用名 ↔ 仓库/模块」清单（进程内缓存 10 分钟）。"""
    def build():
        entries = []
        for name, repo in repo_map(cfg).items():
            try:
                entries.extend(_scan_repo(name, repo))
            except core.ElkError:
                continue
        return entries
    return _memo(("catalog", tuple(cfg["roots"])), 600, build)


def parse_host(host):
    """pod 名 → (deployment, replicaset 前缀)。非 k8s 主机名原样返回。"""
    h = host.strip()
    m = POD_RS.match(h)
    if m:
        return m.group("dep"), "%s-%s" % (m.group("dep"), m.group("rs"))
    m = POD_STS.match(h)
    if m:
        return m.group("dep"), None
    return h, None


def _find_repo(cfg, name):
    """按仓库名（重名时为 <上级目录名>_<仓库名>）或绝对路径查找；返回 (名称, 路径)。"""
    repos = repo_map(cfg)
    if name in repos:
        return name, repos[name]
    path = os.path.realpath(os.path.expanduser(name))
    for n, p in repos.items():
        if os.path.realpath(p) == path:
            return n, p
    dup = [n for n, p in repos.items() if os.path.basename(p) == name]
    if dup:
        raise core.ElkError("仓库 %s 在多个代码目录中重名，请使用: %s" % (name, ", ".join(dup)))
    raise core.ElkError("代码目录 %s 下没有仓库 %s" % (_roots_text(cfg), name))


def resolve_service(cfg, host=None, service=None, repo=None, module=None):
    """host.name / 应用名 / 仓库名 → 仓库与模块。一个仓库可能包含多个服务模块，按模块精确到服务。"""
    info = {"host": host, "deployment": None, "rs": None, "matches": [], "how": None}
    key = service
    if host:
        dep, rs = parse_host(host)
        info.update(deployment=dep, rs=rs)
        key = key or dep
    if repo:
        repo, path = _find_repo(cfg, repo)
        info.update(key=key, how="指定仓库",
                    matches=[e for e in catalog(cfg) if e["repo"] == repo
                             and (not module or e["module"] == module)]
                    or [{"repo": repo, "repo_path": path, "module": module or ".", "app": key}])
        return _primary(info)
    if not key:
        raise core.ElkError("需要 host、service 或 repo 之一")
    info["key"] = key
    manual = cfg["service_map"].get(key)
    if manual:
        r, _, mod = manual.partition("/")
        r, path = _find_repo(cfg, r)
        info.update(how="手动映射 ELK_CODE_SERVICE_MAP",
                    matches=[{"repo": r, "repo_path": path, "module": mod or ".", "app": key}])
        return _primary(info)
    cat = catalog(cfg)
    rules = (
        ("应用名精确匹配", lambda e: e["app"] == key),
        ("模块目录名精确匹配", lambda e: os.path.basename(e["module"]) == key),
    )
    for how, pred in rules:
        hits = [e for e in cat if pred(e)]
        if hits:
            info.update(how=how, matches=hits)
            return _primary(info)
    # 前缀：order-gateway-hw → order-gateway（同一应用的变体部署）
    pref = [e for e in cat if e["app"] and key.startswith(e["app"] + "-")]
    if pref:
        best = max(len(e["app"]) for e in pref)
        info.update(how="应用名前缀匹配（%s 视为 %s 的变体部署）" % (key, pref[0]["app"]),
                    matches=[e for e in pref if len(e["app"]) == best])
        return _primary(info)
    # 后缀：crm-manager → acme-crm-manager（应用名多了公司/产品前缀）；仅对多段名生效，避免 auth 误配 saas-auth
    if "-" in key:
        suf = [e for e in cat if e["app"] and e["app"].endswith("-" + key)]
        if suf:
            info.update(how="应用名后缀匹配（%s → %s）" % (key, suf[0]["app"]), matches=suf)
            return _primary(info)
    names = sorted({e["app"] for e in cat if e["app"]} | {os.path.basename(e["module"]) for e in cat})
    close = difflib.get_close_matches(key, names, n=6, cutoff=0.5)
    raise core.ElkError(
        "未能把 %s 映射到 %s 下的代码。%s\n可在 cc-switch 的 elk MCP env 中配置 "
        "ELK_CODE_SERVICE_MAP={\"%s\":\"<仓库>/<模块>\"}，或调用时直接传 repo/module。" % (
            key, _roots_text(cfg), ("相近的应用/模块: " + ", ".join(close)) if close else "代码目录中没有相近的应用名。",
            key))


def _primary(info):
    m = info["matches"][0]
    info.update(repo=m["repo"], repo_path=m["repo_path"], module=m["module"], app=m.get("app"))
    return info


# ---------------------------------------------------------------- git: fetch / branch / commit

def fetch(cfg, repo_path, force=False):
    """节流 fetch：同一仓库 ELK_CODE_FETCH_TTL 秒内只 fetch 一次。失败不致命，沿用本地远程引用。"""
    name = repo_name(cfg, repo_path)
    stamp = os.path.join(_cache_dir(cfg, "fetch"), name + ".stamp")
    with _Lock(cfg, "repo-" + name):
        if not force and os.path.exists(stamp) and time.time() - os.path.getmtime(stamp) < cfg["fetch_ttl"]:
            return "已在 %d 秒内 fetch 过，跳过" % int(time.time() - os.path.getmtime(stamp))
        r = git(repo_path, "fetch", "--no-tags", "origin", timeout=120, check=False)
        if r.returncode != 0:
            return "fetch 失败，使用本地已有的远程引用：%s" % (r.stderr or "").strip()[:200]
        with open(stamp, "w"):
            pass
        return "已 fetch origin"


def _ref_exists(repo_path, ref):
    return git(repo_path, "rev-parse", "--verify", "-q", ref, check=False).returncode == 0


def scoped(cfg, env):
    """按环境分层后的代码配置：服务映射 = 全局 ELK_CODE_SERVICE_MAP + 环境 ELK_<ENV>_SERVICE_MAP（环境优先）；
    自动建索引 = ELK_<ENV>_AUTO_INDEX > ELK_CODE_AUTO_INDEX。"""
    if not env:
        return cfg
    out = dict(cfg)
    out["service_map"] = dict(cfg["service_map"], **(env.get("service_map") or {}))
    if env.get("auto_index") is not None:
        out["auto_index"] = env["auto_index"]
    return out


def branch_spec(cfg, env):
    """环境 → 候选分支：ELK_<ENV>_BRANCH > ELK_CODE_BRANCHES（旧写法）> 环境级别预设 > 按环境名的内置默认。"""
    name = env["_name"]
    if env.get("branch") and env.get("_src", {}).get("branch") == "环境":
        return env["branch"], "ELK_%s_BRANCH" % core.env_prefix(name)[4:-1]
    if name in cfg.get("branches_legacy", {}):
        return cfg["branches_legacy"][name], "ELK_CODE_BRANCHES"
    if env.get("branch"):
        return env["branch"], "级别预设（%s）" % env.get("tier")
    if name in DEFAULT_BRANCHES:
        return DEFAULT_BRANCHES[name], "内置默认"
    return None, None


def env_branch(cfg, repo_path, env):
    env_name = env["_name"]
    spec, _ = branch_spec(cfg, env)
    if not spec:
        raise core.ElkError("环境 %s 未配置代码分支，也无法按名称推断级别：请在 cc-switch 中设置 %sBRANCH=<分支>"
                            "（或 %sTIER=dev/test/staging/prod）" % (env_name, core.env_prefix(env_name),
                                                                   core.env_prefix(env_name)))
    cands = [b.strip() for b in spec.split(",") if b.strip()]
    exist = [b for b in cands if _ref_exists(repo_path, "refs/remotes/origin/" + b)]
    if not exist:
        raise core.ElkError("仓库 %s 没有环境 %s 对应的远程分支（候选: %s）"
                            % (repo_path, env_name, ", ".join(cands)))
    if len(exist) == 1:
        return exist[0]
    r = git(repo_path, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", check=False)
    head = r.stdout.strip().replace("origin/", "", 1) if r.returncode == 0 else ""
    if head in exist:
        return head
    return max(exist, key=lambda b: int(git(repo_path, "log", "-1", "--format=%ct", "origin/" + b).strip()))


def commit_info(repo_path, rev):
    out = git(repo_path, "log", "-1", "--format=%H%x00%ct%x00%an%x00%s", rev).strip()
    sha, ct, author, subject = out.split("\0", 3)
    return {"sha": sha, "time": int(ct), "author": author, "subject": subject}


# ---------------------------------------------------------------- 部署对齐

def deploy_time(env, rs_prefix, lookback_days=90):
    """ReplicaSet 下所有 pod 的最早一条日志时间 ≈ 该版本上线时间（epoch 秒）。
    返回 (epoch, 说明)；早于日志保留期或查不到时 epoch 为 None。"""
    f = env["_fields"]
    ts = f["timestamp"]
    host_kw = f.get("host_keyword") or f["host"] + ".keyword"
    rng = {"range": {ts: {"gte": "now-%dd" % lookback_days}}}
    body = {"size": 0, "query": {"bool": {"filter": [{"prefix": {host_kw: rs_prefix + "-"}}, rng]}},
            "aggs": {"first": {"min": {"field": ts}}}}
    idx = core.resolve_index(env)
    res = core.call(env, "POST", "/%s/_search" % idx, body, interactive=False)
    first = (res.get("aggregations", {}).get("first") or {}).get("value")
    if not first:
        return None, "近 %d 天没有 %s-* 的日志" % (lookback_days, rs_prefix)
    body = {"size": 0, "query": rng, "aggs": {"first": {"min": {"field": ts}}}}
    res = core.call(env, "POST", "/%s/_search" % idx, body, interactive=False)
    oldest = (res.get("aggregations", {}).get("first") or {}).get("value") or 0
    if first - oldest < 2 * 3600 * 1000:
        return None, "该版本上线时间早于日志保留期，无法从日志推断"
    return int(first / 1000), None


def resolve_commit(cfg, env, a, info, tz, allow_elk):
    """选定环境分支，并确定要看的 commit：显式 commit > 部署时间对齐 > 分支最新。"""
    repo_path = info["repo_path"]
    fetch_note = fetch(cfg, repo_path, force=a.get("fetch") is True)
    branch = env_branch(cfg, repo_path, env)
    head = commit_info(repo_path, "origin/" + branch)
    ctx = {"branch": branch, "head": head, "commit": head, "fetch_note": fetch_note, "align": "分支最新提交"}
    if a.get("commit"):
        ctx["commit"] = commit_info(repo_path, a["commit"])
        ctx["align"] = "调用方指定 commit"
    elif (a.get("align") or env.get("align") or "deploy") == "deploy" and info.get("rs"):
        if not allow_elk:
            ctx["align"] = "分支最新提交（生产环境未确认 confirm_production，未查询日志推断部署时间）"
        else:
            try:
                sec, why = deploy_time(env, info["rs"])
            except core.ElkError as e:
                sec, why = None, "查询部署时间失败: %s" % str(e)[:200]
            if sec is None:
                ctx["align"] = "分支最新提交（%s）" % why
            else:
                sha = git(repo_path, "rev-list", "-1", "--first-parent", "--before=@%d" % sec,
                          "origin/" + branch).strip()
                if sha:
                    ctx["commit"] = commit_info(repo_path, sha)
                    ahead = git(repo_path, "rev-list", "--count", "%s..origin/%s" % (sha, branch)).strip()
                    ctx["align"] = ("按部署时间对齐：%s 首条日志 %s → 取此前最后一个提交（近似部署版本；"
                                    "分支 HEAD 比它新 %s 个提交）" % (info["rs"], _fmt_epoch(sec, tz), ahead))
                else:
                    ctx["align"] = "分支最新提交（部署时间 %s 早于分支上所有提交）" % _fmt_epoch(sec, tz)
    return ctx


# ---------------------------------------------------------------- worktree & gitnexus

def worktree_target(ctx, exact=False):
    """worktree/索引要检出的 commit 及名称后缀。
    默认跟随分支 HEAD：同一分支只维护一份索引，仅在分支有新提交时增量更新，不因不同服务的部署版本来回切换。
    exact=True 且部署版本 ≠ HEAD 时，按 commit 单独建 worktree，调用链与部署版本严格一致。"""
    c, h = ctx["commit"]["sha"], ctx["head"]["sha"]
    if exact and c != h:
        return c, c[:10]
    return h, ctx["branch"]


def worktree_name(info, ctx, exact=False):
    return "%s@%s" % (info["repo"], worktree_target(ctx, exact)[1])


def _index_file(cfg, name):
    return os.path.join(_cache_dir(cfg, "index"), name + ".json")


def _alive(pid):
    if os.name == "nt":  # Windows 上 os.kill(pid, 0) 会直接结束进程；原生 Windows 不支持后台索引，见 start_index
        return False
    try:
        os.kill(pid, 0)
    except (OSError, TypeError):
        return False
    r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return "gitnexus" in r.stdout


def index_state(cfg, wt, name):
    st = {"state": "missing", "indexed_commit": None, "log": None}
    job = None
    if os.path.exists(_index_file(cfg, name)):
        with open(_index_file(cfg, name), encoding="utf-8") as f:
            job = json.load(f)
        st["log"] = job.get("log")
    if job and _alive(job.get("pid")):
        st.update(state="building", target=job.get("commit"), started=job.get("started"))
        return st
    meta = os.path.join(wt, ".gitnexus", "meta.json")
    head = git(wt, "rev-parse", "HEAD").strip() if os.path.exists(os.path.join(wt, ".git")) else None
    if os.path.exists(meta):
        with open(meta, encoding="utf-8") as f:
            st["indexed_commit"] = json.load(f).get("lastCommit")
        st["state"] = "fresh" if st["indexed_commit"] == head else "stale"
    elif job and not _log_succeeded(job.get("log")):
        st["state"] = "failed"  # 构建成功过但索引目录丢失（如 worktree 被重建）则视为 missing
    return st


def _log_succeeded(log):
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            f.seek(max(0, os.path.getsize(log) - 4096))
            return "indexed successfully" in f.read()
    except (OSError, TypeError):
        return False


def _gitnexus_env(cfg):
    """gitnexus 是 node 脚本：MCP 进程的 PATH 可能不含 node，补上 gitnexus 所在目录与 Homebrew 目录。"""
    gn = cfg["gitnexus"]
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([os.path.dirname(os.path.realpath(gn)), os.path.dirname(gn),
                                   "/opt/homebrew/bin", "/usr/local/bin", env.get("PATH", "")])
    return env


def start_index(cfg, wt, name, commit):
    gn = cfg["gitnexus"]
    if os.name == "nt":
        raise core.ElkError("原生 Windows 部署不支持后台构建 GitNexus 索引，请改用 Docker 或 WSL 部署")
    if not os.path.exists(gn):
        raise core.ElkError("未找到 gitnexus（%s），可在 ELK_CODE_GITNEXUS 中指定路径" % gn)
    log = os.path.join(_cache_dir(cfg, "index"), name + ".log")
    env = _gitnexus_env(cfg)
    # 经 sh 后台启动：进程脱离 MCP 进程组，不留僵尸，MCP 重启也不会中断索引
    script = 'log="$1"; shift; nohup "$@" >"$log" 2>&1 </dev/null & echo $!'
    cmd = [gn, "analyze", "--index-only", "--name", name, wt]
    r = subprocess.run(["/bin/sh", "-c", script, "sh", log] + cmd, cwd=wt, env=env,
                       capture_output=True, text=True, timeout=15, start_new_session=True)
    pid = int(r.stdout.strip().splitlines()[-1])
    with open(_index_file(cfg, name), "w", encoding="utf-8") as f:
        json.dump({"pid": pid, "commit": commit, "log": log,
                   "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, f)
    return pid


def _lease_file(cfg, name):
    return os.path.join(_cache_dir(cfg, "lease"), name + ".stamp")


def _lease_age(cfg, name):
    """距上次使用该 worktree 的秒数；从未使用返回 None。"""
    try:
        return time.time() - os.path.getmtime(_lease_file(cfg, name))
    except OSError:
        return None


def _touch_lease(cfg, name):
    with open(_lease_file(cfg, name), "a"):
        pass
    os.utime(_lease_file(cfg, name), None)


def ensure_worktree(cfg, info, ctx, want_index=True, exact=False, refresh=False):
    """独立只读 worktree（detached）检出到分支 HEAD（exact 时为部署 commit），并按需触发 GitNexus 增量索引。

    使用中不推进：worktree 在 ELK_CODE_LEASE 秒内被 code_prepare 用过时，即使分支有新提交也保持原 commit，
    避免同一分支上正在进行的分析（可能在其他会话）读到的文件/调用链中途变化；refresh=True 强制推进。
    返回 (worktree 路径, 名称, 索引状态, 说明, worktree 实际 commit)。"""
    name = worktree_name(info, ctx, exact)
    wt = os.path.join(_cache_dir(cfg, "worktrees"), name)
    sha = worktree_target(ctx, exact)[0]
    notes = []
    with _Lock(cfg, "wt-" + name):
        st = index_state(cfg, wt, name) if os.path.exists(wt) else {"state": "missing"}
        if not os.path.exists(os.path.join(wt, ".git")):
            if os.path.exists(wt):  # 残留目录：改名保留，不删除
                os.rename(wt, "%s.stale-%d" % (wt, int(time.time())))
            # --force 只覆盖「本缓存路径已登记但目录已丢失」的情况；不做全局 prune，避免动用户其他 worktree 登记
            git(info["repo_path"], "worktree", "add", "--force", "--detach", wt, sha, timeout=300)
            notes.append("已创建 worktree")
        else:
            cur = git(wt, "rev-parse", "HEAD").strip()
            age = _lease_age(cfg, name)
            if cur != sha:
                if st["state"] == "building":
                    notes.append("索引构建中，worktree 保持在 %s，未切换到 %s；构建完成后再次调用即可更新"
                                 % (cur[:9], sha[:9]))
                elif not refresh and age is not None and age < cfg["lease"]:
                    notes.append("worktree %d 分钟前刚被使用（可能有分析进行中），保持在 %s 未推进；"
                                 "空闲超过 %d 分钟后自动推进，或传 refresh=true 立即推进"
                                 % (int(age // 60), cur[:9], cfg["lease"] // 60))
                else:
                    git(wt, "checkout", "--detach", "--force", sha, timeout=300)
                    notes.append("worktree 已从 %s 推进到 %s" % (cur[:9], sha[:9]))
        wt_sha = git(wt, "rev-parse", "HEAD").strip()
        st = index_state(cfg, wt, name)
        if want_index and cfg["auto_index"] and st["state"] in ("missing", "stale", "failed"):
            pid = start_index(cfg, wt, name, wt_sha)
            notes.append("已在后台%s GitNexus 索引（pid %d）" % (
                "增量更新" if st["state"] == "stale" else "构建", pid))
            st = index_state(cfg, wt, name)
        _touch_lease(cfg, name)
    return wt, name, st, notes, wt_sha


# ---------------------------------------------------------------- 闲置清理

EXACT_SUFFIX = re.compile(r"@[0-9a-f]{10}$")


def _last_used(cfg, wt, name):
    """最近一次使用/更新时间：code_prepare 使用记录、索引任务启动、索引完成、worktree 创建，取最新。
    直接调用 GitNexus MCP 查询不经过本进程，无法记录（按 SKILL 流程查询前都会先 code_prepare）。"""
    cands = [_lease_file(cfg, name), _index_file(cfg, name),
             os.path.join(wt, ".gitnexus", "meta.json"), os.path.join(wt, ".git"), wt]
    times = []
    for p in cands:
        try:
            times.append(os.path.getmtime(p))
        except OSError:
            pass
    return max(times) if times else 0


def _gc_log(cfg, msg):
    with open(os.path.join(_cache_dir(cfg), "gc.log"), "a", encoding="utf-8") as f:
        f.write("%s %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


def _remove_worktree(cfg, wt, name):
    """删除一个缓存 worktree：先 gitnexus remove（删索引并从注册表注销），再 git worktree remove（注销主仓库登记）。
    只处理缓存目录下的路径。"""
    root = os.path.realpath(_cache_dir(cfg, "worktrees"))
    if os.path.dirname(os.path.realpath(wt)) != root:
        raise core.ElkError("拒绝删除缓存目录之外的路径: %s" % wt)
    steps = []
    if os.path.exists(os.path.join(wt, ".gitnexus")) and os.path.exists(cfg["gitnexus"]):
        r = subprocess.run([cfg["gitnexus"], "remove", "-f", wt], capture_output=True, text=True,
                           timeout=120, env=_gitnexus_env(cfg))
        steps.append("gitnexus remove=%s" % ("ok" if r.returncode == 0 else "失败:" + (r.stderr or r.stdout).strip()[:120]))
    common = None
    if os.path.exists(os.path.join(wt, ".git")):
        r = git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
        common = r.stdout.strip() if r.returncode == 0 else None
    if common and os.path.isdir(common):
        main_repo = os.path.dirname(common) if os.path.basename(common) == ".git" else common
        r = git(main_repo, "worktree", "remove", "--force", wt, timeout=120, check=False)
        steps.append("worktree remove=%s" % ("ok" if r.returncode == 0 else "失败:" + (r.stderr or "").strip()[:120]))
    if os.path.exists(wt):  # 主仓库已不存在或 worktree remove 失败：缓存目录内的残留直接删除
        shutil.rmtree(wt, ignore_errors=True)
        steps.append("rmtree")
    # 运行时文件按名称归属：只有删除的正是该名称的正式 worktree 时才清理（残留目录 xxx.stale-* 与正式 worktree 同名）
    if os.path.basename(wt) == name:
        for p in (_lease_file(cfg, name), _index_file(cfg, name),
                  os.path.join(_cache_dir(cfg, "index"), name + ".log")):
            if os.path.exists(p):
                os.remove(p)
    return steps


def remove_stale_dir(cfg, path):
    """删除缓存目录内的残留目录（xxx.stale-* 或没有 .git 的目录），不触碰同名正式 worktree 的运行时文件。"""
    root = os.path.realpath(_cache_dir(cfg, "worktrees"))
    if os.path.dirname(os.path.realpath(path)) != root:
        raise core.ElkError("拒绝删除缓存目录之外的路径: %s" % path)
    shutil.rmtree(path, ignore_errors=True)


def op_gc(cfg, dry_run=False, days=None, exact_days=None):
    """清理闲置的缓存 worktree 与其 GitNexus 索引：分支 worktree 默认 10 天、按 commit 的 worktree 默认 3 天未使用即删除。
    跳过：索引构建中、使用中（ELK_CODE_LEASE 内）；只处理缓存目录，不碰用户仓库自己的 .gitnexus。"""
    days = cfg["gc_days"] if days is None else days
    exact_days = cfg["gc_exact_days"] if exact_days is None else exact_days
    base = _cache_dir(cfg, "worktrees")
    now = time.time()
    lines, freed = [], 0
    for entry in sorted(os.listdir(base)):
        wt = os.path.join(base, entry)
        if not os.path.isdir(wt):
            continue
        stale_dir = ".stale-" in entry
        name = entry.split(".stale-")[0]
        limit = exact_days if EXACT_SUFFIX.search(name) else days
        if limit <= 0:
            continue
        idle = (now - _last_used(cfg, wt, name)) / 86400
        if idle < limit:
            lines.append("保留 %-45s 闲置 %.1f 天（阈值 %g 天）" % (entry, idle, limit))
            continue
        if not stale_dir:
            st = index_state(cfg, wt, name)
            if st["state"] == "building":
                lines.append("跳过 %-45s 索引构建中" % entry)
                continue
        size = _du(wt)
        if dry_run:
            lines.append("将删除 %-43s 闲置 %.1f 天，约 %s" % (entry, idle, _fmt_size(size)))
            freed += size
            continue
        with _Lock(cfg, "wt-" + name):
            if not stale_dir and (now - _last_used(cfg, wt, name)) / 86400 < limit:  # 加锁后复核，防止刚被使用
                lines.append("保留 %-45s 加锁复核时发现刚被使用" % entry)
                continue
            steps = _remove_worktree(cfg, wt, name) if not stale_dir else (remove_stale_dir(cfg, wt) or ["rmtree"])
        freed += size
        msg = "已删除 %s 闲置 %.1f 天，释放约 %s（%s）" % (entry, idle, _fmt_size(size), ", ".join(steps))
        _gc_log(cfg, msg)
        lines.append(msg)
    head = "# 闲置清理%s：分支 worktree %g 天 / 按 commit worktree %g 天；%s约 %s" % (
        "预览" if dry_run else "", days, exact_days, "可释放" if dry_run else "已释放", _fmt_size(freed))
    return "\n".join([head] + (lines or ["（没有缓存 worktree）"]))


def _du(path):
    total = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def _fmt_size(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return "%.0f%s" % (n, unit) if unit in ("B", "K") else "%.1f%s" % (n, unit)
        n /= 1024.0


def maybe_gc_background(cfg):
    """MCP 调用 code_* 时顺带触发：每天最多一次，后台线程执行，不阻塞当前请求；出错只记日志。"""
    if cfg["gc_days"] <= 0 and cfg["gc_exact_days"] <= 0:
        return
    stamp = os.path.join(_cache_dir(cfg), "gc.stamp")
    try:
        if time.time() - os.path.getmtime(stamp) < 86400:
            return
    except OSError:
        pass
    with open(stamp, "a"):
        pass
    os.utime(stamp, None)

    def run():
        try:
            op_gc(cfg)
        except Exception as e:  # noqa: BLE001
            _gc_log(cfg, "清理失败: %s" % e)
    threading.Thread(target=run, name="elk-code-gc", daemon=True).start()


# ---------------------------------------------------------------- 源码读取与堆栈定位

def tree_index(repo_path, rev):
    """某 commit 下源码文件：文件名 → [路径]（按 commit 缓存）。"""
    def build():
        out = git(repo_path, "ls-tree", "-r", "-z", "--name-only", rev, timeout=60)
        idx = {}
        for p in filter(None, out.split("\0")):
            if p.endswith(SOURCE_EXT):
                idx.setdefault(p.rsplit("/", 1)[-1], []).append(p)
        return idx
    return _memo(("tree", repo_path, rev), 3600, build)


def worktree_index(repo_path):
    """用户工作区（当前检出）的源码文件索引，用于推断仓库与定位依赖库帧。"""
    def build():
        out = git(repo_path, "ls-files", "-z", timeout=60)
        idx = {}
        for p in filter(None, out.split("\0")):
            if p.endswith(SOURCE_EXT):
                idx.setdefault(p.rsplit("/", 1)[-1], []).append(p)
        return idx
    return _memo(("wtidx", repo_path), 600, build)


def parse_stack(text):
    sections, cur = [], None
    for line in text.splitlines():
        fm = FRAME.match(line)
        if fm:
            if cur is None:
                cur = {"header": "(无异常头)", "kind": "top", "frames": []}
                sections.append(cur)
            cls = fm.group("cls")
            pkg = cls.rsplit(".", 1)[0]
            cur["frames"].append({"cls": cls, "method": fm.group("method"), "file": fm.group("file"),
                                  "line": int(fm.group("line")) if fm.group("line") else None,
                                  "suffix": pkg.replace(".", "/") + "/" + fm.group("file")})
            continue
        hm = HEADER.match(line.strip())
        if hm:
            cur = {"header": line.strip()[:300], "kind": hm.group("kind") or "top", "frames": []}
            sections.append(cur)
    return [s for s in sections if s["frames"]]


def _match(idx, frame):
    return [p for p in idx.get(frame["file"], []) if p == frame["suffix"] or p.endswith("/" + frame["suffix"])]


def infer_repo(cfg, sections):
    """未给出 host/service/repo 时，按堆栈帧在各仓库中的命中数推断仓库。"""
    score = {}
    for s in sections:
        for fr in s["frames"]:
            if fr["cls"].startswith(LIB_PREFIX):
                continue
            for repo in list_repos(cfg):
                if _match(worktree_index(repo), fr):
                    score[repo] = score.get(repo, 0) + 1
    if not score:
        raise core.ElkError("堆栈中的业务帧在 %s 下的仓库里都找不到，请传 host/service/repo" % _roots_text(cfg))
    ranked = sorted(score.items(), key=lambda kv: -kv[1])
    return ranked


def _infer_module(cfg, repo_path, sections):
    """最靠近异常抛出点的业务帧所在的服务模块（按模块路径最长前缀匹配）。"""
    mods = [e["module"] for e in catalog(cfg) if e["repo_path"] == repo_path and e["module"] != "."]
    idx = worktree_index(repo_path)
    for s in reversed(sections):
        for fr in s["frames"]:
            for p in _match(idx, fr):
                owners = [m for m in mods if p.startswith(m + "/")]
                if owners:
                    return max(owners, key=len)
    return None


def snippet(repo_path, rev, path, line, context):
    r = git(repo_path, "show", "%s:%s" % (rev, path), check=False)
    if r.returncode != 0:
        return ["     (读取失败)"]
    lines = r.stdout.splitlines()
    if not line:
        return []
    if line > len(lines):
        return ["     ⚠ 行号 %d 超出文件长度 %d，运行版本与该 commit 可能不一致" % (line, len(lines))]
    lo, hi = max(1, line - context), min(len(lines), line + context)
    return ["   %s%5d | %s" % (">" if i == line else " ", i, lines[i - 1]) for i in range(lo, hi + 1)]


def locate(cfg, info, ctx, sections, context=3, max_snippets=6):
    repo_path, sha = info["repo_path"], ctx["commit"]["sha"]
    idx = tree_index(repo_path, sha)
    others = [r for r in list_repos(cfg) if r != repo_path]
    # 根因（最后一个 Caused by）优先分配源码片段
    order = list(range(len(sections)))[::-1]
    budget = {}
    left = max_snippets
    for i in order:
        for j, fr in enumerate(sections[i]["frames"]):
            if left and fr["line"] and _match(idx, fr):
                budget[(i, j)] = True
                left -= 1
    out = []
    for i, s in enumerate(sections):
        tag = "  ← 根因" if i == len(sections) - 1 and len(sections) > 1 else ""
        out.append("## %s%s" % (s["header"], tag))
        skipped = 0
        for j, fr in enumerate(s["frames"]):
            if not fr["line"]:  # CGLIB/SkyWalking 增强等无行号帧，没有定位价值
                skipped += 1
                continue
            loc = "%s.%s(%s:%s)" % (fr["cls"], fr["method"], fr["file"], fr["line"])
            paths = _match(idx, fr)
            if paths:
                out.append("  - %s\n    %s:%s" % (loc, paths[0], fr["line"] or ""))
                if len(paths) > 1:
                    out.append("    （同名文件还有: %s）" % ", ".join(paths[1:3]))
                if budget.get((i, j)):
                    out.extend(snippet(repo_path, sha, paths[0], fr["line"], context))
                continue
            dep = None
            if not fr["cls"].startswith(LIB_PREFIX):
                for r in others:
                    hit = _match(worktree_index(r), fr)
                    if hit:
                        dep = (repo_name(cfg, r), hit[0])
                        break
            if dep:
                out.append("  - %s\n    [依赖仓库 %s，工作区当前检出，非环境分支] %s:%s" % (
                    loc, dep[0], dep[1], fr["line"] or ""))
            else:
                skipped += 1
        if skipped:
            out.append("  （省略 %d 个 JDK/框架/外部依赖帧）" % skipped)
    return out


# ---------------------------------------------------------------- 输出

def _head_lines(info, ctx, env, tz):
    c, h = ctx["commit"], ctx["head"]
    lines = ["# 服务定位"]
    if info.get("host"):
        lines.append("host=%s → deployment=%s%s" % (info["host"], info["deployment"],
                                                     " (replicaset %s)" % info["rs"] if info.get("rs") else ""))
    lines.append("应用=%s 仓库=%s 模块=%s（%s）" % (info.get("app") or "-", info["repo"], info["module"], info["how"]))
    for m in info["matches"][1:4]:
        lines.append("  其他候选: 仓库=%s 模块=%s 应用=%s" % (m["repo"], m["module"], m.get("app")))
    lines += ["# 代码版本",
              "env=%s 分支=origin/%s（%s）" % (env["_name"], ctx["branch"], ctx["fetch_note"]),
              "commit=%s %s %s: %s" % (c["sha"][:10], _fmt_epoch(c["time"], tz), c["author"], c["subject"][:80]),
              "对齐方式: %s" % ctx["align"]]
    if c["sha"] != h["sha"]:
        lines.append("分支 HEAD=%s %s: %s" % (h["sha"][:10], _fmt_epoch(h["time"], tz), h["subject"][:80]))
    return lines


def op_services(cfg, a, env=None):
    cfg = scoped(cfg, env)
    if a.get("host") or a.get("service"):
        info = resolve_service(cfg, a.get("host"), a.get("service"))
        lines = ["%s → 仓库=%s 模块=%s 应用=%s（%s）" % (a.get("host") or a.get("service"), m["repo"],
                                                    m["module"], m.get("app"), info["how"])
                 for m in info["matches"]]
        return "\n".join(lines)
    kw = (a.get("grep") or "").lower()
    rows = ["%-36s %-32s %s" % (e.get("app") or "(未声明)", e["repo"], e["module"]) for e in catalog(cfg)
            if not kw or kw in ("%s %s %s" % (e.get("app"), e["repo"], e["module"])).lower()]
    return "# 应用名 / 仓库 / 模块（共 %d）\n%s" % (len(rows), "\n".join(rows))


def _gap(info, a, b):
    """a...b 的对称差：(仅在 a 中的提交数, 仅在 b 中的提交数, 其中改动了本模块的提交数或 None)。"""
    repo = info["repo_path"]
    only_a, only_b = (int(x) for x in git(repo, "rev-list", "--left-right", "--count", "%s...%s" % (a, b)).split())
    mod = None
    if info["module"] != ".":
        mod = int(git(repo, "rev-list", "--count", "%s...%s" % (a, b), "--", info["module"]).strip())
    return only_a, only_b, mod


def _relation(label, a, only_a, only_b):
    if only_a == 0:
        return "比%s %s 新 %d 个提交" % (label, a[:10], only_b)
    if only_b == 0:
        return "比%s %s 旧 %d 个提交" % (label, a[:10], only_a)
    return "与%s %s 分叉（对方独有 %d、本身独有 %d 个提交）" % (label, a[:10], only_a, only_b)


def _version_gap_lines(info, ctx, exact, wt_sha):
    """worktree/索引实际版本与部署版本、分支 HEAD 不一致时的说明（附本模块改动数，便于判断差异是否要紧）。"""
    c, h = ctx["commit"]["sha"], ctx["head"]["sha"]
    mod_name = info["module"]
    lines = []
    if wt_sha != c:
        only_c, only_w, mod = _gap(info, c, wt_sha)
        rel = "worktree/索引 %s %s" % (wt_sha[:10], _relation("部署版本", c, only_c, only_w))
        if mod == 0:
            lines.append("%s，但模块 %s 无改动，模块内调用链可直接使用（跨模块调用仍可能有差异）" % (rel, mod_name))
        elif mod is None:
            lines.append("⚠ %s：调用链/影响面可参考；精确行号以 code_locate 为准；需要严格一致时传 exact=true" % rel)
        else:
            lines.append("⚠ %s，其中 %d 个改动了模块 %s：调用链/影响面仅供参考；精确行号以 code_locate 为准；"
                         "关键结论需严格一致时传 exact=true（git log %s...%s -- %s 可查看差异）"
                         % (rel, mod, mod_name, c[:10], wt_sha[:10], mod_name))
    elif exact and c != h:
        lines.append("worktree/索引基于部署版本 %s（按 commit 单独建立，闲置 ELK_CODE_GC_EXACT_DAYS 天后自动清理）"
                     % c[:10])
    if not exact and wt_sha != h:
        _, only_h, mod = _gap(info, wt_sha, h)
        lines.append("worktree 未推进到分支 HEAD %s（HEAD 比它新 %d 个提交%s）；需要最新代码时传 refresh=true" % (
            h[:10], only_h, "" if mod is None else "，其中 %d 个改动了模块 %s" % (mod, mod_name)))
    return lines


def op_prepare(cfg, env, a, tz, allow_elk):
    cfg = scoped(cfg, env)
    info = resolve_service(cfg, a.get("host"), a.get("service"), a.get("repo"), a.get("module"))
    ctx = resolve_commit(cfg, env, a, info, tz, allow_elk)
    exact = a.get("exact") is True
    wt, name, st, notes, wt_sha = ensure_worktree(cfg, info, ctx, a.get("index", True) is not False, exact,
                                                  a.get("refresh") is True)
    lines = _head_lines(info, ctx, env, tz)
    mod_dir = wt if info["module"] == "." else os.path.join(wt, info["module"])
    lines += ["# 源码（只读 worktree，禁止在其中修改/提交）",
              "worktree=%s @ %s" % (wt, wt_sha[:10]), "模块目录=%s" % mod_dir]
    lines += _version_gap_lines(info, ctx, exact, wt_sha)
    lines += ["  " + n for n in notes]
    desc = {"fresh": "最新，可直接使用", "stale": "过期（索引 %s ≠ worktree HEAD）" % (st.get("indexed_commit") or "")[:9],
            "building": "构建中（目标 %s，开始于 %s），完成前用 Read/Grep 直接读 worktree" % (
                (st.get("target") or "")[:9], st.get("started")),
            "missing": "未建立（ELK_CODE_AUTO_INDEX=false 或 index=false）",
            "failed": "上次构建失败，见日志"}.get(st["state"], st["state"])
    lines += ["# GitNexus", "repo=%s 状态=%s" % (name, desc)]
    if st.get("log"):
        lines.append("日志=%s" % st["log"])
    if st["state"] == "fresh":
        lines.append("gitnexus 工具调用时传 repo=\"%s\"（query/context/impact 等）" % name)
    return "\n".join(lines)


def op_locate(cfg, env, a, tz, allow_elk):
    cfg = scoped(cfg, env)
    sections = parse_stack(a.get("stack") or "")
    if not sections:
        raise core.ElkError("未从 stack 中解析到堆栈帧（需包含 'at pkg.Class.method(File.java:123)' 行）")
    infer_note = None
    if a.get("host") or a.get("service") or a.get("repo"):
        info = resolve_service(cfg, a.get("host"), a.get("service"), a.get("repo"), a.get("module"))
    else:
        ranked = infer_repo(cfg, sections)
        repo = repo_name(cfg, ranked[0][0])
        info = resolve_service(cfg, repo=repo, module=_infer_module(cfg, ranked[0][0], sections))
        info["how"] = "按堆栈帧推断仓库/模块"
        if len(ranked) > 1:
            infer_note = "其他命中仓库: " + ", ".join("%s(%d)" % (repo_name(cfg, r), n) for r, n in ranked[1:4])
    ctx = resolve_commit(cfg, env, a, info, tz, allow_elk)
    lines = _head_lines(info, ctx, env, tz)
    if infer_note:
        lines.append(infer_note)
    lines.append("# 堆栈定位（源码取自 commit %s）" % ctx["commit"]["sha"][:10])
    ctx_lines = int_arg(a, "context", 3)
    budget = int_arg(a, "max_snippets", 6)
    lines += locate(cfg, info, ctx, sections, ctx_lines, budget)
    if a.get("prepare"):
        exact = a.get("exact") is True
        wt, name, st, notes, wt_sha = ensure_worktree(cfg, info, ctx, exact=exact, refresh=a.get("refresh") is True)
        lines += ["# worktree=%s @ %s  gitnexus repo=%s 状态=%s" % (wt, wt_sha[:10], name, st["state"])]
        lines += _version_gap_lines(info, ctx, exact, wt_sha) + ["  " + n for n in notes]
    return "\n".join(lines)
