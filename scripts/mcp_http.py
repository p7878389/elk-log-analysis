#!/usr/bin/env python3
"""ELK 日志查询 MCP 服务的 HTTP 传输（MCP Streamable HTTP，无状态、只返回 JSON，仅标准库，只读）。

与 mcp_server.py（stdio）共用同一套工具实现，供团队通过网络共用一个部署：
  python3 mcp_http.py [--env-file .env]    启动服务（配置见下方环境变量，也可写在 .env 中）
  python3 mcp_http.py gen-token <name>     生成一个成员 token，输出应写入 tokens 文件的条目

环境变量：
  ELK_HTTP_ENV_FILE        等同 --env-file
  ELK_HTTP_HOST            监听地址，默认 127.0.0.1（容器内用 0.0.0.0）
  ELK_HTTP_PORT            端口，默认 8765
  ELK_HTTP_PATH            MCP 端点路径，默认 /mcp
  ELK_HTTP_TOKENS_FILE     成员 token 文件（JSON，见 deploy/tokens.example.json），修改后自动重新加载
  ELK_HTTP_NO_AUTH         =1 时不校验 token（仅限前面已有反向代理做鉴权的场景）
  ELK_HTTP_DISABLED_TOOLS  全局禁用的工具，逗号分隔，如 code_prepare
  ELK_HTTP_ALLOWED_ORIGINS 允许的 Origin（逗号分隔）；带 Origin 头且不在列表中的请求拒绝（防 DNS rebinding）
  ELK_HTTP_CERT / ELK_HTTP_KEY  证书与私钥路径，配置后直接提供 HTTPS
  ELK_HTTP_MAX_BODY        请求体上限字节数，默认 1048576
  ELK_GITNEXUS_MCP         =1 时托管一个 gitnexus MCP（仅监听本机），并在 /gitnexus/mcp 以成员 token 转发，
                           让远程成员直接查询服务端的 GitNexus 索引（调用链/影响面）
  ELK_GITNEXUS_PORT        托管的 gitnexus MCP 本机端口，默认 8767
  GitLab 仓库定时同步的配置见 repo_sync.py（ELK_GITLAB_URL / ELK_GITLAB_TOKEN 等），配置后自动启用

token 权限：prod=false（默认）的成员查询生产日志会被拒绝，code_* 退化为按分支最新提交（不查生产日志）；
admin=false（默认）的成员调用 elk_doctor 时 fix 会被剥离，只能诊断不能修复，也不能手动触发仓库同步；
code=false 的成员不能使用 code_* 与 /gitnexus/mcp（不能查看源码）。
"""
import hashlib
import hmac
import http.client
import json
import os
import secrets
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envfile  # noqa: E402

envfile.apply_from_argv()  # 必须在导入 elk_core 之前：其 CONFIG_PATH 在导入时读取 ELK_CONFIG
import mcp_server as mcp  # noqa: E402

HOST = os.environ.get("ELK_HTTP_HOST", "127.0.0.1")
PORT = int(os.environ.get("ELK_HTTP_PORT") or 8765)
PATH = "/" + os.environ.get("ELK_HTTP_PATH", "/mcp").strip("/")
TOKENS_FILE = os.environ.get("ELK_HTTP_TOKENS_FILE", "")
NO_AUTH = os.environ.get("ELK_HTTP_NO_AUTH", "").lower() in ("1", "true", "yes")
DISABLED = {t.strip() for t in os.environ.get("ELK_HTTP_DISABLED_TOOLS", "").split(",") if t.strip()}
ORIGINS = {o.strip().rstrip("/") for o in os.environ.get("ELK_HTTP_ALLOWED_ORIGINS", "").split(",") if o.strip()}
MAX_BODY = int(os.environ.get("ELK_HTTP_MAX_BODY") or 1048576)
GITNEXUS = os.environ.get("ELK_GITNEXUS_MCP", "").lower() in ("1", "true", "yes")
GN_PORT = int(os.environ.get("ELK_GITNEXUS_PORT") or 8767)
GN_PREFIX = "/gitnexus"
GN_TOKEN = secrets.token_urlsafe(32)  # 仅本进程与托管的 gitnexus 之间使用，每次启动随机生成
# 会改写服务端 worktree / 索引分组的 gitnexus 工具：共享服务上只读，一律拒绝
GN_BLOCKED = {"rename", "group_sync"}


def sha256(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Tokens:
    """tokens 文件：{"tokens":[{"name":"alice","sha256":"...","prod":false,"admin":false}]}。
    也接受明文 "token" 字段（不推荐）。按 mtime 自动重新加载，增删成员无需重启。"""

    def __init__(self, path):
        self.path, self.mtime, self.items, self.lock = path, None, [], threading.Lock()

    def _load(self):
        mtime = os.stat(self.path).st_mtime
        if mtime == self.mtime:
            return
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        items = []
        for t in data.get("tokens", []):
            digest = t.get("sha256") or (sha256(t["token"]) if t.get("token") else None)
            if not t.get("name") or not digest or t.get("disabled"):
                continue
            items.append({"name": t["name"], "sha256": digest.lower(),
                          "prod": t.get("prod") is True, "admin": t.get("admin") is True,
                          "code": t.get("code") is not False})
        self.items, self.mtime = items, mtime
        log("已加载 %d 个成员 token（%s）" % (len(items), self.path))

    def lookup(self, token):
        with self.lock:
            try:
                self._load()
            except (OSError, ValueError, KeyError) as e:  # 文件临时损坏时沿用上一次的有效内容
                log("tokens 文件读取失败，沿用旧内容：%s" % e)
        digest = sha256(token)
        for t in self.items:
            if hmac.compare_digest(t["sha256"], digest):
                return t
        return None


TOKENS = Tokens(TOKENS_FILE) if TOKENS_FILE else None
ANONYMOUS = {"name": "anonymous", "prod": True, "admin": True, "code": True}


def log(msg):
    sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


def restrict(req, user):
    """按成员权限改写请求：剥离无权使用的参数，禁用工具直接拒绝。返回 (改写后的请求, 拒绝响应或 None)。"""
    if req.get("method") != "tools/call":
        return req, None
    params = dict(req.get("params") or {})
    name, args = params.get("name"), dict(params.get("arguments") or {})
    def deny(text):
        log("%s 被拒绝 %s：%s" % (user["name"], name, text[:40]))
        return req, mcp.reply(req.get("id"), {"content": [{"type": "text", "text": text}], "isError": True})
    if name in DISABLED:
        return deny("工具 %s 已在服务端禁用" % name)
    if name.startswith("code_") and not user["code"]:
        return deny("当前成员（%s）的 token 没有源码查看权限，请联系 elk MCP 管理员，不要重试。" % user["name"])
    if name == "code_repos" and args.get("sync") is True and not user["admin"]:
        return deny("只有管理员可以手动触发仓库同步；服务会按计划自动同步，可稍后再查看 code_repos。")
    if not user["prod"] and args.pop("confirm_production", None) is True and name.startswith("elk_") \
            and name != "elk_doctor":
        return deny("当前成员（%s）的 token 没有生产环境权限，无法查询生产日志。请告知用户联系 elk MCP 管理员"
                    "在 tokens 文件中为其开启 prod，不要重试。" % user["name"])
    # code_* 去掉 confirm_production 后按分支最新提交处理，elk_doctor 则跳过生产连通性检查
    if name == "elk_doctor" and not user["admin"]:
        args.pop("fix", None)
    params["arguments"] = args
    return dict(req, params=params), None


def process(req, user):
    if not isinstance(req, dict):
        return mcp.reply(None, error={"code": -32600, "message": "Invalid Request"})
    req, denied = restrict(req, user)
    if denied:
        return denied
    start = time.time()
    msg = mcp.handle(req)
    if req.get("method") == "tools/call" and msg is not None:
        p = req.get("params") or {}
        err = (msg.get("result") or {}).get("isError")
        log("%s %s env=%s %s %dms" % (user["name"], p.get("name"), (p.get("arguments") or {}).get("env") or "-",
                                      "ERR" if err else "ok", (time.time() - start) * 1000))
    elif req.get("method") == "tools/list" and msg and DISABLED:
        msg["result"] = {"tools": [t for t in msg["result"]["tools"] if t["name"] not in DISABLED]}
    return msg


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "elk-mcp"

    def log_message(self, fmt, *args):  # 访问日志由 process 按工具调用输出，这里静默
        pass

    def _send(self, code, body=None, ctype="application/json", headers=None):
        data = b"" if body is None else (body if isinstance(body, bytes) else
                                         json.dumps(body, ensure_ascii=False).encode("utf-8"))
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if data:
            self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _error(self, code, message, headers=None):
        self._send(code, {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": message}},
                   headers=headers)

    def _client(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def _auth(self):
        """返回成员信息；失败时已发送 401 并返回 None。"""
        if NO_AUTH:
            return ANONYMOUS
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        user = TOKENS.lookup(token) if token else None
        if not user:
            log("鉴权失败 from %s" % self._client())
            self._error(401, "未授权：请在 Authorization 头中携带 Bearer token",
                        {"WWW-Authenticate": 'Bearer realm="elk-mcp"'})
        return user

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin or origin.rstrip("/") in ORIGINS:
            return True
        self._error(403, "Origin 不被允许: %s" % origin)
        return False

    def _is_gitnexus(self):
        return GITNEXUS and (self.path == GN_PREFIX or self.path.startswith(GN_PREFIX + "/"))

    def _proxy_gitnexus(self, body=b""):
        """以成员 token 鉴权后，把请求转发给本机托管的 gitnexus MCP（换成内部 token）；SSE/分块响应边读边转发。"""
        if not self._origin_ok():
            return
        user = self._auth()
        if not user:
            return
        if not user["code"]:
            return self._error(403, "当前成员没有源码查看权限")
        if body:
            try:
                req = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                req = None
            if isinstance(req, dict) and req.get("method") == "tools/call":
                tool = (req.get("params") or {}).get("name")
                log("%s gitnexus:%s" % (user["name"], tool))
                if tool in GN_BLOCKED:
                    return self._send(200, mcp.reply(req.get("id"), {"content": [{"type": "text", "text": (
                        "团队共享的 GitNexus 为只读，不支持 %s。需要重构时请在自己本机的仓库中操作。" % tool)}],
                        "isError": True}))
        headers = {k: self.headers[k] for k in ("Content-Type", "Accept", "Mcp-Session-Id",
                                                 "Mcp-Protocol-Version", "Last-Event-ID") if self.headers.get(k)}
        headers["Authorization"] = "Bearer " + GN_TOKEN
        # GET 是服务端推送的长连接，不设读超时
        conn = http.client.HTTPConnection("127.0.0.1", GN_PORT, timeout=None if self.command == "GET" else 600)
        try:
            conn.request(self.command, self.path[len(GN_PREFIX):] or "/", body=body or None, headers=headers)
            resp = conn.getresponse()
        except OSError as e:
            conn.close()
            return self._error(502, "GitNexus 服务暂不可用（%s），请稍后重试或联系管理员" % e)
        self.send_response(resp.status)
        for k in ("Content-Type", "Mcp-Session-Id", "Mcp-Protocol-Version", "Cache-Control", "Allow"):
            if resp.getheader(k):
                self.send_header(k, resp.getheader(k))
        try:
            if resp.getheader("Content-Length") is not None:
                data = resp.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            conn.close()

    def do_GET(self):
        if self.path.split("?")[0] == "/healthz":
            return self._send(200, b"ok", "text/plain")
        if self._is_gitnexus():
            return self._proxy_gitnexus()
        if self.path.split("?")[0] == PATH:  # 不提供服务端推送的 SSE 流
            return self._send(405, headers={"Allow": "POST"})
        self._send(404)

    def do_DELETE(self):
        if self._is_gitnexus():
            return self._proxy_gitnexus()
        self._send(405, headers={"Allow": "POST"})  # elk MCP 无状态，没有会话可结束

    def do_POST(self):
        # 先读完请求体再做任何拒绝：keep-alive 下未读的请求体会被当成下一个请求解析
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.close_connection = True
            return self._error(413, "请求体过大")
        body = self.rfile.read(length) if length > 0 else b""
        if self._is_gitnexus():
            return self._proxy_gitnexus(body)
        if self.path.split("?")[0] != PATH:
            return self._send(404)
        if not self._origin_ok():
            return
        user = self._auth()
        if not user:
            return
        if not body:
            return self._error(400, "请求体为空")
        try:
            req = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._send(400, mcp.reply(None, error={"code": -32700, "message": "Parse error"}))
        if isinstance(req, list):
            out = [m for m in (process(r, user) for r in req) if m is not None]
            return self._send(200, out) if out else self._send(202)
        msg = process(req, user)
        self._send(200, msg) if msg is not None else self._send(202)


def start_gitnexus():
    """托管 gitnexus MCP（只监听 127.0.0.1，内部 token 经环境变量传入，不出现在命令行）；退出后自动重启。"""
    ccfg = mcp.code.load_code_config()
    gn = ccfg["gitnexus"]
    if not os.path.exists(gn):
        log("未找到 gitnexus（%s），/gitnexus/mcp 不可用" % gn)
        return
    logfile = os.path.join(mcp.code._cache_dir(ccfg), "gitnexus-mcp.log")
    env = dict(mcp.code._gitnexus_env(ccfg), GITNEXUS_MCP_AUTH_TOKEN=GN_TOKEN)

    def loop():
        while True:
            with open(logfile, "a", encoding="utf-8") as out:
                proc = subprocess.Popen([gn, "mcp", "--http", "--host", "127.0.0.1", "--port", str(GN_PORT)],
                                        env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
                log("gitnexus MCP 已启动（pid %d，127.0.0.1:%d，转发路径 %s/mcp）" % (proc.pid, GN_PORT, GN_PREFIX))
                code = proc.wait()
            log("gitnexus MCP 退出（%s），5 秒后重启，详见 %s" % (code, logfile))
            time.sleep(5)

    threading.Thread(target=loop, name="gitnexus-mcp", daemon=True).start()


def gen_token(name):
    token = "elk_" + secrets.token_urlsafe(32)
    print("成员 %s 的 token（只显示这一次，请通过私密渠道发给对方）：\n\n  %s\n" % (name, token))
    print("把下面这行加入 tokens 文件的 tokens 数组（需要查生产时把 prod 改为 true）：\n")
    print("  " + json.dumps({"name": name, "sha256": sha256(token), "prod": False, "admin": False}))


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "gen-token":
        if len(sys.argv) != 3:
            sys.exit("用法: mcp_http.py gen-token <成员名>")
        return gen_token(sys.argv[2])
    if not NO_AUTH and not TOKENS_FILE:
        sys.exit("未配置 ELK_HTTP_TOKENS_FILE。团队共享必须启用 token 鉴权；"
                 "若前面已有反向代理做鉴权，可设置 ELK_HTTP_NO_AUTH=1。")
    if TOKENS:
        TOKENS._load()  # 启动时校验并加载一次，文件有误时直接报错退出
        if not TOKENS.items:
            log("警告：tokens 文件中没有可用成员，所有请求都会被拒绝")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    scheme = "http"
    cert, key = os.environ.get("ELK_HTTP_CERT"), os.environ.get("ELK_HTTP_KEY")
    if cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key or None)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        scheme = "https"
    log("elk MCP HTTP 服务已启动: %s://%s:%d%s（鉴权: %s，禁用工具: %s）"
        % (scheme, HOST, PORT, PATH, "关闭" if NO_AUTH else "Bearer token", ",".join(sorted(DISABLED)) or "无"))
    scfg = mcp.repo_sync.load_sync_config()
    if scfg:
        bad = mcp.repo_sync.gitlab_auth.problem(scfg["auth"])
        if bad:
            log("GitLab 仓库同步未启用：%s" % bad)
        else:
            mcp.repo_sync.start_scheduler(scfg, log)
            log("GitLab 仓库同步已启用：%s（%s）→ %s，%s%s" % (
                scfg["url"], mcp.repo_sync.gitlab_auth.describe(scfg["auth"]), scfg["dir"], "每 %d 秒" % scfg["interval"] if scfg["interval"] else "仅手动触发",
                "，同步后预建 %s 分支索引" % ",".join(scfg["index_envs"]) if scfg["index_envs"] else ""))
    if GITNEXUS:
        start_gitnexus()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
