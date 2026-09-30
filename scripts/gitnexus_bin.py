"""定位 gitnexus 及运行它的 node。供 code_core / repo_sync / mcp_http / diag / install.py 共用，仅标准库。

MCP 进程常由 GUI 客户端启动，PATH 里没有用户在终端配置的 nvm / fnm / volta 等目录，直接 which 经常找不到。

ELK_CODE_GITNEXUS 可以是（不设则自动检测）：
  - gitnexus 可执行文件：…/bin/gitnexus、…\\npm\\gitnexus.cmd
  - 安装目录：npm 全局前缀（含 bin/ 或 lib/node_modules/）、bin 目录、或 gitnexus 包目录（…/node_modules/gitnexus）
ELK_CODE_NODE 指定 node（不设则优先用与 gitnexus 同目录的 node，再按版本要求检测）。

自动检测顺序：上次检测的缓存（文件仍存在即用）→ 当前 PATH → 常见安装位置（Homebrew、npm 全局、nvm、fnm、volta、
pnpm、asdf、mise、Windows 的 %APPDATA%\\npm 等）→ npm 全局目录（npm prefix -g）→ 登录 shell 的 PATH。

运行方式：能找到 gitnexus 包入口时用 `<node> <包>/dist/cli/index.js`，显式指定 node，不依赖 PATH 与 shebang，
Windows 上也不经过 .cmd；找不到包入口（如 volta/asdf 的 shim）时直接运行可执行文件，并把 node 所在目录加入 PATH。
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

WINDOWS = os.name == "nt"
EXE_NAMES = ("gitnexus.cmd", "gitnexus.exe", "gitnexus") if WINDOWS else ("gitnexus",)
NODE_NAMES = ("node.exe",) if WINDOWS else ("node",)
DEFAULT_ENGINES = "^22.18.0 || >=24.11.0"  # 读不到 package.json 时使用（gitnexus 1.6.x 的要求）
CACHE_FILE = "gitnexus.json"


def _home(*p):
    return os.path.join(os.path.expanduser("~"), *p)


def _version_key(path):
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)", path)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def common_dirs():
    """各平台常见的 gitnexus / node 所在目录（存在的才返回）。版本管理器按版本从新到旧。"""
    if WINDOWS:
        env = os.environ
        dirs = [os.path.join(env.get("APPDATA", ""), "npm"),
                os.path.join(env.get("LOCALAPPDATA", ""), "pnpm"),
                os.path.join(env.get("LOCALAPPDATA", ""), "Volta", "bin"),
                env.get("NVM_SYMLINK", ""),
                os.path.join(env.get("ProgramFiles", r"C:\Program Files"), "nodejs"),
                _home("scoop", "shims")]
        dirs += sorted(glob.glob(os.path.join(env.get("NVM_HOME", ""), "v*")), key=_version_key, reverse=True) \
            if env.get("NVM_HOME") else []
    else:
        dirs = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", _home(".npm-global", "bin"), _home(".local", "bin"),
                _home(".volta", "bin"), _home(".bun", "bin"), _home("Library", "pnpm"), _home(".local", "share", "pnpm"),
                _home(".asdf", "shims"), _home(".local", "share", "mise", "shims")]
        for pattern in (_home(".nvm", "versions", "node", "*", "bin"),
                        _home(".local", "share", "fnm", "node-versions", "*", "installation", "bin"),
                        _home("Library", "Application Support", "fnm", "node-versions", "*", "installation", "bin"),
                        "/usr/local/n/versions/node/*/bin"):
            dirs += sorted(glob.glob(pattern), key=_version_key, reverse=True)
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


# ---------------------------------------------------------------- gitnexus 包与入口

def _read_pkg(pkg):
    try:
        with open(os.path.join(pkg, "package.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if data.get("name") != "gitnexus":
        return None
    bin_ = data.get("bin")
    entry = bin_.get("gitnexus") if isinstance(bin_, dict) else bin_
    entry = os.path.join(pkg, entry or "dist/cli/index.js")
    return {"pkg": pkg, "entry": entry if os.path.isfile(entry) else None, "version": data.get("version", ""),
            "engines": (data.get("engines") or {}).get("node") or DEFAULT_ENGINES}


def _pkg_for(launcher):
    """由可执行文件推出 gitnexus 包目录：符号链接指向 dist/cli/index.js（npm/Homebrew/nvm），
    或按 npm 的目录约定（Unix: <prefix>/lib/node_modules，Windows: <prefix>\\node_modules）。"""
    real = os.path.realpath(launcher)
    cands = []
    parts = real.replace("\\", "/").split("/node_modules/gitnexus/")
    if len(parts) == 2:
        cands.append(parts[0] + "/node_modules/gitnexus")
    d = os.path.dirname(launcher)
    cands += [os.path.join(d, "node_modules", "gitnexus"), os.path.join(d, "..", "lib", "node_modules", "gitnexus"),
              os.path.join(os.path.dirname(real), "..", "lib", "node_modules", "gitnexus")]
    for c in cands:
        info = _read_pkg(os.path.normpath(c))
        if info:
            return info
    return None


def _launchers_in(path):
    """用户给的路径（文件或目录）里可能的 gitnexus 可执行文件 / 包目录。"""
    path = os.path.expanduser(path)
    if os.path.isfile(path):
        return [path], None
    if not os.path.isdir(path):
        return [], None
    pkg = _read_pkg(path) or _read_pkg(os.path.join(path, "node_modules", "gitnexus")) \
        or _read_pkg(os.path.join(path, "lib", "node_modules", "gitnexus"))
    files = [os.path.join(path, sub, n) for sub in ("", "bin", os.path.join("node_modules", ".bin")) for n in EXE_NAMES]
    return [f for f in files if os.path.isfile(f)], pkg


# ---------------------------------------------------------------- node

def _node_version(node):
    try:
        r = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", r.stdout.strip()) if r.returncode == 0 else None
    return tuple(int(x) for x in m.groups()) if m else None


def satisfies(ver, engines):
    """只支持 gitnexus 实际使用的写法：用 || 连接的 ^X.Y.Z 与 >=X.Y.Z。无法解析时视为满足。"""
    if not ver:
        return False
    ok_any, parsed = False, False
    for part in (engines or "").split("||"):
        m = re.match(r"\s*(\^|>=)\s*v?(\d+)\.(\d+)\.(\d+)\s*$", part)
        if not m:
            continue
        parsed = True
        want = tuple(int(x) for x in m.groups()[1:])
        if m.group(1) == ">=" and ver >= want:
            ok_any = True
        if m.group(1) == "^" and ver[0] == want[0] and ver >= want:
            ok_any = True
    return ok_any or not parsed


def _fmt(ver):
    return "v%d.%d.%d" % ver if ver else "?"


def find_node(near, engines, explicit=None, extra=()):
    """返回 (node 路径, 版本, 说明)。优先：显式指定 → 与 gitnexus 同目录（nvm/Homebrew 装的全局包用的就是它）
    → PATH → 常见位置 → 登录 shell；选第一个满足 engines 的，都不满足时返回找到的第一个并说明。"""
    cands = []
    if explicit:
        cands.append(os.path.expanduser(explicit))
    for d in near:
        cands += [os.path.join(d, n) for n in NODE_NAMES]
    w = shutil.which("node")
    if w:
        cands.append(w)
    for d in common_dirs():
        cands += [os.path.join(d, n) for n in NODE_NAMES]
    cands += list(extra)
    first, seen = None, set()
    for c in cands:
        real = os.path.realpath(c)
        if real in seen or not os.path.isfile(c):
            continue
        seen.add(real)
        ver = _node_version(c)
        if not ver:
            continue
        first = first or (c, ver)
        if satisfies(ver, engines):
            return c, ver, None
    if first:
        return first[0], first[1], "node %s 不满足 gitnexus 的要求（%s）" % (_fmt(first[1]), engines)
    return None, None, "未找到 node"


# ---------------------------------------------------------------- 登录 shell / npm prefix

def login_shell_which():
    """GUI 进程拿不到用户终端里的 PATH（nvm 等常在 .zshrc/.bashrc 中初始化）：用登录交互 shell 查一次。"""
    if WINDOWS:
        return None, None
    shell = os.environ.get("SHELL") or ("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")
    script = 'printf "__ELK__%s|%s\\n" "$(command -v gitnexus)" "$(command -v node)"'
    try:
        r = subprocess.run([shell, "-lic", script], capture_output=True, text=True, timeout=10,
                           stdin=subprocess.DEVNULL, env=dict(os.environ, TERM="dumb"))
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    m = re.search(r"__ELK__([^|\n]*)\|([^\n]*)", r.stdout)
    if not m:
        return None, None
    return (m.group(1).strip() or None), (m.group(2).strip() or None)


def npm_global_dirs(node=None):
    npm = shutil.which("npm") or (node and next((os.path.join(os.path.dirname(node), n)
                                                  for n in ("npm.cmd", "npm") if
                                                  os.path.isfile(os.path.join(os.path.dirname(node), n))), None))
    if not npm:
        return []
    try:
        r = subprocess.run([npm, "prefix", "-g"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    prefix = r.stdout.strip()
    return [prefix, os.path.join(prefix, "bin")] if r.returncode == 0 and prefix else []


# ---------------------------------------------------------------- 定位

def _build(launcher, pkg, source, node_hint):
    pkg = pkg or (_pkg_for(launcher) if launcher else None)
    engines = (pkg or {}).get("engines") or DEFAULT_ENGINES
    near = []
    if launcher:
        near += [os.path.dirname(launcher), os.path.dirname(os.path.realpath(launcher))]
    if pkg:  # <prefix>/lib/node_modules/gitnexus → <prefix>/bin；Windows <prefix>\node_modules\gitnexus → <prefix>
        near += [os.path.normpath(os.path.join(pkg["pkg"], "..", "..", "..", "bin")),
                 os.path.normpath(os.path.join(pkg["pkg"], "..", ".."))]
    node, ver, warn = find_node(near, engines, node_hint)
    if pkg and pkg["entry"] and node:
        argv = [node, pkg["entry"]]
    elif launcher:
        argv = [launcher]
    else:
        return None
    return {"launcher": launcher or pkg["entry"], "pkg": pkg["pkg"] if pkg else None, "argv": argv,
            "version": (pkg or {}).get("version", ""), "node": node, "node_version": _fmt(ver) if ver else None,
            "warning": warn, "source": source, "error": None}


def _try_paths(paths, source, node_hint):
    for p in paths:
        launchers, pkg = _launchers_in(p)
        for launcher in launchers or ([None] if pkg else []):
            info = _build(launcher, pkg, source, node_hint)
            if info:
                return info
    return None


def locate(spec=None, node_hint=None, cache_dir=None, refresh=False):
    """返回 gitnexus 信息 dict：argv（运行前缀）、launcher、version、node、source、warning；找不到时 error 非空。"""
    spec = spec if spec is not None else os.environ.get("ELK_CODE_GITNEXUS")
    node_hint = node_hint if node_hint is not None else os.environ.get("ELK_CODE_NODE")
    bad_spec = None
    if spec:
        info = _try_paths([spec], "ELK_CODE_GITNEXUS", node_hint)
        if info:
            return info
        # 常见原因：安装脚本记录的是某个 nvm 版本下的路径，之后切换/卸载了该版本。改用自动检测并提示
        bad_spec = "ELK_CODE_GITNEXUS（%s）下没有找到 gitnexus" % spec
    info = _auto(node_hint, cache_dir, refresh)
    if bad_spec:
        if info.get("error"):
            info["error"] = bad_spec + "，自动检测也未找到：请指向 gitnexus 可执行文件、npm 全局目录或 gitnexus 包目录"
        else:
            info["warning"] = "；".join(filter(None, [bad_spec + "，已改用自动检测到的位置，请更新该配置",
                                                     info.get("warning")]))
    return info


def _auto(node_hint, cache_dir, refresh):
    cache = os.path.join(cache_dir, CACHE_FILE) if cache_dir else None
    if cache and not refresh:
        try:
            with open(cache, encoding="utf-8") as f:
                hit = json.load(f)
            if all(os.path.exists(a) for a in hit["argv"]) and (not hit.get("node") or os.path.exists(hit["node"])):
                hit["source"] = "缓存（%s）" % hit.get("detected_by", "自动检测")
                return hit
        except (OSError, ValueError, KeyError, TypeError):
            pass
    info = None
    w = shutil.which("gitnexus")
    if w:
        info = _try_paths([w], "PATH", node_hint)
    info = info or _try_paths(common_dirs(), "常见安装位置", node_hint)
    if not info:
        info = _try_paths(npm_global_dirs(), "npm 全局目录", node_hint)
    if not info:
        gn, node = login_shell_which()
        if gn:
            info = _try_paths([gn], "登录 shell 的 PATH", node_hint or node)
    if not info:
        return {"error": "未找到 gitnexus：请安装（npm i -g gitnexus），或用 ELK_CODE_GITNEXUS 指定可执行文件 / 安装目录",
                "argv": None, "source": None}
    if cache:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(dict(info, detected_by=info["source"], checked=int(time.time())), f, ensure_ascii=False)
        except OSError:
            pass
    return info


def run_env(info, base=None):
    """运行 gitnexus 的环境：把 node 与 gitnexus 所在目录放到 PATH 最前（直接运行可执行文件时 shebang 要能找到 node）。"""
    env = dict(os.environ if base is None else base)
    dirs = []
    for p in (info.get("node"), info.get("launcher")):
        if p:
            dirs += [os.path.dirname(p), os.path.dirname(os.path.realpath(p))]
    env["PATH"] = os.pathsep.join([d for i, d in enumerate(dirs) if d and d not in dirs[:i]] + [env.get("PATH", "")])
    return env


def describe(info):
    if info.get("error"):
        return info["error"]
    s = "gitnexus %s（%s，来源：%s）" % (info.get("version") or "", info["launcher"], info["source"])
    if info.get("node"):
        s += "，node %s（%s）" % (info.get("node_version"), info["node"])
    if info.get("warning"):
        s += "；⚠ %s" % info["warning"]
    return s
