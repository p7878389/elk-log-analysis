"""ELK 只读查询核心逻辑，供 CLI(elk.py) 与 MCP(mcp_server.py) 共用。

仅依赖 Python 3.8+ 标准库。
配置：优先读 MCP env（ELK_ENVS 等变量），其次 cc-switch 中 elk MCP 条目的顶层 elk 对象，
最后 ~/.config/elk-log-analysis/envs.json（ELK_CONFIG 可覆盖）。
"""
import base64
import getpass
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CONFIG_PATH = os.path.expanduser(
    os.environ.get("ELK_CONFIG", "~/.config/elk-log-analysis/envs.json"))
# cc-switch 数据库与其中 elk MCP 条目的 id；环境配置作为 JSON 对象放在该条目 server 配置的顶层 elk 字段
CCSWITCH_DB = os.path.expanduser(os.environ.get("ELK_CCSWITCH_DB", "~/.cc-switch/cc-switch.db"))
CCSWITCH_ID = os.environ.get("ELK_CCSWITCH_ID", "elk")
CCSWITCH_LABEL = "cc-switch:%s.elk" % CCSWITCH_ID

# 只允许这些只读 API，防止误操作写入/删除
READONLY_API = re.compile(
    r"^/?[^/]*/?(_search|_count|_mapping|_field_caps)$"
    r"|^/?_cat/(indices|aliases)(/.*)?$"
    r"|^/?_resolve/index/.*$"
    r"|^/?$")

DEFAULT_FIELDS = {
    "timestamp": "@timestamp",    # 仅用于时间范围过滤与趋势聚合（需 date 类型）
    "sort": "time.keyword",        # 排序字段（默认升序）
    "time": "time",                # 展示用时间字段，缺失时回退 timestamp
    "message": "message",
    "level": "level",
    "service": "app_name",
    "trace": "traceId",
    "logger": "logger_name",
    "host": "host.name",
    "stack": "stack_trace",
}

# 明细默认展示列；可用环境 COLUMNS 或 columns 参数覆盖
DEFAULT_COLUMNS = ["time.keyword", "level", "host.name", "traceId", "message"]

# 输出脱敏：手机号、身份证号
MASK_RULES = [
    (re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)"), r"\1****\2"),
    (re.compile(r"(?<![\dA-Za-z])(\d{6})\d{8}(\d{3}[\dXx])(?![\dA-Za-z])"), r"\1********\2"),
]


class ElkError(Exception):
    pass


# 本次调用中发生的运行时自愈（如 403 回退、text 字段改用 keyword），由 MCP 取出写入结构化日志。
# 按线程隔离：HTTP 模式下每个请求一个线程，并发调用互不串味
_HEAL = threading.local()


def heal_events():
    if not hasattr(_HEAL, "events"):
        _HEAL.events = []
    return _HEAL.events


def _healed(kind, detail):
    heal_events().append({"kind": kind, "detail": detail})


TEXT_FIELD_ERR = re.compile(r"Text fields are not optimised|fielddata is disabled on text fields|Fielddata is disabled")


# ---------------------------------------------------------------- config

def _bool(v, default=False):
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _json(v, label):
    if not v:
        return {}
    try:
        return json.loads(v)
    except ValueError:
        raise ElkError("%s 不是合法 JSON" % label)


def _secret_from_env(value, var):
    """MCP env 中的凭据值：支持明文、keychain:<service>/<account>、env:<VAR>。"""
    if value is None or value == "":
        return {"type": "missing", "name": var}
    if value.startswith("keychain:"):
        service, _, account = value[len("keychain:"):].partition("/")
        return {"type": "keychain", "service": service or "elk-log-analysis", "account": account}
    if value.startswith("env:"):
        return {"type": "env", "name": value[4:]}
    return {"type": "inline", "value": value}


# ---------------------------------------------------------------- 分层配置
# 取值优先级：环境 ELK_<ENV>_<KEY>  >  全局 ELK_<KEY>  >  环境级别预设  >  内置默认

# 环境级别：按环境名推断（也可用 ELK_<ENV>_TIER 显式指定），决定安全策略与代码分支等预设
TIER_RULES = [
    ("prod", re.compile(r"^(prod|production|prd|live|online)$|生产|线上", re.I)),
    ("staging", re.compile(r"^(stag|staging|stage|sta|pre|preprod|uat)$|预发", re.I)),
    ("test", re.compile(r"^(test|testing|beta|qa|sit)$|测试", re.I)),
    ("dev", re.compile(r"^(dev|develop|local)$|开发", re.I)),
]
TIER_PRESETS = {
    "prod": {"production": True, "branch": "master,main", "align": "deploy", "aliases": ["生产", "线上"]},
    "staging": {"production": False, "branch": "staging", "align": "deploy", "aliases": ["预发"]},
    "test": {"production": False, "branch": "beta", "align": "deploy", "aliases": ["测试"]},
    "dev": {"production": False, "branch": "dev", "align": "head", "aliases": ["开发"]},
}
# 可放在全局（所有环境共用）的键；其余键只能按环境配置
GLOBAL_KEYS = ("URL", "MODE", "INDEX", "INDICES", "USERNAME", "PASSWORD", "API_KEY", "VERIFY_SSL", "CA_CERT",
               "KIBANA_SPACE", "KIBANA_API", "PROXY", "MAX_SIZE", "TIMEOUT", "MASK", "COLUMNS")
ENV_ONLY_KEYS = ("TIER", "ALIASES", "PRODUCTION", "FIELDS", "BRANCH", "SERVICE_MAP", "ALIGN", "AUTO_INDEX")
SYSTEM_KEYS = ("ENVS", "ENVIRONMENTS", "DEFAULT_ENV", "FIELDS", "CONFIG", "ENV")
ENV_KEYS = GLOBAL_KEYS + ENV_ONLY_KEYS


def infer_tier(name):
    for tier, rx in TIER_RULES:
        if rx.search(name):
            return tier
    return None


def env_prefix(name):
    return "ELK_%s_" % re.sub(r"\W", "_", name).upper()


def _item_str(v):
    """数组配置项的值 → 平铺变量字符串。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v)
    if isinstance(v, dict):
        if v.get("type") == "keychain":
            return "keychain:%s/%s" % (v.get("service", "elk-log-analysis"), v.get("account", ""))
        if v.get("type") == "env":
            return "env:%s" % v.get("name", "")
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def expand_env_array(items, environ, label="ELK_ENVIRONMENTS"):
    """数组配置 → 等价的平铺变量。每项一个环境：{"env":"test","url":...,"username":...,"password":...,"index":...}。
    数组是环境层的唯一来源：同名环境的平铺 ELK_<ENV>_* 会被忽略；全局 ELK_<KEY> 仍作为默认值。"""
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError as e:
            raise ElkError("%s 不是合法 JSON：%s" % (label, e))
    if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
        raise ElkError("%s 必须是对象数组，如 [{\"env\":\"test\",\"url\":\"...\"}]" % label)
    synth = {k: v for k, v in environ.items() if k not in ("ELK_ENVS", "ELK_ENVIRONMENTS")}
    notes = {"source": label, "ignored": [], "unknown": [], "labels": {}, "prefixes": {}}
    if environ.get("ELK_ENVS"):
        notes["ignored"].append("ELK_ENVS")
    names, default = [], None
    for i, item in enumerate(items):
        name = item.get("env") or item.get("name")
        if not name:
            raise ElkError("%s[%d] 缺少 env（环境名）" % (label, i))
        if name in names:
            raise ElkError("%s 中环境 %s 重复" % (label, name))
        names.append(name)
        prefix = env_prefix(name)
        notes["prefixes"][prefix] = name
        for var in [k for k in synth if k.startswith(prefix) and k[len(prefix):] in ENV_KEYS]:
            notes["ignored"].append(var)
            del synth[var]
        for k, v in item.items():
            lk = k.lower()
            if lk in ("env", "name"):
                continue
            if lk == "default":
                default = name if v else default
                continue
            if lk.upper() not in ENV_KEYS:
                notes["unknown"].append("%s[%s].%s" % (label, name, k))
                continue
            if v is None or v == "":
                continue
            synth[prefix + lk.upper()] = _item_str(v)
            notes["labels"][prefix + lk.upper()] = "%s[%s].%s" % (label, name, lk)
    synth["ELK_ENVS"] = ",".join(names)
    if default and not environ.get("ELK_DEFAULT_ENV"):
        synth["ELK_DEFAULT_ENV"] = default
    return synth, notes


def config_from_environ(environ=None):
    """从环境变量（MCP 配置的 env 字段）构建分层配置；ELK_ENVIRONMENTS（数组）优先，其次 ELK_ENVS（平铺）；都没有返回 None。"""
    environ = os.environ if environ is None else environ
    if environ.get("ELK_ENVIRONMENTS"):
        synth, notes = expand_env_array(environ["ELK_ENVIRONMENTS"], environ)
        return _flat_config(synth, notes)
    if environ.get("ELK_ENVS"):
        return _flat_config(environ, {"source": "ELK_ENVS", "ignored": [], "unknown": [], "labels": {}})
    return None


def _flat_config(environ, notes):
    """从平铺变量构建分层配置（数组配置也先展开成平铺变量再走这里，行为完全一致）。

    系统：ELK_ENVS=stag,test,prod  ELK_DEFAULT_ENV=test  ELK_FIELDS={"trace":"traceId"}
    全局：ELK_<KEY>，KEY ∈ GLOBAL_KEYS（URL/MODE/INDEX/USERNAME/PASSWORD/API_KEY/TIMEOUT/MAX_SIZE/PROXY…）
    环境：ELK_<ENV>_<KEY>，KEY ∈ GLOBAL_KEYS ∪ ENV_ONLY_KEYS（TIER/ALIASES/PRODUCTION/FIELDS/BRANCH/SERVICE_MAP/ALIGN/AUTO_INDEX）
    环境级别预设见 TIER_PRESETS（按环境名推断，ELK_<ENV>_TIER 可显式指定）。
    """
    labels = notes.get("labels", {})

    def lbl(var):
        """变量名 → 用户实际写配置的位置（数组配置时指向 ELK_ENVIRONMENTS[<env>].<key>）。"""
        if var in labels:
            return labels[var]
        for p, n in notes.get("prefixes", {}).items():
            if var.startswith(p):
                return "%s[%s].%s" % (notes["source"], n, var[len(p):].lower())
        return var

    names = [n.strip() for n in environ.get("ELK_ENVS", "").split(",") if n.strip()]
    cfg = {"default_env": environ.get("ELK_DEFAULT_ENV") or names[0],
           "fields": _json(environ.get("ELK_FIELDS"), "ELK_FIELDS"),
           "environments": {},
           "_layers": {"source": notes.get("source"), "ignored": notes.get("ignored", []),
                       "unknown": notes.get("unknown", []),
                       "globals": {k: environ["ELK_" + k] for k in GLOBAL_KEYS if environ.get("ELK_" + k)},
                       "env_values": {}}}
    for name in names:
        prefix = env_prefix(name)
        src = {}
        cfg["_layers"]["env_values"][name] = {k: environ[prefix + k] for k in ENV_KEYS if environ.get(prefix + k)}

        def lookup(key, default=None, global_ok=True):
            """返回 (值, 变量名)，并记录来源层。"""
            for var, layer in ((prefix + key, "环境"), ("ELK_" + key, "全局")):
                if layer == "全局" and not global_ok:
                    continue
                v = environ.get(var)
                if v is not None and v != "":
                    src[key.lower()] = layer
                    return v, var
            return default, None

        def g(key, default=None, global_ok=True):
            return lookup(key, default, global_ok)[0]

        mode = g("MODE", "es").lower()
        if mode not in ("es", "kibana"):
            raise ElkError("%s（或 ELK_MODE）只能是 es 或 kibana" % lbl(prefix + "MODE"))
        url, url_var = lookup("URL")
        env = {
            "mode": mode,
            "index": g("INDEX", "*"),
            "verify_ssl": _bool(g("VERIFY_SSL"), True),
            "indices": _json(g("INDICES"), lbl(prefix + "INDICES")),
            "fields": _json(g("FIELDS", global_ok=False), lbl(prefix + "FIELDS")),
            "_url_var": lbl(url_var or prefix + "URL"),
            "_src": src,
        }
        if g("TIER", global_ok=False):
            env["tier"] = g("TIER", global_ok=False).lower()
        if g("ALIASES", global_ok=False):
            env["aliases"] = [a.strip() for a in g("ALIASES", global_ok=False).split(",") if a.strip()]
        if g("PRODUCTION", global_ok=False) is not None:
            env["production"] = _bool(g("PRODUCTION", global_ok=False))
        for key in ("BRANCH", "ALIGN"):
            if g(key, global_ok=False):
                env[key.lower()] = g(key, global_ok=False)
        if g("SERVICE_MAP", global_ok=False):
            env["service_map"] = _json(g("SERVICE_MAP", global_ok=False), lbl(prefix + "SERVICE_MAP"))
        if g("AUTO_INDEX", global_ok=False) is not None:
            env["auto_index"] = _bool(g("AUTO_INDEX", global_ok=False))
        if g("COLUMNS"):
            env["columns"] = [c.strip() for c in g("COLUMNS").split(",") if c.strip()]
        env["kibana_url" if mode == "kibana" else "es_url"] = url
        for key, conv in (("CA_CERT", str), ("KIBANA_SPACE", str), ("KIBANA_API", str), ("PROXY", str),
                          ("MAX_SIZE", int), ("TIMEOUT", int)):
            if g(key):
                env[key.lower()] = conv(g(key))
        if g("MASK") is not None:
            env["mask"] = _bool(g("MASK"))
        # 凭据：环境级 API_KEY > 用户名密码（环境/全局）> 全局 API_KEY
        user, user_var = lookup("USERNAME")
        if environ.get(prefix + "API_KEY") is not None:
            env["auth"] = {"type": "apikey", "api_key": _secret_from_env(g("API_KEY"), lbl(prefix + "API_KEY"))}
        elif user:
            pwd, pwd_var = lookup("PASSWORD")
            env["auth"] = {"type": "basic", "username": user,
                           "password": _secret_from_env(pwd, lbl(pwd_var or (
                               prefix if user_var.startswith(prefix) else "ELK_") + "PASSWORD"))}
        elif environ.get("ELK_API_KEY") is not None:
            src["api_key"] = "全局"
            env["auth"] = {"type": "apikey", "api_key": _secret_from_env(environ.get("ELK_API_KEY"), "ELK_API_KEY")}
        else:
            env["auth"] = {"type": "none"}
        cfg["environments"][name] = env
    return cfg


def apply_tiers(cfg):
    """补齐环境级别及其预设（只填未显式配置的项）。预设别名只在该级别唯一时生效，避免多个生产环境抢同一个别名。"""
    envs = cfg.get("environments", {})
    for name, env in envs.items():
        src = env.setdefault("_src", {})
        if env.get("tier"):
            src.setdefault("tier", "环境")
        else:
            env["tier"] = infer_tier(name)
            src["tier"] = "按名称推断" if env["tier"] else "未识别"
        if env.get("production") and not env["tier"]:
            env["tier"] = "prod"
            src["tier"] = "由 PRODUCTION 推断"
    counts = {}
    for env in envs.values():
        counts[env["tier"]] = counts.get(env["tier"], 0) + 1
    for name, env in envs.items():
        preset = TIER_PRESETS.get(env["tier"], {})
        src = env["_src"]
        for key, val in preset.items():
            if key == "aliases":
                if not env.get("aliases") and counts[env["tier"]] == 1:
                    env["aliases"] = list(val)
                    src["aliases"] = "级别预设"
                continue
            if key not in env:
                env[key] = val
                src[key] = "级别预设"
        if env["tier"] == "prod" and not env.get("production"):
            # 安全兜底：prod 级别一律按生产保护，PRODUCTION=false 不能绕过；确需非生产请显式设置 ELK_<ENV>_TIER
            env["production"] = True
            src["production"] = "级别强制（忽略 PRODUCTION=false）"
        env.setdefault("aliases", [])
        env.setdefault("production", False)
    return cfg


def config_from_ccswitch():
    """读取 cc-switch 中 elk MCP 条目的顶层 elk 对象（只读打开数据库）。

    env 的值只能是字符串（Claude Code 会拒绝对象/数组，cc-switch 同步 Codex 时会丢弃非字符串），
    所以环境配置放在条目顶层的 elk 字段：Claude Code / Codex 都忽略该字段，由本服务直接从 cc-switch 读取。
    没有数据库、条目或 elk 字段时返回 None。"""
    if not os.path.exists(CCSWITCH_DB):
        return None
    import sqlite3
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(CCSWITCH_DB), uri=True, timeout=5)
        try:
            row = conn.execute("SELECT server_config FROM mcp_servers WHERE id = ?", (CCSWITCH_ID,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise ElkError("读取 cc-switch 配置失败（%s）：%s" % (CCSWITCH_DB, e))
    if not row:
        return None
    try:
        server = json.loads(row[0])
    except ValueError as e:
        raise ElkError("cc-switch 中 %s 的配置不是合法 JSON：%s" % (CCSWITCH_ID, e))
    obj = server.get("elk") if isinstance(server, dict) else None
    if obj is None:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("environments"), list):
        raise ElkError("%s 必须是 JSON 对象，且 environments 为数组，如 "
                       "{\"default_env\":\"test\",\"environments\":[{\"env\":\"test\",\"url\":\"...\"}]}"
                       % CCSWITCH_LABEL)
    return obj


def _config_from_object(obj, label):
    """{default_env, fields, defaults, environments:[...]} 对象 → 分层配置。defaults 为全局层，
    每项的键与 ELK_ENVIRONMENTS 相同（env/url/index/username/password…）。"""
    defaults = obj.get("defaults") or {}
    base = {"ELK_" + k.upper(): _item_str(v) for k, v in defaults.items()
            if k.upper() in GLOBAL_KEYS and v not in (None, "")}
    if obj.get("fields"):
        base["ELK_FIELDS"] = json.dumps(obj["fields"])
    if obj.get("default_env"):
        base["ELK_DEFAULT_ENV"] = obj["default_env"]
    synth, notes = expand_env_array(obj["environments"], base, label=label + " environments")
    return apply_tiers(_flat_config(synth, notes))


def load_config():
    """配置来源按优先级取第一个存在的：MCP env（ELK_ENVIRONMENTS 数组 > ELK_ENVS 平铺）
    > cc-switch 中 elk MCP 条目的 elk 对象 > envs.json。"""
    cfg = config_from_environ()
    if cfg:
        return apply_tiers(cfg)
    obj = config_from_ccswitch()
    if obj is not None:
        return _config_from_object(obj, CCSWITCH_LABEL)
    if not os.path.exists(CONFIG_PATH):
        raise ElkError("未找到环境配置：请在 cc-switch 的 elk MCP 配置顶层添加 elk 对象"
                       "（{\"default_env\":\"test\",\"environments\":[{\"env\":\"test\",\"url\":\"...\"}]}），"
                       "或创建 %s（模板见 skill 目录 envs.example.json）。" % CONFIG_PATH)
    mode = os.stat(CONFIG_PATH).st_mode & 0o777
    if mode & 0o077 and os.name != "nt":  # Windows 权限由 ACL 控制，st_mode 恒为 666，无参考意义
        print("[elk] 警告: %s 权限为 %o，建议 chmod 600" % (CONFIG_PATH, mode), file=sys.stderr)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        text = f.read()
    if re.search(r'"type"\s*:\s*"inline"', text):
        raise ElkError("%s 中不允许 inline 凭据，请改为 keychain/env 引用" % CONFIG_PATH)
    cfg = json.loads(text)
    if isinstance(cfg.get("environments"), list):
        # 数组写法：与 ELK_ENVIRONMENTS 相同的简化键；defaults 为全局层；凭据只能用 keychain:/env: 引用
        for item in cfg["environments"] + [cfg.get("defaults") or {}]:
            for key in ("password", "api_key"):
                v = item.get(key)
                if isinstance(v, str) and v and not v.startswith(("keychain:", "env:")):
                    raise ElkError("%s 中 %s 不能写明文，请改为 keychain:<service>/<account> 或 env:<变量名>"
                                   % (CONFIG_PATH, key))
        return _config_from_object(cfg, CONFIG_PATH)
    # envs.json 同样分层：顶层 defaults 为全局，environments.<name> 覆盖之
    defaults = cfg.get("defaults") or {}
    for name, env in list(cfg.get("environments", {}).items()):
        merged = dict(defaults)
        merged.update(env)
        merged["_src"] = dict({k: "全局" for k in defaults}, **{k: "环境" for k in env})
        cfg["environments"][name] = merged
    return apply_tiers(cfg)


def get_env(cfg, name):
    envs = cfg.get("environments", {})
    name = name or os.environ.get("ELK_ENV") or cfg.get("default_env")
    if not name:
        raise ElkError("未指定环境，可选: " + ", ".join(envs))
    for key, env in envs.items():
        if name == key or name in env.get("aliases", []):
            env = dict(env)
            env["_name"] = key
            fields = dict(DEFAULT_FIELDS)
            fields.update(cfg.get("fields", {}))
            fields.update(env.get("fields", {}))
            env["_fields"] = fields
            return env
    raise ElkError("未知环境 '%s'，可选: %s" % (name, ", ".join(envs)))


def describe_envs(cfg, verbose=False):
    lines = []
    default = cfg.get("default_env")
    for name, env in cfg.get("environments", {}).items():
        target = env.get("kibana_url") if env.get("mode") == "kibana" else env.get("es_url")
        tags = []
        if name == default:
            tags.append("default")
        if env.get("production"):
            tags.append("PROD")
        aliases = ",".join(env.get("aliases", []))
        auth = env.get("auth", {})
        ref = auth.get("password") or auth.get("api_key") or {}
        cred = {"inline": "已配置", "missing": "未配置", "keychain": "钥匙串",
                "env": "环境变量"}.get(ref.get("type") if isinstance(ref, dict) else "", "-")
        lines.append("%-8s %-7s %-40s index=%s auth=%s(%s) tier=%s branch=%s aliases=%s %s" % (
            name, env.get("mode", "es"), target or "(未配置地址)", env.get("index", "*"),
            auth.get("type", "none"), cred, env.get("tier") or "-", env.get("branch") or "-",
            aliases or "-", " ".join(tags)))
        for alias, real in env.get("indices", {}).items():
            lines.append("         index-alias %s -> %s" % (alias, real))
        if verbose:
            src = env.get("_src", {})
            keys = sorted(k for k in src if k not in ("password", "api_key"))
            lines.append("         来源: " + ", ".join("%s=%s" % (k, src[k]) for k in keys))
    if verbose:
        lines.append("（配置来源：%s；优先级：环境 > 全局 ELK_<KEY> > 级别预设 > 内置默认；凭据只显示是否配置）"
                     % (cfg.get("_layers", {}).get("source") or "envs.json"))
    return "\n".join(lines)


def resolve_secret(ref, env_name, label, interactive=True):
    """把配置里的凭据引用解析成真实值。值只在内存中使用，从不输出。"""
    if not ref:
        return None
    if isinstance(ref, str):
        raise ElkError("环境 %s 的 %s 是明文字符串，已拒绝。请改为 keychain/env/prompt 引用。"
                       % (env_name, label))
    kind = ref.get("type")
    if kind == "inline":
        return ref["value"]
    if kind == "missing":
        raise ElkError("环境 %s 的 %s 未配置，请在 MCP 配置的 env 中填写 %s"
                       % (env_name, label, ref["name"]))
    if kind == "env":
        val = os.environ.get(ref["name"])
        if not val:
            raise ElkError("环境变量 %s 未设置（%s.%s）" % (ref["name"], env_name, label))
        return val
    if kind == "keychain":
        service = ref.get("service", "elk-log-analysis")
        account = ref.get("account", env_name)
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                capture_output=True, text=True, check=True)
            return out.stdout.rstrip("\n")
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise ElkError("钥匙串中未找到 service=%s account=%s。请用户自行在终端执行：\n"
                           "  security add-generic-password -U -s %s -a %s -w"
                           % (service, account, service, account))
    if kind == "prompt":
        if not interactive or not sys.stdin.isatty():
            raise ElkError("%s.%s 配置为交互输入，当前运行方式不支持（MCP 下请改用 keychain/env）"
                           % (env_name, label))
        return getpass.getpass("%s %s: " % (env_name, label))
    raise ElkError("不支持的凭据类型: %s" % kind)


def auth_header(env, interactive=True):
    auth = env.get("auth", {})
    kind = auth.get("type", "none")
    if kind == "none":
        return {}
    if kind == "basic":
        user = auth.get("username")
        if isinstance(user, dict):
            user = resolve_secret(user, env["_name"], "username", interactive)
        pwd = resolve_secret(auth.get("password"), env["_name"], "password", interactive)
        token = base64.b64encode(("%s:%s" % (user, pwd)).encode()).decode()
        return {"Authorization": "Basic " + token}
    if kind == "apikey":
        key = resolve_secret(auth.get("api_key"), env["_name"], "api_key", interactive)
        return {"Authorization": "ApiKey " + key}
    raise ElkError("不支持的认证类型: %s" % kind)


# ---------------------------------------------------------------- http

# Kibana 模式下 Console 代理被拒（账号无 Dev Tools 权限）的环境，后续直接走搜索接口
_CONSOLE_DENIED = set()


def _http(env, url, headers, data, method):
    ctx = None
    if url.startswith("https"):
        ctx = ssl.create_default_context(cafile=env.get("ca_cert"))
        if env.get("verify_ssl", True) is False:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # 默认直连（走内部 DNS），忽略 HTTP(S)_PROXY 与系统代理；需要代理时在环境里配置 proxy
    proxy = env.get("proxy")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}),
        urllib.request.HTTPSHandler(context=ctx))
    try:
        with opener.open(req, timeout=env.get("timeout", 60)) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:1500]
        err = ElkError("HTTP %s %s\n%s" % (e.code, e.reason, detail))
        err.status = e.code
        raise err
    except urllib.error.URLError as e:
        raise ElkError("连接失败: %s（检查 VPN/域名/证书）" % e.reason)
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def call(env, method, path, body=None, interactive=True):
    method = method.upper()
    if method not in ("GET", "POST") or not READONLY_API.match(path.split("?")[0]):
        raise ElkError("拒绝调用非只读 API: %s %s" % (method, path))
    target = env.get("kibana_url") if env.get("mode") == "kibana" else env.get("es_url")
    if not target:
        raise ElkError("环境 %s 未配置地址（%s）" % (env["_name"], env.get("_url_var", "es_url/kibana_url")))
    headers = {"Content-Type": "application/json"}
    headers.update(auth_header(env, interactive))
    headers.update(env.get("headers", {}))

    if env.get("mode", "es") != "kibana":
        url = env["es_url"].rstrip("/") + "/" + path.lstrip("/")
        data = json.dumps(body).encode() if body is not None else None
        return _http(env, url, headers, data, method)

    headers["kbn-xsrf"] = "true"
    space = env.get("kibana_space")
    base = env["kibana_url"].rstrip("/") + ("/s/%s" % space if space else "")
    # kibana_api: auto（默认，Console 403 时改走 Discover 搜索接口）| console | search
    api = env.get("kibana_api", "auto")
    if api == "console" or (api == "auto" and env["_name"] not in _CONSOLE_DENIED):
        url = "%s/api/console/proxy?%s" % (
            base, urllib.parse.urlencode({"path": path, "method": method}))
        data = json.dumps(body).encode() if body is not None else None
        try:
            return _http(env, url, headers, data, "POST")
        except ElkError as e:
            if api == "console" or getattr(e, "status", None) != 403:
                raise
            _CONSOLE_DENIED.add(env["_name"])
    return _kibana_search_api(env, base, headers, path, body)


def _kibana_search(env, base, headers, index, body):
    """走 Discover 同款的 /internal/search/es，只需索引 read 权限，不需要 Dev Tools 权限。"""
    h = dict(headers)
    h["elastic-api-version"] = "1"
    h["x-elastic-internal-origin"] = "Kibana"
    payload = {"params": {"index": index, "body": body or {}}}
    res = _http(env, base + "/internal/search/es", h, json.dumps(payload).encode(), "POST")
    if isinstance(res, dict) and "rawResponse" in res:
        return res["rawResponse"]
    return res


def _kibana_search_api(env, base, headers, path, body):
    """无 Console 权限时，把只读 ES API 映射到 Kibana 搜索/状态接口。"""
    p = path.split("?")[0].strip("/")
    if p == "":
        res = _http(env, base.split("/s/")[0] + "/api/status", headers, None, "GET")
        return {"cluster_name": "(kibana %s, 搜索接口)" % res.get("name"),
                "version": res.get("version", {})}
    idx, _, api = p.rpartition("/")
    if api == "_search":
        return _kibana_search(env, base, headers, idx, body)
    if api == "_count":
        q = {"size": 0, "track_total_hits": True}
        if body and "query" in body:
            q["query"] = body["query"]
        return {"count": _total(_kibana_search(env, base, headers, idx, q))}
    if api == "_field_caps":
        pat = urllib.parse.urlencode({"pattern": idx})
        for route in ("/api/index_patterns/_fields_for_wildcard", "/internal/data_views/_fields_for_wildcard"):
            try:
                res = _http(env, "%s%s?%s" % (base, route, pat), headers, None, "GET")
                break
            except ElkError as e:
                if getattr(e, "status", None) not in (400, 404):
                    raise
        else:
            raise ElkError("Kibana 未提供字段查询接口")
        return {"fields": {f["name"]: {t: {} for t in (f.get("esTypes") or [f.get("type")])}
                           for f in res.get("fields", [])}}
    m = re.match(r"^_cat/indices(?:/(.+))?$", p)
    if m:
        q = {"size": 0, "aggs": {"idx": {"terms": {"field": "_index", "size": 500,
                                                  "order": {"_key": "desc"}}}}}
        res = _kibana_search(env, base, headers, m.group(1) or "*", q)
        return [{"index": b["key"], "docs.count": b["doc_count"], "store.size": "-",
                 "health": "-"} for b in res.get("aggregations", {}).get("idx", {}).get("buckets", [])]
    raise ElkError("账号无 Kibana Dev Tools 权限，搜索接口不支持该 API: %s" % path)


# ---------------------------------------------------------------- query build

REL_TIME = re.compile(r"^(\d+)([smhdw])$")


def parse_tz(s):
    m = re.match(r"^([+-])(\d{2}):?(\d{2})$", s or "+08:00")
    if not m:
        raise ElkError("时区格式应为 +08:00")
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return timezone(delta if m.group(1) == "+" else -delta)


def parse_time(value, tz):
    if value in (None, "", "now"):
        return "now"
    m = REL_TIME.match(value)
    if m:
        return "now-%s%s" % (m.group(1), m.group(2))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt).replace(tzinfo=tz)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except ValueError:
            continue
    return value  # 原样透传（ES date math 或带时区 ISO）


def build_query(env, p, tz):
    """p: dict，键 query/since/until/level/service/trace/host/logger/terms。"""
    f = env["_fields"]
    must, filt = [], []
    filt.append({"range": {f["timestamp"]: {
        "gte": parse_time(p.get("since") or "15m", tz),
        "lte": parse_time(p.get("until") or "now", tz)}}})
    if p.get("query"):
        must.append({"query_string": {
            "query": p["query"], "default_field": f["message"],
            "analyze_wildcard": True, "default_operator": "AND"}})
    for k in ("level", "service", "trace", "host", "logger"):
        v = p.get(k)
        if v:
            values = [x.strip() for x in str(v).split(",") if x.strip()]
            filt.append({"bool": {"should": [
                {"match_phrase": {f[k]: x}} for x in values], "minimum_should_match": 1}})
    for k, v in (p.get("terms") or {}).items():
        filt.append({"match_phrase": {k: v}})
    return {"bool": {"must": must or [{"match_all": {}}], "filter": filt}}


def resolve_index(env, name=None):
    """把索引别名解析为真实索引；支持逗号分隔多个，未知名称原样透传。"""
    if not name:
        return env.get("index", "*")
    aliases = env.get("indices", {})
    parts = [aliases.get(x.strip(), x.strip()) for x in str(name).split(",") if x.strip()]
    return ",".join(parts)


def index_of(env, p):
    return resolve_index(env, p.get("index"))


# ---------------------------------------------------------------- output

def dig(src, dotted):
    if dotted in src:
        return src[dotted]
    cur = src
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def fmt_ts(value, tz):
    if not value:
        return "-"
    try:
        dt = datetime.strptime(value[:23].rstrip("Z"), "%Y-%m-%dT%H:%M:%S.%f")
        dt = dt.replace(tzinfo=timezone.utc).astimezone(tz)
        return dt.strftime("%m-%d %H:%M:%S.") + "%03d" % (dt.microsecond // 1000)
    except ValueError:
        return str(value)


def mask(env, text):
    if not env.get("mask", bool(env.get("production"))):
        return text
    for pattern, repl in MASK_RULES:
        text = pattern.sub(repl, text)
    return text


def format_hits(env, hits, tz, full=False, width=300, show_source=False, columns=None):
    """按 columns 逐列展示（字段逻辑名或原始字段名）；message/时间/级别不加括号。"""
    f = env["_fields"]
    if isinstance(columns, str):
        columns = [c.strip() for c in columns.split(",") if c.strip()]
    cols = [f.get(c, c) for c in (columns or env.get("columns") or DEFAULT_COLUMNS)]
    time_cols = {f["time"], f["time"] + ".keyword", f["timestamp"]}
    out = []
    for h in hits:
        s = h.get("_source", {})
        parts = []
        for c in cols:
            v = dig(s, c)
            if v is None and c.endswith(".keyword"):
                v = dig(s, c[:-len(".keyword")])  # keyword 子字段不在 _source 中
            if c in time_cols:
                parts.append(str(v or fmt_ts(dig(s, f["timestamp"]), tz)))
            elif c == f["level"]:
                parts.append("%-5s" % (v or "-"))
            elif c == f["message"]:
                msg = str(v or "")
                if not full and width > 0:
                    msg = msg.replace("\n", " ⏎ ")[:width]
                parts.append(msg)
            else:
                parts.append("[%s]" % ("-" if v in (None, "") else v))
        out.append(" ".join(parts))
        if full:
            stack = dig(s, f["stack"])
            if stack:
                out.append("    " + str(stack).replace("\n", "\n    "))
            if show_source:
                out.append("    _index=%s _id=%s" % (h.get("_index"), h.get("_id")))
    return mask(env, "\n".join(out))


def _total(res):
    t = res.get("hits", {}).get("total", {})
    return t.get("value") if isinstance(t, dict) else t


# ---------------------------------------------------------------- operations

def op_ping(env, interactive=True):
    try:
        res = call(env, "GET", "/", interactive=interactive)
    except ElkError as e:
        if getattr(e, "status", None) != 403:
            raise
        # 账号无集群/Kibana 状态接口权限（常见）：改用一次最小检索探测连通性与索引读权限
        res = call(env, "POST", "/%s/_search" % resolve_index(env),
                   {"size": 0, "track_total_hits": False, "query": {"match_all": {}}}, interactive=interactive)
        _healed("ping_403_fallback", env["_name"])
        return "ok: env=%s（状态接口 403，已改用检索探测；took=%sms，索引可读）" % (
            env["_name"], res.get("took") if isinstance(res, dict) else "?")
    if isinstance(res, dict):
        return "ok: env=%s cluster=%s version=%s" % (
            env["_name"], res.get("cluster_name"), res.get("version", {}).get("number"))
    return str(res)


def op_indices(env, pattern=None, limit=50, interactive=True):
    pattern = resolve_index(env, pattern)
    res = call(env, "GET", "/_cat/indices/%s?format=json&h=index,docs.count,store.size,health"
               "&s=index:desc" % pattern, interactive=interactive)
    return "\n".join("%-60s docs=%-12s size=%-10s %s" % (
        r.get("index"), r.get("docs.count"), r.get("store.size"), r.get("health"))
        for r in (res or [])[:limit])


def op_fields(env, index=None, grep=None, interactive=True):
    res = call(env, "GET", "/%s/_field_caps?fields=*" % resolve_index(env, index),
               interactive=interactive)
    kw = grep.lower() if grep else None
    lines = []
    for n in sorted(k for k in res.get("fields", {}) if not k.startswith("_")):
        if kw and kw not in n.lower():
            continue
        lines.append("%-50s %s" % (n, ",".join(res["fields"][n].keys())))
    return "\n".join(lines)


def search_body(env, p, tz):
    f = env["_fields"]
    size = min(int(p.get("size") or 50), int(env.get("max_size", 500)))
    return {
        "size": size,
        "track_total_hits": True,
        "sort": [{f["sort"]: {"order": "desc" if p.get("desc") else "asc",
                              "missing": "_last", "unmapped_type": "keyword"}}],
        "query": build_query(env, p, tz),
    }


def op_search(env, p, tz, interactive=True):
    body = search_body(env, p, tz)
    idx = index_of(env, p)
    res = call(env, "POST", "/%s/_search" % idx, body, interactive=interactive)
    hits = res.get("hits", {}).get("hits", [])
    head = "# env=%s index=%s total=%s shown=%d took=%sms" % (
        env["_name"], idx, _total(res), len(hits), res.get("took"))
    if p.get("json"):
        return head + "\n" + mask(env, json.dumps(
            [h.get("_source") for h in hits], ensure_ascii=False, indent=2))
    # 降序时取的是最新 N 条，展示时翻转回升序，便于按时间线阅读
    ordered = list(reversed(hits)) if p.get("desc") else hits
    return head + "\n" + format_hits(env, ordered, tz, p.get("full"),
                                     int(p.get("width", 300)), p.get("show_source"),
                                     p.get("columns"))


def agg_body(env, p, tz):
    f = env["_fields"]
    field = f.get(p.get("field") or "", p.get("field") or "")
    top = int(p.get("top") or 20)
    if p.get("interval"):
        aggs = {"trend": {"date_histogram": {
            "field": f["timestamp"], "fixed_interval": p["interval"],
            "time_zone": p.get("tz", "+08:00"), "min_doc_count": 0}}}
        if field:
            aggs["trend"]["aggs"] = {"top": {"terms": {"field": field, "size": top}}}
    elif field:
        aggs = {"top": {"terms": {"field": field, "size": top}}}
    else:
        raise ElkError("agg 需要 field 或 interval")
    return {"size": 0, "track_total_hits": True, "query": build_query(env, p, tz), "aggs": aggs}


def op_agg(env, p, tz, interactive=True):
    body = agg_body(env, p, tz)
    note = None
    try:
        res = call(env, "POST", "/%s/_search" % index_of(env, p), body, interactive=interactive)
    except ElkError as e:
        field = env["_fields"].get(p.get("field") or "", p.get("field") or "")
        if getattr(e, "status", None) != 400 or not field or field.endswith(".keyword") \
                or not TEXT_FIELD_ERR.search(str(e)):
            raise
        # text 字段不能聚合：自动改用 keyword 子字段重试
        p = dict(p, field=field + ".keyword")
        res = call(env, "POST", "/%s/_search" % index_of(env, p), agg_body(env, p, tz), interactive=interactive)
        note = "# 注: %s 是 text 字段，已自动改用 %s.keyword" % (field, field)
        _healed("agg_text_field_keyword", field)
    lines = ["# env=%s total=%s" % (env["_name"], _total(res))]
    if note:
        lines.append(note)
    aggs = res.get("aggregations", {})
    if "trend" in aggs:
        for b in aggs["trend"]["buckets"]:
            extra = ""
            if "top" in b:
                extra = "  " + ", ".join("%s=%d" % (x["key"], x["doc_count"])
                                          for x in b["top"]["buckets"])
            lines.append("%s %8d%s" % (b.get("key_as_string"), b["doc_count"], extra))
    else:
        for b in aggs.get("top", {}).get("buckets", []):
            lines.append("%8d  %s" % (b["doc_count"], b["key"]))
    return mask(env, "\n".join(lines))


def op_raw(env, method, path, body=None, interactive=True):
    res = call(env, method, path, body, interactive=interactive)
    text = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, indent=2)
    return mask(env, text)
