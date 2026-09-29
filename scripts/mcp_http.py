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

token 权限：prod=false（默认）的成员查询生产日志会被拒绝，code_* 退化为按分支最新提交（不查生产日志）；
admin=false（默认）的成员调用 elk_doctor 时 fix 会被剥离，只能诊断不能修复。
"""
import hashlib
import hmac
import json
import os
import secrets
import ssl
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PATH_KEYS = ("ELK_CONFIG", "ELK_HTTP_TOKENS_FILE", "ELK_HTTP_CERT", "ELK_HTTP_KEY", "ELK_CODE_CACHE")


def load_env_file(path):
    """读取 KEY=VALUE 格式的 .env（与 docker compose env_file 相同：不做引号/变量展开，# 开头为注释）。
    已存在的环境变量优先；PATH_KEYS 中的相对路径按 .env 所在目录解析，便于各平台原生部署共用一份配置。"""
    base = os.path.dirname(os.path.abspath(path))
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = (x.strip() for x in line.split("=", 1))
            if key in PATH_KEYS and value and not os.path.isabs(os.path.expanduser(value)):
                value = os.path.join(base, value)
            if value:
                os.environ.setdefault(key, value)


# 必须在导入 elk_core 之前加载：其 CONFIG_PATH 在导入时读取 ELK_CONFIG
if "--env-file" in sys.argv:
    i = sys.argv.index("--env-file")
    if i + 1 >= len(sys.argv):
        sys.exit("用法: mcp_http.py --env-file <.env 路径>")
    load_env_file(sys.argv[i + 1])
    del sys.argv[i:i + 2]
elif os.environ.get("ELK_HTTP_ENV_FILE"):
    load_env_file(os.environ["ELK_HTTP_ENV_FILE"])

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_server as mcp  # noqa: E402

HOST = os.environ.get("ELK_HTTP_HOST", "127.0.0.1")
PORT = int(os.environ.get("ELK_HTTP_PORT") or 8765)
PATH = "/" + os.environ.get("ELK_HTTP_PATH", "/mcp").strip("/")
TOKENS_FILE = os.environ.get("ELK_HTTP_TOKENS_FILE", "")
NO_AUTH = os.environ.get("ELK_HTTP_NO_AUTH", "").lower() in ("1", "true", "yes")
DISABLED = {t.strip() for t in os.environ.get("ELK_HTTP_DISABLED_TOOLS", "").split(",") if t.strip()}
ORIGINS = {o.strip().rstrip("/") for o in os.environ.get("ELK_HTTP_ALLOWED_ORIGINS", "").split(",") if o.strip()}
MAX_BODY = int(os.environ.get("ELK_HTTP_MAX_BODY") or 1048576)


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
                          "prod": t.get("prod") is True, "admin": t.get("admin") is True})
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
ANONYMOUS = {"name": "anonymous", "prod": True, "admin": True}


def log(msg):
    sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


def restrict(req, user):
    """按成员权限改写请求：剥离无权使用的参数，禁用工具直接拒绝。返回 (改写后的请求, 拒绝响应或 None)。"""
    if req.get("method") != "tools/call":
        return req, None
    params = dict(req.get("params") or {})
    name, args = params.get("name"), dict(params.get("arguments") or {})
    if name in DISABLED:
        return req, mcp.reply(req.get("id"), {"content": [{"type": "text", "text": "工具 %s 已在服务端禁用" % name}],
                                              "isError": True})
    if not user["prod"] and args.pop("confirm_production", None) is True and name.startswith("elk_") \
            and name != "elk_doctor":
        log("%s 无生产权限，拒绝 %s" % (user["name"], name))
        return req, mcp.reply(req.get("id"), {"content": [{"type": "text", "text": (
            "当前成员（%s）的 token 没有生产环境权限，无法查询生产日志。请告知用户联系 elk MCP 管理员"
            "在 tokens 文件中为其开启 prod，不要重试。" % user["name"])}], "isError": True})
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

    def do_GET(self):
        if self.path.split("?")[0] == "/healthz":
            return self._send(200, b"ok", "text/plain")
        if self.path.split("?")[0] == PATH:  # 不提供服务端推送的 SSE 流
            return self._send(405, headers={"Allow": "POST"})
        self._send(404)

    def do_DELETE(self):  # 无状态服务，没有会话可结束
        self._send(405, headers={"Allow": "POST"})

    def do_POST(self):
        # 先读完请求体再做任何拒绝：keep-alive 下未读的请求体会被当成下一个请求解析
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.close_connection = True
            return self._error(413, "请求体过大")
        body = self.rfile.read(length) if length > 0 else b""
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
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
