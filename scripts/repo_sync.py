"""GitLab 仓库同步：列出令牌有权限的全部项目，部分 clone / fetch 到代码目录，可选预建 GitNexus 索引。
供 mcp_http.py（定时）、mcp_server.py（code_repos 工具）、elk.py（repo-sync 命令）共用，仅标准库。

环境变量：
  ELK_GITLAB_URL            GitLab 地址，如 https://gitlab.example.com（支持自建实例）
  认证（访问令牌 / OAuth 令牌 / 账号密码）与拉取协议（HTTPS / SSH）见 gitlab_auth.py
  ELK_GITLAB_GROUPS         只同步这些组（完整路径，逗号分隔，含子组）；不设则同步令牌所属账号是成员的全部项目
  ELK_GITLAB_EXCLUDE        排除的项目，按 path_with_namespace 通配，逗号分隔，如 sandbox/*,*/docs
  ELK_REPO_SYNC_DIR         clone 到哪个目录，默认 ELK_CODE_ROOT 的第一个目录
  ELK_REPO_SYNC_INTERVAL    HTTP 服务内定时同步的间隔秒数，默认 21600（6 小时）；0 关闭定时
  ELK_REPO_SYNC_JOBS        并发 clone/fetch 数，默认 4
  ELK_REPO_SYNC_INDEX       同步后为哪些环境的分支预建 GitNexus 索引，如 prod 或 test,prod；默认不预建
  ELK_REPO_SYNC_INDEX_REPOS 只为这些仓库预建索引（仓库名通配，逗号分隔），默认全部
  ELK_REPO_SYNC_INDEX_TIMEOUT 单个仓库建索引的超时秒数，默认 3600

安全约束：只 clone 令牌可见且未被排除的项目；本脚本 clone 的仓库（登记在清单中）会被强制对齐到远程默认分支，
清单外已有的仓库只 fetch、不改工作区；GitLab 上已无权限/已删除的项目只标记为 stale，不自动删除。
"""
import fnmatch
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import code_core as code
import elk_core as core
import gitlab_auth

MANIFEST = ".elk-repo-sync.json"
_RUN_LOCK = threading.Lock()  # 进程内：定时任务与手动触发不重叠
_TRIGGER = threading.Event()


def _split(v):
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def load_sync_config(environ=None):
    """返回同步配置；未配置 GitLab 时返回 None。"""
    environ = os.environ if environ is None else environ
    auth = gitlab_auth.load(environ)
    if auth is None:
        return None
    ccfg = code.load_code_config(environ)
    return {
        "url": auth["url"],
        "auth": auth,
        "groups": _split(environ.get("ELK_GITLAB_GROUPS")),
        "exclude": _split(environ.get("ELK_GITLAB_EXCLUDE")),
        "dir": ccfg["sync_dir"],
        "interval": int(environ.get("ELK_REPO_SYNC_INTERVAL") or 21600),
        "jobs": max(1, int(environ.get("ELK_REPO_SYNC_JOBS") or 4)),
        "index_envs": _split(environ.get("ELK_REPO_SYNC_INDEX")),
        "index_repos": _split(environ.get("ELK_REPO_SYNC_INDEX_REPOS")),
        "index_timeout": int(environ.get("ELK_REPO_SYNC_INDEX_TIMEOUT") or 3600),
        "code": ccfg,
    }


def _state_dir(scfg):
    return code._cache_dir(scfg["code"], "repo-sync")


def status_path(scfg):
    return os.path.join(_state_dir(scfg), "status.json")


def _log(scfg, msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(os.path.join(_state_dir(scfg), "sync.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    return line


# ---------------------------------------------------------------- GitLab API

def _api(scfg, path, params=None):
    return gitlab_auth.api(scfg["auth"], path, params)


def _paged(scfg, path, params):
    page, out = "1", []
    while page:
        data, page = _api(scfg, path, dict(params, page=page, per_page=100))
        out.extend(data)
    return out


def check_login(scfg):
    """确定并验证 API 凭据（auto 时从 git 凭据管理器检测），返回 GitLab 用户名。"""
    bad = gitlab_auth.problem(scfg["auth"])
    if bad:
        raise core.ElkError(bad)
    return gitlab_auth.resolve_api(scfg["auth"])


def check_all(scfg):
    """repo-sync --check：API 凭据 + SSH、HTTPS 两种拉取方式都实测一遍，报告各自结果与将采用的方式。"""
    auth = scfg["auth"]
    lines = ["GitLab: %s" % scfg["url"]]
    try:
        user = check_login(scfg)
    except core.ElkError as e:
        return "\n".join(lines + ["✘ API：%s" % e])
    lines.append("✔ API：以 %s 登录（%s）" % (user, gitlab_auth.describe(auth).split("；")[0][4:]))
    lines += ["  · " + d for d in auth["detect"]]
    data, _ = _api(scfg, "/groups/%s/projects" % urllib.parse.quote(scfg["groups"][0], safe="")
                   if scfg["groups"] else "/projects",
                   {"membership": "true", "archived": "false", "per_page": 1, "include_subgroups": "true"})
    if not data:
        return "\n".join(lines + ["· 账号下没有可见项目，无法测试拉取"])
    p = data[0]
    lines.append("用项目 %s 测试拉取：" % p.get("path_with_namespace"))
    results = gitlab_auth.probe_transports(auth, p.get("http_url_to_repo"), p.get("ssh_url_to_repo"),
                                           stop_on_success=False) if auth["protocol"] == "auto" else \
        gitlab_auth.probe_transports(auth, p.get("http_url_to_repo"), p.get("ssh_url_to_repo"))
    for proto, ok, why in results:
        lines.append("  %s %s%s" % ("✔" if ok else "✘", proto.upper(), "" if ok else "：%s" % why))
    chosen = next((proto for proto, ok, _ in results if ok), None)
    lines.append("→ 同步时将使用 %s 拉取代码" % chosen.upper() if chosen else "✘ 两种方式都无法拉取代码")
    return "\n".join(lines)


def choose_transport(scfg, projects):
    """确定拉代码的协议（auto：SSH 优先，不通用 HTTPS），并给每个项目填上 clone 地址。
    以前几个项目实测，避免某一个项目的权限特例导致误判。"""
    auth, last = scfg["auth"], None
    for p in projects[:3]:
        try:
            gitlab_auth.resolve_transport(auth, p["http_url"], p["ssh_url"])
            break
        except core.ElkError as e:
            last = e
    else:
        if projects:
            raise last
    for p in projects:
        p["url"] = gitlab_auth.clone_url(auth, p)
    return projects


def list_projects(scfg):
    """令牌可见的项目（排除归档、空仓库、仓库功能关闭的项目，以及 ELK_GITLAB_EXCLUDE 命中的项目）。"""
    base = {"archived": "false", "order_by": "id", "sort": "asc"}
    if scfg["groups"]:
        raw = []
        for g in scfg["groups"]:
            raw.extend(_paged(scfg, "/groups/%s/projects" % urllib.parse.quote(g, safe=""),
                              dict(base, include_subgroups="true", with_shared="false")))
    else:
        raw = _paged(scfg, "/projects", dict(base, membership="true"))
    seen, out = set(), []
    for p in raw:
        pwn = p.get("path_with_namespace", "")
        if p["id"] in seen or p.get("empty_repo") or p.get("archived") \
                or p.get("repository_access_level") == "disabled" \
                or any(fnmatch.fnmatch(pwn, pat) for pat in scfg["exclude"]):
            continue
        seen.add(p["id"])
        out.append({"id": p["id"], "path": p["path"], "path_with_namespace": pwn,
                    "http_url": p.get("http_url_to_repo") or "", "ssh_url": p.get("ssh_url_to_repo") or "",
                    "default_branch": p.get("default_branch") or ""})
    return out


# ---------------------------------------------------------------- 本地目录

def _load_manifest(scfg):
    try:
        with open(os.path.join(scfg["dir"], MANIFEST), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"projects": {}}


def _save_manifest(scfg, m):
    path = os.path.join(scfg["dir"], MANIFEST)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _norm_url(u):
    """http(s)/ssh 两种 remote 写法归一为 host/path，用于判断已有目录是否就是该项目。"""
    u = (u or "").strip().lower()
    if u.startswith("git@"):
        u = u[4:].replace(":", "/", 1)
    else:
        u = urllib.parse.urlsplit(u)
        u = (u.hostname or "") + u.path
    return u[:-4] if u.endswith(".git") else u.rstrip("/")


def _origin(path):
    r = code.git(path, "remote", "get-url", "origin", check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def plan_dirs(scfg, projects, manifest):
    """项目 → 本地目录名。已登记的沿用；同名项目（不同组下 path 相同）改用 组_子组_项目 以免冲突。"""
    known = {int(k): v["dir"] for k, v in manifest["projects"].items()}
    counts = {}
    for p in projects:
        counts[p["path"]] = counts.get(p["path"], 0) + 1
    taken = set(known.values())
    for p in projects:
        if p["id"] in known:
            p["dir"] = known[p["id"]]
            continue
        full = p["path_with_namespace"].replace("/", "_")
        name = p["path"] if counts[p["path"]] == 1 else full
        target = os.path.join(scfg["dir"], name)
        conflict = name in taken
        if not conflict and os.path.exists(target):
            # 已有同名目录但不是这个项目（普通目录或别的仓库）：换用带命名空间的名字；是同一项目则直接接管（只 fetch）
            conflict = not code._is_repo(target) or _norm_url(_origin(target)) != _norm_url(p["url"])
        if conflict:
            name = full
        p["dir"] = name
        taken.add(name)
    return projects


# ---------------------------------------------------------------- clone / fetch

def _git_err(r):
    """git 失败输出里只取 fatal/error/remote 行（去掉进度与本地路径噪音），便于在状态里阅读。"""
    text = (r.stderr or r.stdout or "").strip()
    keep = [ln.strip() for ln in text.splitlines()
            if ln.strip().lower().startswith(("fatal:", "error:", "remote:"))]
    if keep:
        return "；".join(keep)[:300]
    return (text.splitlines()[-1] if text else "未知错误")[:300]


def _sync_one(scfg, p, manifest):
    """返回 (动作, 错误)。动作：cloned / updated / adopted。"""
    path = os.path.join(scfg["dir"], p["dir"])
    managed = str(p["id"]) in manifest["projects"]
    with code._Lock(scfg["code"], "repo-" + p["dir"]):
        if not os.path.exists(path):
            tmp = os.path.join(scfg["dir"], ".%s.cloning" % p["dir"])
            if os.path.exists(tmp):
                shutil.rmtree(tmp, ignore_errors=True)
            # blob:none：只下载提交与目录树，文件内容在检出/查看时按需获取，几十个仓库也只占少量磁盘
            r = code.git(scfg["dir"], "clone", "--filter=blob:none", "--no-tags", p["url"], tmp,
                         timeout=1800, check=False)
            if r.returncode != 0:
                shutil.rmtree(tmp, ignore_errors=True)
                return None, _git_err(r)
            os.rename(tmp, path)
            action, managed = "cloned", True
        elif not code._is_repo(path):
            return None, "目录已存在但不是 git 仓库：%s" % path
        else:
            if managed and _origin(path) != p["url"]:
                code.git(path, "remote", "set-url", "origin", p["url"], check=False)
            r = code.git(path, "fetch", "--prune", "--no-tags", "origin", timeout=900, check=False)
            if r.returncode != 0:
                return None, _git_err(r)
            action = "updated" if managed else "adopted"
        db = p["default_branch"]
        if db and code._ref_exists(path, "refs/remotes/origin/" + db):
            code.git(path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/" + db, check=False)
            if managed:  # 只对本脚本 clone 的仓库强制对齐工作区；用户自己的仓库不动
                code.git(path, "reset", "-q", "--hard", "origin/" + db, timeout=600, check=False)
        # 刷新 code_core 的 fetch 节流时间戳，code_locate 不必马上再 fetch
        with open(os.path.join(code._cache_dir(scfg["code"], "fetch"), p["dir"] + ".stamp"), "w"):
            pass
        if managed:
            manifest["projects"][str(p["id"])] = {"dir": p["dir"], "path": p["path_with_namespace"],
                                                  "url": p["url"]}
        return action, None


# ---------------------------------------------------------------- GitNexus 预建索引

def _run_index(ccfg, wt, name, sha, timeout):
    """同步地建索引（逐个进行，避免几十个仓库同时建索引压垮机器）；登记任务文件，期间 code_prepare 会看到“构建中”。"""
    log = os.path.join(code._cache_dir(ccfg, "index"), name + ".log")
    with open(log, "w", encoding="utf-8") as out:
        proc = subprocess.Popen([ccfg["gitnexus"], "analyze", "--index-only", "--name", name, wt], cwd=wt,
                                env=code._gitnexus_env(ccfg), stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        with open(code._index_file(ccfg, name), "w", encoding="utf-8") as f:
            json.dump({"pid": proc.pid, "commit": sha, "log": log,
                       "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, f)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            return "超时（%ds）" % timeout
    return None if code._log_succeeded(log) else "失败，见 %s" % log


def prebuild_indexes(scfg, repo_dirs, report):
    ccfg = scfg["code"]
    if os.name == "nt":
        report["index_skipped"] = "原生 Windows 不支持建索引"
        return
    if not os.path.exists(ccfg["gitnexus"]):
        report["index_skipped"] = "未找到 gitnexus（%s）" % ccfg["gitnexus"]
        return
    conf = core.load_config()
    envs = []
    for name in scfg["index_envs"]:
        try:
            envs.append(core.get_env(conf, name))
        except core.ElkError as e:
            report["index_failed"].append({"repo": "-", "env": name, "error": str(e)[:200]})
    code._MEMO.clear()
    for d in repo_dirs:
        if scfg["index_repos"] and not any(fnmatch.fnmatch(d, pat) for pat in scfg["index_repos"]):
            continue
        repo_path = os.path.join(scfg["dir"], d)
        for env in envs:
            try:
                branch = code.env_branch(ccfg, repo_path, env)
            except core.ElkError:
                continue  # 该仓库没有这个环境的分支（未部署到该环境），跳过
            try:
                head = code.commit_info(repo_path, "origin/" + branch)
                info = {"repo": code.repo_name(ccfg, repo_path), "repo_path": repo_path}
                ctx = {"branch": branch, "head": head, "commit": head}
                wt, name, st, _, sha = code.ensure_worktree(ccfg, info, ctx, want_index=False)
                if st["state"] == "fresh":
                    report["index_fresh"] += 1
                    continue
                if st["state"] == "building":
                    continue
                err = _run_index(ccfg, wt, name, sha, scfg["index_timeout"])
                if err:
                    report["index_failed"].append({"repo": name, "env": env["_name"], "error": err})
                else:
                    report["indexed"].append(name)
                    _log(scfg, "索引完成 %s" % name)
            except core.ElkError as e:
                report["index_failed"].append({"repo": d, "env": env["_name"], "error": str(e)[:200]})


# ---------------------------------------------------------------- 一次完整同步

def run_sync(scfg, index=True, progress=None):
    """同步一次并写状态文件，返回报告。跨进程互斥：CLI 与服务进程不会同时同步。"""
    say = progress or (lambda m: None)
    bad = gitlab_auth.problem(scfg["auth"])
    if bad:
        raise core.ElkError(bad)
    if not scfg["dir"]:
        raise core.ElkError("未配置同步目录：请设置 ELK_CODE_ROOT 或 ELK_REPO_SYNC_DIR")
    if not _RUN_LOCK.acquire(blocking=False):
        raise core.ElkError("同步正在进行中")
    try:
        os.makedirs(scfg["dir"], exist_ok=True)
        with code._Lock(scfg["code"], "repo-sync"):
            return _run_sync(scfg, index, say)
    finally:
        _RUN_LOCK.release()


def _run_sync(scfg, index, say):
    start = time.time()
    report = {"started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "running": True, "total": 0,
              "cloned": [], "updated": 0, "adopted": [], "failed": [], "stale": [],
              "indexed": [], "index_fresh": 0, "index_failed": [], "index_skipped": None}
    _write_status(scfg, report)
    scfg["auth"] = gitlab_auth.load()  # 每次重新检测：本机凭据或 SSH 配置变化后下次同步即生效
    try:
        report["user"] = check_login(scfg)
        projects = choose_transport(scfg, list_projects(scfg))
        report["auth"] = gitlab_auth.describe(scfg["auth"])
        report["detect"] = scfg["auth"]["detect"]
    except core.ElkError as e:
        report["detect"] = scfg["auth"]["detect"]
        report.update(running=False, error=str(e), finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        _write_status(scfg, report)
        say(_log(scfg, "同步失败：%s" % e))
        raise
    manifest = _load_manifest(scfg)
    plan_dirs(scfg, projects, manifest)
    report["total"] = len(projects)
    say(_log(scfg, "以 %s 登录，开始同步 %d 个项目 → %s" % (report["user"], len(projects), scfg["dir"])))
    lock = threading.Lock()

    def one(p):
        action, err = _sync_one(scfg, p, manifest)
        with lock:
            if err:
                report["failed"].append({"repo": p["path_with_namespace"], "error": err})
                say(_log(scfg, "✘ %s：%s" % (p["path_with_namespace"], err)))
            elif action == "cloned":
                report["cloned"].append(p["dir"])
                say(_log(scfg, "+ clone %s → %s" % (p["path_with_namespace"], p["dir"])))
            elif action == "adopted":
                report["adopted"].append(p["dir"])
            else:
                report["updated"] += 1

    with ThreadPoolExecutor(max_workers=scfg["jobs"]) as pool:
        list(pool.map(one, projects))
    live = {str(p["id"]) for p in projects}
    report["stale"] = sorted(v["path"] for k, v in manifest["projects"].items() if k not in live)
    _save_manifest(scfg, manifest)
    code._MEMO.clear()  # 新仓库立即可被 code_* 发现
    say(_log(scfg, "仓库同步完成：新增 %d，更新 %d，失败 %d，耗时 %ds"
             % (len(report["cloned"]), report["updated"], len(report["failed"]), time.time() - start)))
    if index and scfg["index_envs"]:
        ok_dirs = sorted({p["dir"] for p in projects} - {f["repo"] for f in report["failed"]})
        say(_log(scfg, "预建索引：环境 %s" % ",".join(scfg["index_envs"])))
        prebuild_indexes(scfg, ok_dirs, report)
        say(_log(scfg, "索引：新建/更新 %d，已是最新 %d，失败 %d%s" % (
            len(report["indexed"]), report["index_fresh"], len(report["index_failed"]),
            "（跳过：%s）" % report["index_skipped"] if report["index_skipped"] else "")))
    report.update(running=False, finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  seconds=int(time.time() - start))
    _write_status(scfg, report)
    return report


def _write_status(scfg, report):
    try:
        data = dict(report)
        if scfg["interval"] > 0 and not report.get("running"):
            data["next_run"] = datetime.fromtimestamp(time.time() + scfg["interval"]).strftime("%Y-%m-%d %H:%M:%S")
        tmp = status_path(scfg) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, status_path(scfg))
    except OSError:
        pass


def read_status(scfg):
    try:
        with open(status_path(scfg), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- 定时与触发

_SCHEDULED = False


def start_scheduler(scfg, log, first_delay=15):
    """后台线程：启动后稍等片刻同步一次，之后按间隔同步；request_sync() 可提前唤醒。interval=0 时只响应手动触发。"""
    global _SCHEDULED
    _SCHEDULED = True

    def loop():
        _TRIGGER.wait(first_delay if scfg["interval"] > 0 else None)
        while True:
            _TRIGGER.clear()
            try:
                st = run_sync(scfg)
                log("仓库同步完成：共 %s 个项目，新增 %d，失败 %d；索引新建 %d，失败 %d" % (
                    st["total"], len(st["cloned"]), len(st["failed"]), len(st["indexed"]), len(st["index_failed"])))
            except Exception as e:  # noqa: BLE001 — 定时线程不能因任何异常退出；详情见状态文件与 sync.log
                log("仓库同步失败：%s" % e)
            _TRIGGER.wait(scfg["interval"] if scfg["interval"] > 0 else None)

    threading.Thread(target=loop, name="repo-sync", daemon=True).start()


def request_sync(scfg):
    """手动触发一次后台同步。有调度线程时唤醒它，否则（stdio 进程）单独起线程。已在同步中返回 False。"""
    if _RUN_LOCK.locked():
        return False
    if _SCHEDULED:
        _TRIGGER.set()
    else:
        threading.Thread(target=_run_quietly, args=(scfg,), name="repo-sync-once", daemon=True).start()
    return True


def _run_quietly(scfg):
    try:
        run_sync(scfg)
    except Exception:  # noqa: BLE001 — 错误已写入状态文件与 sync.log
        pass


# ---------------------------------------------------------------- 展示

def describe(scfg, grep=None):
    if scfg is None:
        return ("未配置 GitLab 仓库同步。在服务端 .env 中设置 ELK_GITLAB_URL 与 ELK_GITLAB_TOKEN"
                "（read_api + read_repository）后，会定时把有权限的仓库同步到代码目录。")
    lines = ["# GitLab 仓库同步",
             "GitLab: %s" % scfg["url"],
             "认证: %s" % (gitlab_auth.problem(scfg["auth"]) or (read_status(scfg) or {}).get("auth")
                          or gitlab_auth.describe(scfg["auth"])),
             "范围: %s%s" % ("组 " + ",".join(scfg["groups"]) if scfg["groups"]
                                          else "账号可访问的全部项目",
                                          "  排除: " + ",".join(scfg["exclude"]) if scfg["exclude"] else ""),
             "目录: %s  间隔: %s  预建索引: %s" % (
                 scfg["dir"], "%d 小时" % (scfg["interval"] // 3600) if scfg["interval"] >= 3600
                 else ("%d 秒" % scfg["interval"] if scfg["interval"] else "关闭（仅手动）"),
                 ",".join(scfg["index_envs"]) or "不预建")]
    st = read_status(scfg)
    if not st:
        lines.append("状态: 尚未同步过")
    else:
        if st.get("running"):
            lines.append("状态: 同步中（开始于 %s）" % st["started"])
        elif st.get("error"):
            lines.append("状态: 上次同步失败（%s）：%s" % (st.get("finished"), st["error"]))
        else:
            lines.append("上次同步: %s（耗时 %ss，账号 %s）  共 %s 个项目，新增 %d，更新 %d，失败 %d%s" % (
                st.get("finished"), st.get("seconds", "-"), st.get("user", "-"), st.get("total"),
                len(st.get("cloned", [])),
                st.get("updated", 0), len(st.get("failed", [])),
                "  下次: %s" % st["next_run"] if st.get("next_run") else ""))
            if st.get("indexed") or st.get("index_failed") or st.get("index_fresh"):
                lines.append("索引: 新建/更新 %d，已是最新 %d，失败 %d" % (
                    len(st.get("indexed", [])), st.get("index_fresh", 0), len(st.get("index_failed", []))))
        for d in st.get("detect", []):
            lines.append("  · " + d)
        for f in st.get("failed", [])[:10]:
            lines.append("  ✘ %s：%s" % (f["repo"], f["error"][:160]))
        for f in st.get("index_failed", [])[:10]:
            lines.append("  ✘ 索引 %s（%s）：%s" % (f["repo"], f["env"], f["error"][:160]))
        if st.get("stale"):
            lines.append("已无权限或已删除（本地保留，未删除）: %s" % ", ".join(st["stale"][:20]))
    m = _load_manifest(scfg)["projects"]
    items = sorted((v["dir"], v["path"]) for v in m.values())
    if grep:
        items = [i for i in items if grep.lower() in (i[0] + " " + i[1]).lower()]
    lines.append("\n# 已同步仓库（%d）" % len(items))
    lines += ["%-36s %s" % i for i in items[:300]]
    return "\n".join(lines)
