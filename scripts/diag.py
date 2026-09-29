"""elk MCP 自诊断：结构化调用日志 + doctor（诊断，并在 fix 模式下只修复缓存/运行时状态）。仅标准库。

修复边界：
- 自动修复（fix=True）：缓存目录内的残留/损坏 worktree、失效的 GitNexus 注册项与主仓库 worktree 登记、
  孤儿运行时文件、失败的索引任务、被改脏的只读 worktree、闲置清理。
- 只提示：cc-switch 配置与凭据、其他会话的 MCP 进程（重连）、MCP 自身代码缺陷（给出位置，由用户确认后修复）。
"""
import calendar
import difflib
import glob
import json
import os
import re
import shutil
import subprocess
import time
import traceback
from datetime import datetime

import code_core as code
import elk_core as core

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_MAX = 5 * 1024 * 1024
CLAUDE_MCP_LOGS = os.path.expanduser("~/Library/Caches/claude-cli-nodejs/*/mcp-logs-elk/*.jsonl")
GITNEXUS_REGISTRY = os.path.expanduser("~/.gitnexus/registry.json")

# (类别, 匹配, 级别, 处理建议)；按顺序匹配第一个
CATEGORIES = [
    ("text_field", core.TEXT_FIELD_ERR, "info", "对 text 字段聚合/排序；当前版本已自动改用 .keyword 重试"),
    ("auth_401", re.compile(r"HTTP 401"), "warn", "用户名/密码错误或已过期：在 cc-switch 的 elk MCP env 中更新凭据"),
    ("auth_403", re.compile(r"HTTP 403"), "warn",
     "账号缺少权限：ping/检索已自动回退；若检索仍 403，需要在 Kibana 给账号授予索引 read 权限"),
    ("bad_request", re.compile(r"HTTP 400"), "info", "查询语法或字段错误：检查 Lucene 语法，用 elk_fields 核对字段名"),
    ("server_5xx", re.compile(r"HTTP 5\d\d"), "warn", "ES/Kibana 服务端异常：稍后重试或缩小时间范围"),
    ("timeout", re.compile(r"超时|timed out|timeout", re.I), "warn", "查询超时：缩小 since 范围，或调大 ELK_<ENV>_TIMEOUT"),
    ("connect", re.compile(r"连接失败|Connection refused|URLError|Name or service not known|nodename"), "warn",
     "网络不通：检查 VPN/内网 DNS；需要代理时配置 ELK_<ENV>_PROXY"),
    ("keychain", re.compile(r"钥匙串"), "warn", "钥匙串缺少凭据：按报错提示，由用户自己在终端执行 security add-generic-password"),
    ("prod_guard", re.compile(r"生产环境"), "info", "生产保护拦截（预期行为）"),
    ("unmapped_service", re.compile(r"未能把 .* 映射"), "info", "服务映射不到代码：在 ELK_CODE_SERVICE_MAP 中配置，或传 repo/module"),
    ("bad_arg", re.compile(r"未知环境|必须是整数|需要 host、service 或 repo|未从 stack 中解析|没有仓库|重名，请使用"), "info",
     "调用参数错误（环境名/仓库名写错、缺参数）：用 elk_envs / code_services 核对"),
    ("config", re.compile(r"未配置|未找到环境配置|不是合法 JSON|ELK_CODE_ROOT"), "warn",
     "配置缺失或有误：在 cc-switch 的 elk MCP env 中补充/修正"),
    ("git", re.compile(r"^git |git \S+ (失败|超时)"), "warn", "git 操作失败：检查仓库状态、SSH 凭据与网络"),
    ("gitnexus", re.compile(r"gitnexus", re.I), "warn", "GitNexus 相关失败：查看索引日志，或执行 doctor fix 重建"),
    ("internal", re.compile(r"内部错误"), "fail", "MCP 自身代码缺陷：按位置修复代码（skill 目录已纳入 git，可 git diff 审阅）"),
]


def classify(msg):
    for cat, rx, level, hint in CATEGORIES:
        if rx.search(msg or ""):
            return cat, level, hint
    return "other", "warn", "未归类错误：查看错误原文"


# ---------------------------------------------------------------- 结构化调用日志

def log_path():
    return os.path.join(code._cache_dir(code.load_code_config()), "mcp.log")


def exc_where(exc):
    """异常在本 skill 代码中的最后一帧：file:line func。"""
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename.startswith(SCRIPTS_DIR)]
    if not frames:
        return None
    f = frames[-1]
    return "%s:%d %s" % (os.path.basename(f.filename), f.lineno, f.name)


def log_call(tool, args, ok, ms, err=None, where=None, healed=None):
    """每次工具调用一行 JSON：不记录参数值（只记键名与 env）、不记录日志内容与凭据。写日志失败不影响调用。"""
    try:
        rec = {"ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "pid": os.getpid(), "tool": tool,
               "env": (args or {}).get("env") or "", "args": sorted((args or {}).keys()), "ok": ok, "ms": ms}
        if err:
            msg = str(err)
            for pattern, repl in core.MASK_RULES:
                msg = pattern.sub(repl, msg)
            rec.update(cat=classify(msg)[0], err=msg[:300])
        if where:
            rec["where"] = where
        if healed:
            rec["healed"] = healed
        path = log_path()
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- MCP 实际配置

def find_mcp_server():
    """在 ~/.claude.json 中找 elk MCP 配置（全局或任一项目下）。返回 (配置 dict, 位置说明) 或 (None, None)。"""
    path = os.path.expanduser("~/.claude.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, None
    if isinstance(data.get("mcpServers"), dict) and "elk" in data["mcpServers"]:
        return data["mcpServers"]["elk"], "~/.claude.json mcpServers.elk"
    for proj, pc in (data.get("projects") or {}).items():
        servers = (pc or {}).get("mcpServers") or {}
        if "elk" in servers:
            return servers["elk"], "~/.claude.json projects[%s].mcpServers.elk" % proj
    return None, None


def apply_mcp_env():
    """CLI 运行 doctor 时套用 MCP 的实际 env（仅当前进程内存中），使诊断口径与 MCP 一致。"""
    srv, _ = find_mcp_server()
    if srv and not os.environ.get("ELK_ENVS"):
        for k, v in (srv.get("env") or {}).items():
            os.environ.setdefault(k, str(v))
        return True
    return False


# ---------------------------------------------------------------- 报告

class Report:
    ICON = {"OK": "[OK]   ", "INFO": "[INFO] ", "WARN": "[WARN] ", "FAIL": "[FAIL] ", "FIXED": "[FIXED]"}

    def __init__(self, fix):
        self.fix = fix
        self.rows = []

    def sec(self, title):
        self.rows.append(("SEC", title, None, None))

    def add(self, level, msg, action=None, fix=None):
        """fix=(说明, 无参函数)：fix 模式下立即执行，否则列入「可自动修复」。"""
        if fix and self.fix:
            desc, fn = fix
            try:
                res = fn()
                level, msg = "FIXED", "%s → 已修复：%s%s" % (msg, desc, "（%s）" % res if res else "")
                fix = None
            except Exception as e:  # noqa: BLE001
                level, msg = "FAIL", "%s → 自动修复失败：%s" % (msg, e)
                fix = None
        self.rows.append((level, msg, action, fix))

    def ok(self, msg):
        self.add("OK", msg)

    def info(self, msg):
        self.add("INFO", msg)

    def warn(self, msg, action=None, fix=None):
        self.add("WARN", msg, action, fix)

    def failed(self, msg, action=None, fix=None):
        self.add("FAIL", msg, action, fix)

    def render(self, title):
        out = [title]
        for level, msg, _, _ in self.rows:
            out.append("\n## %s" % msg if level == "SEC" else "%s %s" % (self.ICON[level], msg))
        todo = [(lv, m, a) for lv, m, a, _ in self.rows if a and lv in ("WARN", "FAIL")]
        fixable = [(m, f[0]) for lv, m, _, f in self.rows if f]
        fixed = [m for lv, m, _, _ in self.rows if lv == "FIXED"]
        counts = {k: sum(1 for r in self.rows if r[0] == k) for k in ("FAIL", "WARN", "FIXED")}
        out.append("\n## 结论：FAIL %d / WARN %d / 已修复 %d" % (counts["FAIL"], counts["WARN"], counts["FIXED"]))
        if fixable:
            out.append("### 可自动修复（%d 项）：执行 elk_doctor {fix:true} 或 elk.py doctor --fix" % len(fixable))
            out += ["- %s → %s" % (m.split("：")[0][:90], d) for m, d in fixable]
        if todo:
            out.append("### 需要你处理（%d 项）" % len(todo))
            out += ["%d. [%s] %s\n   处理：%s" % (i + 1, lv, m[:160], a) for i, (lv, m, a) in enumerate(todo)]
        if not (fixable or todo or fixed):
            out.append("一切正常。")
        return "\n".join(out)


def _guard(r, name, fn, *args):
    """单项检查自身出错时记为 FAIL，不影响其他检查。"""
    try:
        fn(r, *args)
    except Exception as e:  # noqa: BLE001
        r.failed("检查「%s」自身出错：%s（%s）" % (name, e, exc_where(e) or "-"),
                 action="属于 doctor 代码缺陷，请让 Claude 修复")


# ---------------------------------------------------------------- 检查项

def check_config(r, ctx):
    r.sec("配置")
    srv, where = find_mcp_server()
    if not srv:
        r.warn("~/.claude.json 中没有找到 elk MCP 配置", action="在 cc-switch 中启用 elk MCP 并同步到 Claude Code")
    else:
        args = " ".join(srv.get("args") or [])
        if SCRIPTS_DIR not in args and os.path.realpath(SCRIPTS_DIR) not in args:
            r.warn("MCP 启动的不是本目录的脚本：%s" % args, action="在 cc-switch 中把 elk MCP 的启动参数指向 %s/mcp_server.py" % SCRIPTS_DIR)
        else:
            r.ok("MCP 配置位置：%s，启动脚本指向本目录" % where)
        if ctx["cli"] and "ELK_CODE_ROOT" not in (srv.get("env") or {}):
            ctx["code_root_warned"] = True
            r.warn("MCP env 中没有 ELK_CODE_ROOT，code_* 工具不可用",
                   action="在 cc-switch 的 elk MCP env 中添加 ELK_CODE_ROOT=~/code（多个目录逗号分隔），然后重连 elk")
    cfg = core.load_config()
    ctx["cfg"] = cfg
    envs = cfg.get("environments", {})
    default = cfg.get("default_env")
    r.ok("环境：%s；默认 %s" % (", ".join("%s%s" % (n, "(PROD)" if e.get("production") else "") for n, e in envs.items()), default))
    if default in envs and envs[default].get("production"):
        r.failed("默认环境 %s 是生产环境" % default, action="在 cc-switch 中把 ELK_DEFAULT_ENV 改为非生产环境")
    for name in envs:
        env = core.get_env(cfg, name)
        target = env.get("kibana_url") if env.get("mode") == "kibana" else env.get("es_url")
        if not target:
            r.failed("环境 %s 未配置地址" % name, action="在 cc-switch 中设置 %s" % env.get("_url_var"))
            continue
        try:
            core.auth_header(env, interactive=False)  # 只验证凭据可解析，不输出
            r.ok("环境 %s：%s，凭据可解析（%s）" % (name, target, env.get("auth", {}).get("type")))
        except core.ElkError as e:
            r.failed("环境 %s 凭据不可用：%s" % (name, str(e).splitlines()[0]), action=classify(str(e))[2])
    ccfg = code.load_code_config()
    ctx["ccfg"] = ccfg
    if not ccfg["roots"]:
        if not ctx.get("code_root_warned"):
            r.warn("未配置 ELK_CODE_ROOT，code_* 工具不可用",
                   action="在 cc-switch 的 elk MCP env 中添加 ELK_CODE_ROOT=~/code，然后重连 elk")
        return
    missing = [p for p in ccfg["roots"] if not os.path.isdir(p)]
    if missing:
        r.warn("ELK_CODE_ROOT 中的目录不存在：%s" % ", ".join(missing), action="在 cc-switch 中修正 ELK_CODE_ROOT")
    if len(missing) < len(ccfg["roots"]):
        r.ok("代码目录：%s，共 %d 个仓库" % (", ".join(ccfg["roots"]), len(code.repo_map(ccfg))))


CODE_KEYS = ("ROOT", "BRANCHES", "SERVICE_MAP", "CACHE", "FETCH_TTL", "LEASE", "GC_DAYS", "GC_EXACT_DAYS",
             "AUTO_INDEX", "GITNEXUS")
SECRET_KEYS = ("PASSWORD", "API_KEY", "USERNAME")


def check_config_layers(r, ctx):
    """分层配置体检：配置来源、数组中无法识别的字段、被数组取代而失效的平铺变量、未知/拼错的变量、
    未启用环境的配置、级别与生产标记矛盾、可删除的重复项、可提取为全局的共用项。"""
    r.sec("配置分层")
    cfg = ctx.get("cfg")
    if not cfg:
        r.info("配置未加载，跳过")
        return
    envs = cfg.get("environments", {})
    layers = cfg.get("_layers") or {}
    source = layers.get("source") or "envs.json"
    array = source == "ELK_ENVIRONMENTS" or source.endswith(" environments")
    r.ok("配置来源：%s（%s，%d 个环境：%s）" % (source, "数组写法" if array else "平铺写法", len(envs), ", ".join(envs)))
    if not source.startswith(core.CONFIG_PATH) and os.path.exists(core.CONFIG_PATH):
        r.warn("%s 存在但不会被读取（当前配置来源优先于它）" % core.CONFIG_PATH,
               action="确认内容已迁入 %s 后删除该文件，避免误以为它还在起作用" % source.split(" ")[0])
    for item in layers.get("unknown", []):
        field = item.rsplit(".", 1)[-1]
        close = difflib.get_close_matches(field.upper(), core.ENV_KEYS, n=1, cutoff=0.6)
        r.warn("无法识别的字段 %s，不会生效%s" % (item, "（是否想写 %s？）" % close[0].lower() if close else ""),
               action="在 %s 中修正字段名；可用字段：%s" % (
                   "cc-switch" if source.startswith("ELK_") else source.split(" ")[0],
                   ", ".join(k.lower() for k in core.ENV_KEYS)))
    ignored = layers.get("ignored", [])
    if ignored:
        r.warn("以下平铺变量已被 %s 取代，不会生效：%s" % (source, ", ".join(ignored)),
               action="在 cc-switch 中删除这些变量，避免误以为它们还在起作用")
    # 未知/拼错的平铺变量（仅 MCP env 方式）
    elk = {k: v for k, v in os.environ.items() if k.startswith("ELK_")}
    if source in ("ELK_ENVS", "ELK_ENVIRONMENTS"):
        prefixes = {core.env_prefix(n): n for n in envs}
        known = {"ELK_" + k for k in core.SYSTEM_KEYS + core.GLOBAL_KEYS} | {"ELK_CODE_" + k for k in CODE_KEYS}
        known |= {p + k for p in prefixes for k in core.ENV_KEYS}
        for var in sorted(elk):
            if var in known or var in ignored:
                continue
            m = re.match(r"^ELK_([A-Z0-9_]+?)_(%s)$" % "|".join(core.ENV_KEYS), var)
            if m and not var.startswith("ELK_CODE_"):
                r.warn("%s：环境 %s 未启用，这项配置不会生效" % (var, m.group(1).lower()),
                       action="把 %s 加入 %s，或删除该变量" % (m.group(1).lower(), source))
                continue
            close = difflib.get_close_matches(var, sorted(known), n=1, cutoff=0.75)
            r.warn("未知变量 %s，不会生效%s" % (var, "（是否想写 %s？）" % close[0] if close else ""),
                   action="在 cc-switch 中改正变量名：%s" % (close[0] if close else "参考 references/mcp-config.md"))
    ccfg = ctx.get("ccfg") or code.load_code_config()
    for name in envs:
        env = core.get_env(cfg, name)
        src = env.get("_src", {})
        if src.get("production", "").startswith("级别强制"):
            r.warn("环境 %s 的级别是 prod，PRODUCTION=false 已被忽略，仍按生产保护" % name,
                   action="删除该配置；如果它确实不是生产环境，显式设置 tier=test/staging")
        if env.get("production") and env.get("mask") is False:
            r.warn("生产环境 %s 关闭了脱敏（mask=false）" % name, action="除非确有必要，删除 mask 配置")
        if not env.get("tier") and not env.get("branch"):
            r.warn("环境 %s 无法按名称识别级别，也没有配置 branch，code_* 无法确定代码分支" % name,
                   action="为该环境设置 tier（dev/test/staging/prod）或 branch")
            continue
        spec, spec_src = code.branch_spec(ccfg, env)
        r.ok("环境 %s：级别 %s（%s）%s，代码分支 %s（%s）" % (
            name, env.get("tier"), src.get("tier", "-"), "，PROD" if env.get("production") else "", spec, spec_src))
    # 与全局相同的环境值 → 可删除；所有环境相同且无全局 → 可提取为全局
    env_values, globals_ = layers.get("env_values", {}), layers.get("globals", {})

    def where(n, k):
        return "%s[%s].%s" % (source, n, k.lower()) if array else "%s%s" % (core.env_prefix(n), k)
    redundant = [where(n, k) for n, kv in env_values.items() for k, v in kv.items()
                 if k in core.GLOBAL_KEYS and globals_.get(k) == v]
    if redundant:
        r.info("与全局值相同、可删除的环境配置：%s" % ", ".join(redundant))
    if len(env_values) > 1 and not array:  # 数组写法按“每个环境自成一体”设计，不建议提取全局
        present = {k: [kv[k] for kv in env_values.values() if k in kv] for k in core.GLOBAL_KEYS if k not in globals_}
        hoist = [k for k, vals in present.items() if len(vals) == len(env_values) and len(set(vals)) == 1]
        partial = [k for k, vals in present.items() if k not in hoist and len(vals) > 1 and len(set(vals)) < len(vals)]
        if hoist or partial:
            msg = []
            if hoist:
                msg.append("所有环境相同，可改为全局 ELK_<KEY>：%s" % ", ".join(hoist))
            if partial:
                msg.append("部分环境相同，可设为全局默认、个别环境覆盖：%s" % ", ".join(partial))
            r.info("；".join(msg) + "（可选；凭据只比较是否相同，不输出值）")
    r.info("取值优先级：环境（%s）> 全局 ELK_<KEY> > 级别预设 > 内置默认" % ("数组中的环境项" if array else "ELK_<ENV>_<KEY>"))


def check_connectivity(r, ctx, confirm_prod):
    r.sec("ELK 连通性与字段")
    cfg = ctx.get("cfg")
    if not cfg:
        r.info("配置未加载，跳过")
        return
    for name, raw in cfg.get("environments", {}).items():
        if raw.get("production") and not confirm_prod:
            r.info("环境 %s 是生产环境，未检查（需要时传 confirm_production）" % name)
            continue
        env = core.get_env(cfg, name)
        try:
            r.ok("环境 %s 连通：%s" % (name, core.op_ping(env, interactive=False)))
        except core.ElkError as e:
            r.failed("环境 %s 不可用：%s" % (name, str(e).splitlines()[0][:200]), action=classify(str(e))[2])
            continue
        if name == cfg.get("default_env"):
            ctx["reachable_default"] = env
        caps = core.call(env, "GET", "/%s/_field_caps?fields=*" % core.resolve_index(env), interactive=False)
        have = caps.get("fields", {}) if isinstance(caps, dict) else {}
        f = env["_fields"]
        need = {"timestamp": f["timestamp"], "sort": f["sort"], "message": f["message"], "level": f["level"],
                "trace": f["trace"], "host.keyword": f.get("host_keyword") or f["host"] + ".keyword"}
        miss = [("%s(%s)" % (k, v)) for k, v in need.items() if v not in have]
        if miss:
            r.warn("环境 %s 缺少字段：%s" % (name, ", ".join(miss)),
                   action="在 ELK_%s_FIELDS 中覆盖字段映射（用 elk_fields 查实际字段名）" % name.upper())
        elif "date" not in have.get(f["timestamp"], {}):
            r.warn("环境 %s 的时间字段 %s 不是 date 类型" % (name, f["timestamp"]),
                   action="在 ELK_FIELDS 中把 timestamp 指向 date 类型字段")
        else:
            r.ok("环境 %s 关键字段齐全（%s）" % (name, ", ".join(need.values())))


def check_tools(r, ctx):
    r.sec("依赖工具")
    g = subprocess.run(["git", "--version"], capture_output=True, text=True)
    (r.ok if g.returncode == 0 else r.failed)("git：%s" % (g.stdout.strip() or g.stderr.strip()))
    ccfg = ctx.get("ccfg") or code.load_code_config()
    gn = ccfg["gitnexus"]
    if not os.path.exists(gn):
        r.warn("未找到 gitnexus（%s），code_prepare 无法建索引" % gn,
               action="安装 gitnexus（npm i -g gitnexus），或在 ELK_CODE_GITNEXUS 中指定路径")
    else:
        v = subprocess.run([gn, "--version"], capture_output=True, text=True, timeout=30, env=code._gitnexus_env(ccfg))
        if v.returncode == 0:
            r.ok("gitnexus：%s（%s）" % (v.stdout.strip(), gn))
        else:
            r.failed("gitnexus 无法运行：%s" % (v.stderr or v.stdout).strip()[:200],
                     action="检查 node 是否安装、gitnexus 是否完整")
    probe = os.path.join(code._cache_dir(ccfg), ".doctor-probe")
    with open(probe, "w"):
        pass
    os.remove(probe)
    r.ok("缓存目录可写：%s" % ccfg["cache"])


def check_service_mapping(r, ctx):
    r.sec("服务 → 代码映射覆盖率")
    ccfg, env = ctx.get("ccfg"), ctx.get("reachable_default")
    if not ccfg or not ccfg["roots"] or not env:
        r.info("代码目录未配置或默认环境不可达，跳过")
        return
    host_kw = env["_fields"].get("host_keyword") or env["_fields"]["host"] + ".keyword"
    body = {"size": 0, "query": {"range": {env["_fields"]["timestamp"]: {"gte": "now-1d"}}},
            "aggs": {"h": {"terms": {"field": host_kw, "size": 300}}}}
    res = core.call(env, "POST", "/%s/_search" % core.resolve_index(env), body, interactive=False)
    deps = sorted({code.parse_host(b["key"])[0] for b in res.get("aggregations", {}).get("h", {}).get("buckets", [])})
    unmapped = []
    for d in deps:
        try:
            code.resolve_service(ccfg, service=d)
        except core.ElkError:
            unmapped.append(d)
    r.ok("环境 %s 近 1 天 %d 个服务，能映射到代码 %d 个" % (env["_name"], len(deps), len(deps) - len(unmapped)))
    if unmapped:
        r.info("映射不到的服务（本地没有代码可忽略；有代码但名字对不上时配置 ELK_CODE_SERVICE_MAP）：%s"
               % ", ".join(unmapped))


def _registered_worktrees(repo):
    out = code.git(repo, "worktree", "list", "--porcelain", check=False).stdout
    return [line[len("worktree "):] for line in out.splitlines() if line.startswith("worktree ")]


def check_cache(r, ctx):
    r.sec("缓存 worktree 与 GitNexus 索引")
    ccfg = ctx.get("ccfg") or code.load_code_config()
    base = code._cache_dir(ccfg, "worktrees")
    base_real = os.path.realpath(base)
    names = set()
    total = 0
    for entry in sorted(os.listdir(base)):
        wt = os.path.join(base, entry)
        if not os.path.isdir(wt):
            continue
        size = code._du(wt)
        total += size
        name = entry.split(".stale-")[0]
        if ".stale-" in entry or not os.path.exists(os.path.join(wt, ".git")):
            r.warn("残留目录 %s（%s）" % (entry, code._fmt_size(size)),
                   fix=("删除缓存目录内的残留", lambda wt=wt: code.remove_stale_dir(ccfg, wt)))
            continue
        names.add(name)
        head = code.git(wt, "rev-parse", "HEAD", check=False)
        if head.returncode != 0:
            r.failed("worktree %s 已损坏：%s" % (entry, head.stderr.strip()[:120]),
                     fix=("删除损坏的 worktree（下次 code_prepare 自动重建）",
                          lambda wt=wt, name=name: ", ".join(code._remove_worktree(ccfg, wt, name))))
            continue
        dirty = code.git(wt, "status", "--porcelain", check=False).stdout.strip()
        if dirty:
            r.warn("只读 worktree %s 被修改过（%d 处）" % (entry, len(dirty.splitlines())),
                   fix=("还原到 %s（reset --hard + clean -fd，保留 .gitnexus）" % head.stdout.strip()[:9],
                        lambda wt=wt: code.git(wt, "reset", "--hard", "-q") + code.git(wt, "clean", "-fdq")))
        st = code.index_state(ccfg, wt, name)
        if st["state"] == "failed":
            tail = ""
            try:
                with open(st["log"], encoding="utf-8", errors="replace") as f:
                    tail = [ln for ln in f.read().splitlines() if ln.strip()][-1][:160]
            except (OSError, IndexError, TypeError):
                pass
            r.warn("%s 的索引构建失败：%s" % (entry, tail),
                   fix=("重新后台构建索引", lambda wt=wt, name=name, sha=head.stdout.strip():
                        "pid %d" % code.start_index(ccfg, wt, name, sha)))
        else:
            r.ok("%s @ %s：索引 %s，%s" % (entry, head.stdout.strip()[:9], st["state"], code._fmt_size(size)))
    r.info("缓存 worktree 共占用 %s" % code._fmt_size(total))
    # 孤儿运行时文件
    orphans = []
    for sub, suffixes in (("lease", (".stamp",)), ("index", (".json", ".log"))):
        d = code._cache_dir(ccfg, sub)
        for fn in os.listdir(d):
            for suf in suffixes:
                if fn.endswith(suf) and fn[:-len(suf)] not in names:
                    orphans.append(os.path.join(d, fn))
    if orphans:
        r.warn("%d 个孤儿运行时文件（对应的 worktree 已不存在）" % len(orphans),
               fix=("删除孤儿文件", lambda: [os.remove(p) for p in orphans] and None))
    # GitNexus 注册表中指向缓存目录但已不存在的项
    try:
        with open(GITNEXUS_REGISTRY, encoding="utf-8") as f:
            reg = json.load(f)
    except (OSError, ValueError):
        reg = []
    dangling = [e.get("path") for e in reg if isinstance(e, dict) and str(e.get("path", "")).startswith(base_real)
                and not os.path.exists(e.get("path"))]
    for p in dangling:
        r.warn("GitNexus 注册表中有失效项：%s" % p,
               fix=("gitnexus remove -f", lambda p=p: subprocess.run(
                   [ccfg["gitnexus"], "remove", "-f", p], capture_output=True, text=True, timeout=60,
                   env=code._gitnexus_env(ccfg), check=True) and None))
    # 主仓库中指向缓存目录但目录已丢失的 worktree 登记（只注销这一条，不做全局 prune）
    if ccfg["roots"]:
        for repo in code.list_repos(ccfg):
            for p in _registered_worktrees(repo):
                if os.path.realpath(os.path.dirname(p)) == base_real and not os.path.exists(p):
                    r.warn("仓库 %s 中有失效的 worktree 登记：%s" % (os.path.basename(repo), p),
                           fix=("git worktree remove --force（只注销这一条）",
                                lambda repo=repo, p=p: code.git(repo, "worktree", "remove", "--force", p) and None))
    preview = code.op_gc(ccfg, dry_run=True)
    if "将删除" in preview:
        r.warn("有闲置超期的 worktree：%s" % preview.splitlines()[0],
               fix=("执行闲置清理", lambda: code.op_gc(ccfg).splitlines()[0]))


def _proc_start(pid):
    out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    try:
        return time.mktime(datetime.strptime(out, "%a %b %d %H:%M:%S %Y").timetuple())
    except ValueError:
        return None


def check_processes(r, ctx):
    r.sec("MCP 进程与代码版本")
    script = os.path.join(SCRIPTS_DIR, "mcp_server.py")
    code_mtime = _code_mtime()
    pids = subprocess.run(["pgrep", "-f", script], capture_output=True, text=True).stdout.split()
    if not pids:
        r.info("当前没有运行中的 elk MCP 进程")
        return
    old = []
    for pid in pids:
        started = _proc_start(pid)
        if started is not None and started < code_mtime:
            ppid = subprocess.run(["ps", "-o", "ppid=", "-p", pid], capture_output=True, text=True).stdout.strip()
            old.append("pid %s（父进程 %s，启动于 %s）%s" % (
                pid, ppid, datetime.fromtimestamp(started).strftime("%m-%d %H:%M"),
                " ← 当前会话" if int(pid) == os.getpid() else ""))
    r.ok("运行中的 elk MCP 进程 %d 个；代码最后修改于 %s" % (
        len(pids), datetime.fromtimestamp(code_mtime).strftime("%m-%d %H:%M")))
    if old:
        r.warn("%d 个进程运行的是旧代码：%s" % (len(old), "；".join(old)),
               action="在对应会话中执行 /mcp 重连 elk，或新开会话（不会自动结束其他会话的进程）")
    if not ctx["cli"]:
        r.info("如果看不到新工具（如 code_*、elk_doctor），说明当前会话的工具列表未刷新：执行 /mcp 重连 elk")


def _code_mtime():
    return max(os.path.getmtime(p) for p in glob.glob(os.path.join(SCRIPTS_DIR, "*.py")))


def _epoch(ts):
    """mcp.log 为本地时间 YYYY-MM-DDTHH:MM:SS；Claude Code 日志为 UTC（带 Z）。"""
    try:
        if ts.endswith("Z"):
            return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0


def _summarize(r, label, events, days):
    """events: [(ts, tool, msg)]。最近一次发生在当前代码版本之前的非致命错误降为 INFO（可能已被修复，新版本复现才需处理）。"""
    code_mtime = _code_mtime()
    if not events:
        r.ok("%s：近 %d 天无失败调用" % (label, days))
        return
    by = {}
    for ts, tool, msg in events:
        cat, level, hint = classify(msg)
        b = by.setdefault(cat, {"n": 0, "level": level, "hint": hint, "last": ts, "tools": set(), "sample": msg})
        b["n"] += 1
        b["tools"].add(tool)
        if ts > b["last"]:
            b["last"], b["sample"] = ts, msg
    for cat, b in sorted(by.items(), key=lambda kv: -kv[1]["n"]):
        msg = "%s：%s × %d（%s；最近 %s）：%s" % (label, cat, b["n"], ",".join(sorted(b["tools"])), b["last"][:16],
                                           b["sample"].replace("\n", " ")[:120])
        if b["level"] == "warn" and _epoch(b["last"]) < code_mtime:
            r.info(msg + " —— 最近一次发生在当前代码版本之前，新版本复现才需处理；" + b["hint"])
        elif b["level"] == "info":
            r.info(msg + " —— " + b["hint"])
        elif b["level"] == "fail":
            r.failed(msg, action=b["hint"])
        else:
            r.warn(msg, action=b["hint"])


def check_logs(r, ctx, days):
    r.sec("日志分析（近 %d 天）" % days)
    since = time.time() - days * 86400
    cutoff = datetime.fromtimestamp(since).strftime("%Y-%m-%dT%H:%M:%S")
    # 1) 本 MCP 的结构化日志
    recs = []
    for p in (log_path() + ".1", log_path()):
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("ts", "") >= cutoff:
                        recs.append(d)
    if recs:
        fails = [d for d in recs if not d.get("ok")]
        slow = sorted((d for d in recs if d.get("ms", 0) > 20000), key=lambda d: -d["ms"])[:3]
        healed = {}
        for d in recs:
            for h in d.get("healed") or []:
                healed[h["kind"]] = healed.get(h["kind"], 0) + 1
        r.ok("mcp.log：%d 次调用，失败 %d 次（%.0f%%）" % (len(recs), len(fails), 100.0 * len(fails) / len(recs)))
        if healed:
            r.info("运行时自愈：%s" % ", ".join("%s × %d" % kv for kv in healed.items()))
        if slow:
            r.info("最慢调用：%s" % "；".join("%s %.0fs" % (d["tool"], d["ms"] / 1000.0) for d in slow))
        internal = {}
        for d in fails:
            if d.get("cat") == "internal" or d.get("where"):
                internal.setdefault(d.get("where") or "?", d)
        for where, d in internal.items():
            r.failed("MCP 代码缺陷：%s 在 %s 抛出：%s" % (d["tool"], where, d.get("err", "")[:160]),
                     action="让 Claude 按位置修复代码，修复后用 git diff 审阅（skill 目录已纳入 git）")
        _summarize(r, "mcp.log", [(d["ts"], d["tool"], d.get("err", "")) for d in fails
                                  if d.get("cat") != "internal" and not d.get("where")], days)
    else:
        r.info("mcp.log 近 %d 天没有记录（新版本起开始记录）" % days)
    # 2) Claude Code 记录的 MCP 日志（连接失败、工具失败）
    events, conn = [], []
    files = [p for p in glob.glob(CLAUDE_MCP_LOGS) if os.path.getmtime(p) >= since]
    for p in files:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                ts, text = d.get("timestamp", ""), d.get("debug") or d.get("error") or ""
                if ts[:19] < cutoff:
                    continue
                m = re.match(r"Tool '(\w+)' failed after [\d.]+m?s: (.*)", text, re.S)
                if m:
                    events.append((ts, m.group(1), m.group(2)))
                elif re.search(r"Connection failed|failed to (start|connect)|spawn .*ENOENT|Server stderr", text, re.I):
                    conn.append((ts, text))
    r.ok("Claude Code MCP 日志：%d 个文件" % len(files))
    _summarize(r, "Claude Code 日志", events, days)
    if conn:
        r.warn("MCP 连接/启动异常 %d 次，最近 %s：%s" % (len(conn), max(conn)[0][:16], max(conn)[1][:160]),
               action="检查 python3 路径与脚本路径；手动运行 python3 %s/mcp_server.py 看是否报错" % SCRIPTS_DIR)


def run_doctor(fix=False, connectivity=True, confirm_production=False, days=7, cli=False):
    r = Report(fix)
    ctx = {"cli": cli}
    _guard(r, "配置", check_config, ctx)
    _guard(r, "配置分层", check_config_layers, ctx)
    if connectivity:
        _guard(r, "连通性", check_connectivity, ctx, confirm_production)
    _guard(r, "依赖工具", check_tools, ctx)
    if connectivity:
        _guard(r, "服务映射", check_service_mapping, ctx)
    _guard(r, "缓存", check_cache, ctx)
    _guard(r, "进程", check_processes, ctx)
    _guard(r, "日志", check_logs, ctx, days)
    title = "# elk doctor %s（%s）" % (datetime.now().strftime("%Y-%m-%d %H:%M"),
                                      "fix 模式：已执行安全修复" if fix else "只诊断；安全修复需 fix=true")
    return r.render(title)


# ---------------------------------------------------------------- 配置导出（迁移到数组写法）

EXPORT_KEYS = ("URL", "MODE", "INDEX", "INDICES", "USERNAME", "PASSWORD", "API_KEY", "TIER", "ALIASES", "PRODUCTION",
               "MAX_SIZE", "TIMEOUT", "MASK", "VERIFY_SSL", "CA_CERT", "KIBANA_SPACE", "KIBANA_API", "PROXY",
               "COLUMNS", "FIELDS", "BRANCH", "SERVICE_MAP", "ALIGN", "AUTO_INDEX")
NUMERIC = ("MAX_SIZE", "TIMEOUT")
BOOLEAN = ("PRODUCTION", "MASK", "VERIFY_SSL", "AUTO_INDEX")
JSONISH = ("INDICES", "FIELDS", "SERVICE_MAP")


def export_array(cfg, inline_globals=True):
    """当前生效配置 → envs.json 的 environments 数组。只导出显式配置过的项（级别预设能推出来的不重复写）；
    inline_globals=True 时把全局 ELK_<KEY> 也写进每个环境，使每项自成一体。
    凭据：keychain:/env: 引用原样导出；明文密码/API Key 改为 env:ELK_<ENV>_<KEY> 引用（envs.json 禁止明文），
    明文用户名用占位符代替；明文值绝不输出。"""
    layers = cfg.get("_layers") or {}
    globals_ = layers.get("globals", {}) if inline_globals else {}
    out = []
    for name, env in cfg.get("environments", {}).items():
        values = dict(globals_)
        values.update(layers.get("env_values", {}).get(name, {}))
        item = {"env": name}
        for key in EXPORT_KEYS:
            if key not in values:
                continue
            v = values[key]
            if key in ("PASSWORD", "API_KEY") and not str(v).startswith(("keychain:", "env:")):
                v = "env:" + core.env_prefix(name) + key
            elif key == "USERNAME" and not str(v).startswith(("keychain:", "env:")):
                v = "<用户名：沿用原值>"
            elif key in NUMERIC:
                v = int(v)
            elif key in BOOLEAN:
                v = core._bool(v)
            elif key in JSONISH:
                v = json.loads(v)
            elif key in ("ALIASES", "COLUMNS"):
                v = [x.strip() for x in str(v).split(",") if x.strip()]
            item[key.lower()] = v
        if name == cfg.get("default_env"):
            item["default"] = True
        out.append(item)
    return out
