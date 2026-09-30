#!/usr/bin/env python3
"""elk-log-analysis 安装脚本（macOS / Linux / Windows，仅标准库，Python 3.8+）。

clone 仓库后运行，不带参数时逐项询问：
  macOS / Linux:  ./install.sh
  Windows:        powershell -ExecutionPolicy Bypass -File install.ps1
也可直接 python3 install.py [选项]；重复运行即更新，--uninstall 卸载。

做三件事：
1. 安装 skill（SKILL.md、LICENSE、references/、scripts/）到各客户端的 skills 目录：
   Claude Code ~/.claude/skills/elk-log-analysis；检测到 Codex 时另装 ~/.codex/skills/elk-log-analysis
2. 在 Claude Code（~/.claude.json）/ Codex（~/.codex/config.toml）中注册 elk MCP：
   --mode local   本机 stdio 运行 scripts/mcp_server.py，直接连 ELK（环境配置见 references/mcp-config.md）
   --mode remote  连接团队部署的 HTTP 服务（--url + 个人 token）；服务端开启了 GitNexus 转发时，
                  同时注册 gitnexus-remote（查询服务端预建的调用链/影响面索引）
   本地模式会在当前终端中检测 gitnexus 与 node（终端的 PATH 最完整），把路径写进 MCP 配置，
   这样 GUI 客户端启动 MCP 时也能找到；也可用 --gitnexus / --node 手动指定
3. 权限：skill 目录 755/644；含凭据的文件（客户端配置、envs.json、备份）只有当前用户可读写
   （Unix 600/700；Windows 去掉继承的 ACL，只授权当前用户与 SYSTEM）
"""
import argparse
import getpass
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

if sys.version_info < (3, 8):
    sys.exit("需要 Python 3.8+，当前为 %s" % sys.version.split()[0])

WINDOWS = os.name == "nt"
if WINDOWS:  # 控制台编码不是 UTF-8 时，中文输出不因编码错误中断
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass

SRC = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SRC, "scripts"))
import gitnexus_bin  # noqa: E402  仅标准库，与 MCP 运行时使用同一套检测逻辑
HOME = os.path.expanduser("~")
SKILL = "elk-log-analysis"
PAYLOAD = ("SKILL.md", "LICENSE", "references", "scripts")
MARKER = ".elk-install.json"
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
CODEX_DIR = os.path.join(HOME, ".codex")
CODEX_TOML = os.path.join(CODEX_DIR, "config.toml")
CONFIG_DIR = os.path.join(HOME, ".config", SKILL)  # 与 elk_core.CONFIG_PATH 的默认位置一致
BACKUP_DIR = os.path.join(CONFIG_DIR, "backups")
CCSWITCH_DB = os.path.join(HOME, ".cc-switch", "cc-switch.db")
CLIENTS = {
    "claude": {"label": "Claude Code", "home": os.path.join(HOME, ".claude"), "bin": "claude"},
    "codex": {"label": "Codex", "home": CODEX_DIR, "bin": "codex"},
}
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
DRY = False
ASSUME_YES = False


def say(msg=""):
    print(msg, flush=True)


def step(msg):
    say("\n== " + msg)


def interactive():
    return not ASSUME_YES and sys.stdin.isatty()


def ask(prompt, default=""):
    if not interactive():
        return default
    v = input("%s%s: " % (prompt, " [%s]" % default if default else "")).strip()
    return v or default


def confirm(prompt, default=True):
    if not interactive():
        return default if not ASSUME_YES else True
    v = input("%s [%s]: " % (prompt, "Y/n" if default else "y/N")).strip().lower()
    return default if not v else v in ("y", "yes", "是")


# ---------------------------------------------------------------- 权限

_SID = None


def _user_sid():
    global _SID
    if _SID is None:
        out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True).stdout
        m = re.search(r"(S-1-[\d-]+)", out)
        _SID = m.group(1) if m else ""
    return _SID


def private(path):
    """只有当前用户可读写：Unix 目录 700 / 文件 600；Windows 去掉继承 ACL，只授权当前用户与 SYSTEM。"""
    if DRY or not os.path.exists(path):
        return
    is_dir = os.path.isdir(path)
    if not WINDOWS:
        os.chmod(path, 0o700 if is_dir else 0o600)
        return
    sid = _user_sid()
    if not sid:
        say("  ! 无法获取当前用户 SID，未收紧 %s 的权限" % path)
        return
    inherit = "(OI)(CI)" if is_dir else ""
    r = subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", "*%s:%sF" % (sid, inherit),
                        "/grant:r", "*S-1-5-18:%sF" % inherit], capture_output=True, text=True)
    if r.returncode != 0:
        say("  ! 设置 %s 权限失败：%s" % (path, (r.stderr or r.stdout).strip()))


def public_tree(root):
    """skill 目录：Unix 目录 755、文件 644、scripts/*.py 755；Windows 继承用户目录的 ACL（本就只有本人可写）。"""
    if DRY or WINDOWS:
        return
    for d, dirs, files in os.walk(root):
        os.chmod(d, 0o755)
        for f in files:
            p = os.path.join(d, f)
            if not os.path.islink(p):
                os.chmod(p, 0o755 if f.endswith(".py") and os.path.basename(d) == "scripts" else 0o644)


def backup(path, label):
    """备份到 ~/.config/elk-log-analysis/backups（不放在 skills 目录，免得被当成重复 skill），并设为私有。"""
    if DRY or not os.path.lexists(path):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    private(CONFIG_DIR)
    private(BACKUP_DIR)
    dest = os.path.join(BACKUP_DIR, "%s-%s" % (label, STAMP))
    if os.path.islink(path):
        with open(dest + ".link.txt", "w", encoding="utf-8") as f:
            f.write(os.readlink(path) + "\n")
        return dest + ".link.txt"
    (shutil.copytree if os.path.isdir(path) else shutil.copy2)(path, dest)
    private(dest)
    # 每类只保留最近 10 份
    olds = sorted(n for n in os.listdir(BACKUP_DIR) if n.startswith(label + "-"))
    for n in olds[:-10]:
        remove_path(os.path.join(BACKUP_DIR, n))
    return dest


def atomic_write(path, text):
    """先写同目录临时文件再替换；结果文件只有当前用户可读写（客户端配置中可能含 token）。"""
    if DRY:
        return
    tmp = "%s.elk-tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    private(tmp)  # 替换前就收紧，避免出现可被他人读取的窗口
    os.replace(tmp, path)
    private(path)


# ---------------------------------------------------------------- skill 安装

def same_path(a, b):
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return False


def remove_path(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
    elif WINDOWS and _is_junction(path):
        os.rmdir(path)  # 只删除 junction 本身，不动目标
    elif os.path.isdir(path):
        shutil.rmtree(path)


def _is_junction(path):
    try:
        return bool(os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def install_skill(client, link):
    root = os.path.join(CLIENTS[client]["home"], "skills")
    dest = os.path.join(root, SKILL)
    if os.path.lexists(dest) and same_path(dest, SRC):
        say("  %s：%s 就是当前仓库，跳过复制" % (CLIENTS[client]["label"], dest))
        return dest
    ours = os.path.isfile(os.path.join(dest, MARKER)) and not os.path.islink(dest)
    if os.path.lexists(dest) and not ours:
        saved = backup(dest, "skill-%s" % client)
        say("  %s：已有非本脚本安装的 %s，已备份到 %s" % (CLIENTS[client]["label"], dest, saved or "(dry-run)"))
    if DRY:
        say("  [dry-run] %s %s → %s" % ("链接" if link else "复制", SRC, dest))
        return dest
    os.makedirs(root, exist_ok=True)
    if link:
        if os.path.lexists(dest):
            remove_path(dest)
        if WINDOWS:  # junction 不需要管理员权限或开发者模式
            r = subprocess.run(["cmd", "/c", "mklink", "/J", dest, SRC], capture_output=True, text=True)
            if r.returncode != 0:
                sys.exit("创建 junction 失败：%s" % (r.stderr or r.stdout).strip())
        else:
            os.symlink(SRC, dest)
        say("  %s：%s → %s（链接，git pull 后即生效）" % (CLIENTS[client]["label"], dest, SRC))
        return dest
    # 先复制到同级临时目录再替换，避免中途失败留下半个 skill
    staging = os.path.join(root, ".%s.tmp-%d" % (SKILL, os.getpid()))
    if os.path.exists(staging):
        shutil.rmtree(staging)
    os.makedirs(staging)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
    for name in PAYLOAD:
        src = os.path.join(SRC, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(staging, name), ignore=ignore)
        elif os.path.isfile(src):
            shutil.copy2(src, os.path.join(staging, name))
    with open(os.path.join(staging, MARKER), "w", encoding="utf-8") as f:
        json.dump({"source": SRC, "commit": git_commit(), "installed_at": STAMP}, f, ensure_ascii=False, indent=2)
    if os.path.lexists(dest):
        remove_path(dest)
    os.rename(staging, dest)
    public_tree(dest)
    say("  %s：已安装到 %s" % (CLIENTS[client]["label"], dest))
    return dest


def git_commit():
    r = subprocess.run(["git", "-C", SRC, "rev-parse", "--short", "HEAD"], capture_output=True, text=True) \
        if shutil.which("git") else None
    return r.stdout.strip() if r and r.returncode == 0 else ""


# ---------------------------------------------------------------- MCP 条目

def python_exe():
    """MCP 用绝对路径的解释器：GUI 客户端的 PATH 往往与终端不同。虚拟环境可能被删，改用其基础解释器。"""
    exe = sys.executable
    # macOS 系统自带 python3 的真实路径在 CommandLineTools/Xcode 内部，升级或 xcode-select 切换后会变；用稳定的 /usr/bin/python3
    if sys.platform == "darwin" and exe.startswith(("/Library/Developer/CommandLineTools/", "/Applications/Xcode")) \
            and os.path.exists("/usr/bin/python3"):
        exe = "/usr/bin/python3"
    if sys.prefix != sys.base_prefix:
        base = getattr(sys, "_base_executable", "")
        if base and os.path.exists(base):
            say("  注意：当前在虚拟环境中，MCP 将使用基础解释器 %s" % base)
            exe = base
    return exe


def mcp_entry(args, scripts_dir):
    if args.mode == "local":
        e = {"type": "stdio", "command": python_exe(), "args": [os.path.join(scripts_dir, "mcp_server.py")]}
        env = {}
        if args.code_root:
            env["ELK_CODE_ROOT"] = args.code_root
        gn = getattr(args, "gitnexus_info", None)
        if gn and not gn.get("error"):
            # 记录可执行文件与 node：GUI 客户端启动 MCP 时 PATH 不全，也能直接找到。
            # 路径日后失效（如切换了 nvm 版本）时，运行时会自动改用检测到的位置并在 elk_doctor 中提示
            env["ELK_CODE_GITNEXUS"] = gn["launcher"]
            if gn.get("node"):
                env["ELK_CODE_NODE"] = gn["node"]
        if env:
            e["env"] = env
        return e
    auth = "Bearer ${ELK_MCP_TOKEN}" if args.token_store == "env" else "Bearer " + args.token
    return {"type": "http", "url": args.url, "headers": {"Authorization": auth}}


def load_claude():
    if not os.path.exists(CLAUDE_JSON):
        return {}
    with open(CLAUDE_JSON, encoding="utf-8") as f:
        return json.load(f)


def update_claude(name, entry):
    data = load_claude()
    servers = data.setdefault("mcpServers", {})
    old = servers.get(name)
    if old == entry:
        say("  Claude Code：%s 已是最新" % name)
        return
    if old is not None:
        say("  Claude Code：已存在 MCP「%s」（%s）" % (name, describe(old)))
        if not confirm("  替换为新配置（原文件会先备份）？"):
            say("  已跳过 Claude Code")
            return
    saved = backup(CLAUDE_JSON, "claude.json")
    servers[name] = entry
    atomic_write(CLAUDE_JSON, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    say("  Claude Code：%s %s 的 mcpServers.%s%s" % (written(), CLAUDE_JSON, name,
                                                 "（备份 %s）" % saved if saved else ""))


def written():
    return "[dry-run] 将写入" if DRY else "已写入"


def describe(e):
    if not isinstance(e, dict):
        return "未知格式"
    if e.get("url"):
        return "远程 " + e["url"]
    return "本地 %s %s" % (e.get("command", ""), " ".join(e.get("args") or []))


# Codex 使用 TOML；JSON 字符串字面量同时是合法的 TOML basic string
def tq(s):
    return json.dumps(s, ensure_ascii=False)


def codex_block(name, entry, token_store):
    key = name if re.match(r"^[A-Za-z0-9_-]+$", name) else tq(name)
    lines = ["[mcp_servers.%s]" % key]
    if entry["type"] == "stdio":
        lines += ["command = %s" % tq(entry["command"]), "args = [%s]" % ", ".join(tq(a) for a in entry["args"])]
        if entry.get("env"):
            lines += ["", "[mcp_servers.%s.env]" % key] + ["%s = %s" % (k, tq(v)) for k, v in entry["env"].items()]
    else:
        lines.append("url = %s" % tq(entry["url"]))
        if token_store == "env":
            lines.append('bearer_token_env_var = "ELK_MCP_TOKEN"')
        else:
            lines.append("http_headers = { Authorization = %s }" % tq(entry["headers"]["Authorization"]))
    return "\n".join(lines) + "\n"


def split_codex(text, name):
    """把 config.toml 拆成（去掉 [mcp_servers.<name>] 及其子表后的内容, 原有的该段文本）。"""
    head = re.compile(r'^\s*\[\s*mcp_servers\.(?:%s|"%s")(?:\.[^\]]*)?\s*\]\s*$' % (re.escape(name), re.escape(name)))
    kept, old, inside = [], [], False
    for line in text.splitlines(True):
        if line.lstrip().startswith("["):
            inside = bool(head.match(line))
        (old if inside else kept).append(line)
    return "".join(kept), "".join(old)


def update_codex(name, entry, token_store):
    text = open(CODEX_TOML, encoding="utf-8").read() if os.path.exists(CODEX_TOML) else ""
    rest, old = split_codex(text, name)
    block = codex_block(name, entry, token_store)
    if old.strip() == block.strip():
        say("  Codex：%s 已是最新" % name)
        return
    if old.strip():
        say("  Codex：已存在 [mcp_servers.%s]" % name)
        if not confirm("  替换为新配置（原文件会先备份）？"):
            say("  已跳过 Codex")
            return
    saved = backup(CODEX_TOML, "codex-config.toml")
    rest = rest.rstrip("\n")
    atomic_write(CODEX_TOML, (rest + "\n\n" if rest else "") + block)
    say("  Codex：%s %s 的 [mcp_servers.%s]%s" % (written(), CODEX_TOML, name,
                                              "（备份 %s）" % saved if saved else ""))


# ---------------------------------------------------------------- 本地模式配置

def ccswitch_has_elk():
    if not os.path.exists(CCSWITCH_DB):
        return False
    import sqlite3
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(CCSWITCH_DB), uri=True, timeout=3)
        try:
            row = conn.execute("SELECT server_config FROM mcp_servers WHERE id = 'elk'").fetchone()
        finally:
            conn.close()
        return bool(row and isinstance(json.loads(row[0]).get("elk"), dict))
    except Exception:  # noqa: BLE001 — 只是探测，失败按没有处理
        return False


def prepare_local_config():
    path = os.environ.get("ELK_CONFIG") or os.path.join(CONFIG_DIR, "envs.json")
    if ccswitch_has_elk():
        say("  环境配置：使用 cc-switch 中 elk 条目的 elk 对象（优先于 %s）" % path)
        return None
    if DRY:
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    private(os.path.dirname(path))
    if os.path.exists(path):
        private(path)
        say("  环境配置：%s（已存在，未改动；已确认权限为仅本人可读写）" % path)
        return path
    shutil.copy2(os.path.join(SRC, "envs.example.json"), path)
    private(path)
    say("  环境配置：已从模板创建 %s（仅本人可读写），请按实际环境修改" % path)
    return path


def credential_hint():
    if sys.platform == "darwin":
        return ("  密码请存入钥匙串，envs.json 中写 \"password\": \"keychain:elk-log-analysis/<账号>\"：\n"
                "    security add-generic-password -U -s elk-log-analysis -a <账号> -w")
    if WINDOWS:
        return ("  密码请放在用户环境变量中，envs.json 中写 \"password\": \"env:ELK_TEST_PASSWORD\"：\n"
                "    setx ELK_TEST_PASSWORD \"<密码>\"   （设置后需重启客户端）")
    return ("  密码请放在环境变量中，envs.json 中写 \"password\": \"env:ELK_TEST_PASSWORD\"，并在 ~/.profile 中：\n"
            "    export ELK_TEST_PASSWORD='<密码>'   （建议 chmod 600 ~/.profile）")


# ---------------------------------------------------------------- 连通性检查

def check_local(entry):
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "elk_envs", "arguments": {}}}]
    env = dict(os.environ, **entry.get("env", {}))
    try:
        r = subprocess.run([entry["command"]] + entry["args"], input="\n".join(json.dumps(x) for x in reqs) + "\n",
                           capture_output=True, text=True, timeout=30, env=env, encoding="utf-8")
    except (OSError, subprocess.TimeoutExpired) as e:
        say("  ✘ 无法启动本地 MCP：%s" % e)
        return
    out = [json.loads(line) for line in r.stdout.splitlines() if line.strip().startswith("{")]
    if len(out) < 2:
        say("  ✘ 本地 MCP 未正常响应：%s" % (r.stderr.strip()[-300:] or r.stdout[-300:]))
        return
    res = out[1].get("result", {})
    text = (res.get("content") or [{}])[0].get("text", "")
    if res.get("isError"):
        say("  ! MCP 可以启动，但环境配置还不可用：\n    " + text.replace("\n", "\n    ")[:600])
    else:
        say("  ✔ 本地 MCP 正常，已配置的环境：\n    " + text.replace("\n", "\n    ")[:800])
    say("  %s git：%s" % ("✔" if shutil.which("git") else "·",
                          "已安装" if shutil.which("git") else "未找到，影响 code_* 源码定位"))
    env = entry.get("env", {})
    if env.get("ELK_CODE_GITNEXUS"):
        info = gitnexus_bin.locate(env["ELK_CODE_GITNEXUS"], env.get("ELK_CODE_NODE", ""))
        r = subprocess.run(info["argv"] + ["--version"], capture_output=True, text=True, timeout=60,
                           env=gitnexus_bin.run_env(info)) if info.get("argv") else None
        ok = r is not None and r.returncode == 0
        say("  %s gitnexus：%s" % ("✔" if ok else "✘", gitnexus_bin.describe(info) if ok
                                   else "无法运行：%s" % ((r.stderr or r.stdout).strip()[:200] if r else info["error"])))
    else:
        say("  · gitnexus：未配置，code_prepare 调用链索引不可用（安装后重新运行本脚本即可）")


def gitnexus_url(url):
    """elk 端点 …/mcp → GitNexus 转发端点 …/gitnexus/mcp（服务前有路径前缀的反向代理时同样适用）。"""
    parts = urllib.parse.urlsplit(url)
    path = parts.path[:-len("/mcp")] if parts.path.endswith("/mcp") else parts.path.rstrip("/")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path + "/gitnexus/mcp", "", ""))


def probe_gitnexus(url, token):
    """服务端是否开启了 GitNexus 转发：带 token 返回 200，或不带 token 返回 401 都说明端点存在；404 表示未开启。"""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "elk-installer", "version": "1"}}}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urllib.request.urlopen(urllib.request.Request(gitnexus_url(url), data=body, method="POST",
                                                           headers=headers), timeout=15):
            return True
    except urllib.error.HTTPError as e:
        return e.code == 401 and not token
    except Exception:  # noqa: BLE001
        return False


def check_remote(url, token):
    """写入配置前验证：返回 ok / unauthorized / unreachable。"""
    parts = urllib.parse.urlsplit(url)
    health = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/healthz", "", ""))
    try:
        urllib.request.urlopen(health, timeout=10).read()
        say("  ✔ 服务可达：%s" % health)
    except Exception as e:  # noqa: BLE001
        say("  · 健康检查未通过（%s）：%s；若代理只转发 /mcp 可忽略" % (health, e))
    if not token:
        say("  · 未提供 token，跳过鉴权检查")
        return "ok"
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "elk-installer", "version": "1"}}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            info = json.loads(r.read()).get("result", {}).get("serverInfo", {})
        say("  ✔ token 有效，服务端 %s %s" % (info.get("name", ""), info.get("version", "")))
        return "ok"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            say("  ✘ 401：token 无效或已停用，请核对或联系管理员")
            return "unauthorized"
        say("  ✘ %s %s（地址是否写错？MCP 端点一般以 /mcp 结尾）" % (e.code, e.reason))
    except Exception as e:  # noqa: BLE001
        say("  ✘ 连接 %s 失败：%s" % (url, e))
    return "unreachable"


# ---------------------------------------------------------------- 主流程

def detect_clients(requested):
    if requested:
        names = [c.strip() for c in requested.split(",") if c.strip()]
        bad = [c for c in names if c not in CLIENTS]
        if bad:
            sys.exit("未知客户端：%s（可选 %s）" % (",".join(bad), ",".join(CLIENTS)))
        return names
    found = [c for c, v in CLIENTS.items() if os.path.isdir(v["home"]) or shutil.which(v["bin"])]
    return found or ["claude"]


def resolve_args(args):
    if not args.mode:
        if args.url:
            args.mode = "remote"
        elif interactive():
            say("选择 MCP 调用方式：\n  1) 远程：连接团队部署的 elk MCP 服务（推荐，只需服务地址和个人 token）\n"
                "  2) 本地：本机直接运行，自己配置 ELK 地址与凭据")
            args.mode = "local" if ask("请输入 1 或 2", "1") == "2" else "remote"
        else:
            sys.exit("非交互模式下请指定 --mode local 或 --mode remote")
    if args.mode == "remote":
        args.url = args.url or ask("服务地址（如 https://elk-mcp.example.com/mcp）")
        if not args.url:
            sys.exit("远程模式需要服务地址 --url")
        if not re.match(r"^https?://", args.url):
            sys.exit("服务地址需以 http:// 或 https:// 开头")
        if urllib.parse.urlsplit(args.url).path in ("", "/"):
            args.url = args.url.rstrip("/") + "/mcp"
        if args.url.startswith("http://") and not re.match(r"^http://(localhost|127\.|10\.|192\.168\.|172\.)", args.url):
            say("! 警告：使用明文 HTTP 连接非内网地址，token 可能被截获，建议改用 https")
        if args.token_stdin:
            args.token = sys.stdin.readline().strip()
        args.token = args.token or os.environ.get("ELK_MCP_TOKEN", "")
        if not args.token and interactive():
            args.token = getpass.getpass("个人 token（输入不回显）: ").strip()
        if not args.token and args.token_store == "config":
            sys.exit("远程模式需要 token：交互输入、--token-stdin，或预先设置环境变量 ELK_MCP_TOKEN")
    else:
        if args.code_root is None:
            args.code_root = ask("源码根目录 ELK_CODE_ROOT（code_* 用，多个逗号分隔，留空跳过）", "")
        args.gitnexus_info = detect_gitnexus(args)


def detect_gitnexus(args):
    """在当前终端检测 gitnexus；检测不到时交互询问安装位置。返回检测结果（可能带 error）。"""
    if args.no_gitnexus_detect:
        return None
    step("检测 gitnexus（code_prepare 建调用链索引用，可选）")
    def find(path):
        info = gitnexus_bin.locate(path or "", args.node or "")
        if path and info.get("source") != "ELK_CODE_GITNEXUS":
            # 安装时明确指定的路径无效：报错，不像运行时那样悄悄回退到自动检测
            return {"error": "%s 下没有找到 gitnexus（可填可执行文件、npm 全局目录、nvm 版本目录或 gitnexus 包目录）"
                             % path, "argv": None}
        return info

    info = find(args.gitnexus)
    if args.gitnexus and info.get("error") and not interactive():
        sys.exit("--gitnexus：" + info["error"])
    while info.get("error"):
        say("  · " + info["error"])
        path = ask("  gitnexus 可执行文件或安装目录（如 ~/.nvm/versions/node/v22.19.0，留空跳过）", "")
        if not path:
            say("  已跳过：code_locate 仍可用，只是不能建调用链索引；安装 gitnexus 后重新运行本脚本即可")
            return info
        info = find(path)
    say("  ✔ " + gitnexus_bin.describe(info))
    if info.get("warning"):
        say("  ! 建议：升级 node，或用 --node 指定满足要求的 node")
    return info


def uninstall(args, clients):
    step("卸载")
    for c in clients:
        dest = os.path.join(CLIENTS[c]["home"], "skills", SKILL)
        if not os.path.lexists(dest):
            continue
        if same_path(dest, SRC) and not os.path.islink(dest) and not _is_junction(dest):
            say("  %s：%s 就是当前仓库，不删除" % (CLIENTS[c]["label"], dest))
        elif os.path.islink(dest) or _is_junction(dest) or os.path.isfile(os.path.join(dest, MARKER)):
            if not DRY:
                remove_path(dest)
            say("  %s：已删除 %s" % (CLIENTS[c]["label"], dest))
        else:
            say("  %s：%s 不是本脚本安装的，保留" % (CLIENTS[c]["label"], dest))
    for name in (args.name, args.gitnexus_name):
        data = load_claude()
        if name in data.get("mcpServers", {}) and confirm("  从 Claude Code 移除 MCP「%s」？" % name):
            backup(CLAUDE_JSON, "claude.json")
            del data["mcpServers"][name]
            atomic_write(CLAUDE_JSON, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            say("  Claude Code：已移除 %s" % name)
        if os.path.exists(CODEX_TOML):
            rest, old = split_codex(open(CODEX_TOML, encoding="utf-8").read(), name)
            if old.strip() and confirm("  从 Codex 移除 [mcp_servers.%s]？" % name):
                backup(CODEX_TOML, "codex-config.toml")
                atomic_write(CODEX_TOML, rest)
                say("  Codex：已移除 %s" % name)
    if args.purge and os.path.isdir(CONFIG_DIR) and confirm("  删除 %s（含 envs.json 与所有备份）？" % CONFIG_DIR, False):
        if not DRY:
            shutil.rmtree(CONFIG_DIR)
        say("  已删除 %s" % CONFIG_DIR)
    say("\n完成。重启客户端后生效。")


def main():
    global DRY, ASSUME_YES
    p = argparse.ArgumentParser(description="安装 elk-log-analysis skill 与 MCP（macOS / Linux / Windows）")
    p.add_argument("--mode", choices=["local", "remote"], help="local：本机运行；remote：连接团队服务")
    p.add_argument("--url", help="远程服务地址，如 https://elk-mcp.example.com/mcp")
    p.add_argument("--token", help="个人 token（会留在 shell 历史中，建议交互输入或用 --token-stdin）")
    p.add_argument("--token-stdin", action="store_true", help="从标准输入读取 token")
    p.add_argument("--token-store", choices=["config", "env"], default="config",
                   help="config（默认）：token 写入客户端配置（仅本人可读）；env：配置中引用环境变量 ELK_MCP_TOKEN")
    p.add_argument("--code-root", help="本地模式的源码根目录 ELK_CODE_ROOT，多个用逗号分隔")
    p.add_argument("--gitnexus", help="本地模式：gitnexus 可执行文件或安装目录（npm 全局目录、nvm 版本目录、"
                                      "gitnexus 包目录均可），不填则自动检测")
    p.add_argument("--node", help="本地模式：运行 gitnexus 的 node（需满足 gitnexus 的版本要求），不填则自动选择")
    p.add_argument("--no-gitnexus-detect", action="store_true", help="本地模式：不检测、不配置 gitnexus")
    p.add_argument("--clients", help="要配置的客户端，逗号分隔：claude,codex（默认自动检测）")
    p.add_argument("--name", default="elk", help="MCP 名称，默认 elk")
    p.add_argument("--gitnexus-name", default="gitnexus-remote",
                   help="远程 GitNexus 的 MCP 名称，默认 gitnexus-remote（与本机的 gitnexus 区分）")
    p.add_argument("--no-gitnexus", action="store_true", help="远程模式下不注册服务端的 GitNexus")
    p.add_argument("--link", action="store_true", help="用符号链接/junction 指向仓库，而不是复制（git pull 即更新）")
    p.add_argument("--skip-check", action="store_true", help="跳过安装后的连通性检查")
    p.add_argument("--dry-run", action="store_true", help="只显示将要做的操作，不写任何文件")
    p.add_argument("-y", "--yes", action="store_true", help="非交互：使用默认值，已有条目直接替换（先备份）")
    p.add_argument("--uninstall", action="store_true", help="卸载 skill 与 MCP 条目")
    p.add_argument("--purge", action="store_true", help="配合 --uninstall，同时删除 ~/.config/elk-log-analysis")
    args = p.parse_args()
    DRY, ASSUME_YES = args.dry_run, args.yes
    clients = detect_clients(args.clients)

    say("elk-log-analysis 安装（%s，Python %s）" % ({"darwin": "macOS", "win32": "Windows"}.get(sys.platform, sys.platform),
                                              sys.version.split()[0]))
    say("仓库：%s\n客户端：%s%s" % (SRC, ", ".join(CLIENTS[c]["label"] for c in clients), "\n[dry-run] 不会写入任何文件" if DRY else ""))
    if args.uninstall:
        return uninstall(args, clients)
    resolve_args(args)
    if args.mode == "remote" and not args.skip_check:
        step("验证远程服务")
        status = check_remote(args.url, args.token)
        if status == "unauthorized":
            sys.exit("token 验证失败，未做任何改动。")
        if status == "unreachable" and not confirm("  服务暂时连不上（未连 VPN / 服务未启动？），仍然继续安装？", True):
            sys.exit("已取消，未做任何改动。")

    step("安装 skill")
    dests = {c: install_skill(c, args.link) for c in clients}
    scripts_dir = os.path.join(dests.get("claude") or next(iter(dests.values())), "scripts")

    step("注册 MCP（%s）" % ("本地 stdio" if args.mode == "local" else "远程 " + args.url))
    entry = mcp_entry(args, scripts_dir)
    entries = [(args.name, entry)]
    if args.mode == "remote" and not args.no_gitnexus and probe_gitnexus(args.url, args.token):
        say("  检测到服务端提供 GitNexus（%s），一并注册为「%s」" % (gitnexus_url(args.url), args.gitnexus_name))
        entries.append((args.gitnexus_name, dict(entry, url=gitnexus_url(args.url))))
    for name, e in entries:
        if "claude" in clients:
            update_claude(name, e)
        if "codex" in clients:
            update_codex(name, e, args.token_store)

    if args.mode == "local":
        step("本地环境配置")
        cfg = prepare_local_config()
        if cfg:
            say(credential_hint())
    if args.mode == "local" and not args.skip_check and not DRY:
        step("检查")
        check_local(entry)

    step("完成")
    say("- 重启 %s 后生效；Claude Code 可用 `claude mcp list` 确认 %s 为 Connected" %
        ("/".join(CLIENTS[c]["label"] for c in clients), args.name))
    if args.mode == "remote" and args.token_store == "env":
        say("- 已配置为读取环境变量 ELK_MCP_TOKEN，请设置：" +
            ("setx ELK_MCP_TOKEN \"<token>\"" if WINDOWS else "在 ~/.zshrc 或 ~/.bashrc 中 export ELK_MCP_TOKEN=<token>"))
    if os.path.exists(CCSWITCH_DB):
        say("- 检测到 cc-switch：可在其 MCP / Skills 页面使用「从应用导入」统一管理；"
            "若 cc-switch 中已有同名 elk 条目，同步时会覆盖这里写入的配置")
    say("- 更新：cd %s && git pull && 重新运行安装脚本" % SRC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\n已取消")
