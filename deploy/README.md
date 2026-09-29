# elk MCP 团队部署

把 elk MCP 部署成 HTTP 服务（MCP Streamable HTTP）。团队成员在 Windows、macOS 或 Linux 上，用 Claude Code、Codex、Cursor、Claude Desktop 等客户端，通过 URL 加个人 token 连接即可。

```
成员客户端 ──HTTPS + Bearer token──▶ [Caddy/Nginx] ──▶ mcp_http.py ──▶ Kibana/ES（test/stag/prod）
 (Win/Mac/Linux)                                          └─ 可选：/repos 源码 + GitNexus（code_*）
```

- 服务端**只用 Python 标准库**，因此可以用 Docker 部署（任何能跑 Docker 的系统），也可以直接用 Python 3.8+ 在 Linux、macOS、Windows 上运行。
- 与本地 stdio 版（`scripts/mcp_server.py`）共用同一套工具实现，行为一致。
- 鉴权：每个成员一个 token，服务端只保存 sha256。可以按成员开关生产权限（`prod`）和修复权限（`admin`），修改后自动生效，不用重启。

## 1. 服务端部署

### 方式一：Docker（推荐，任何系统）

```bash
git clone <本仓库> /opt/elk-log-analysis && cd /opt/elk-log-analysis/deploy
cp .env.example .env && chmod 600 .env            # 填 ELK_TEST_PASSWORD / ELK_PROD_PASSWORD
cp config/envs.example.json config/envs.json      # 环境列表，密码只写 env:变量名
cp config/tokens.example.json config/tokens.json  # 成员 token，见第 2 节
docker compose up -d --build
curl http://127.0.0.1:8765/healthz                # 返回 ok
```

- **文件权限**：容器以 uid 10001 运行，`config/` 下的文件必须让它可读，执行 `chmod 644 config/*.json`（这两个文件不含密码，密码在 `.env` 中，`.env` 只由宿主机上的 compose 读取，保持 600 即可）。
- **HTTPS**：服务器有公网域名时，在 `.env` 中设置 `ELK_DOMAIN`，然后运行 `docker compose --profile tls up -d --build`，Caddy 会自动申请证书。纯内网的做法见 `Caddyfile` 中的注释，也可以用 `ELK_HTTP_CERT`/`ELK_HTTP_KEY` 由服务直接提供 HTTPS。
- **不走代理、内网直连**：把 `.env` 中的 `ELK_HTTP_PUBLISH` 设为 `8765`，即监听所有网卡。
- **需要 `code_*` 源码定位**：用 `ELK_IMAGE_TARGET=code docker compose up -d --build`，并在 `docker-compose.yml` 中打开 `/repos` 和 SSH key 的挂载。服务器上的仓库要能执行 `git fetch`（用只读的 deploy key 即可）。

### 方式二：原生运行（不用 Docker）

先在 `.env` 中打开「原生部署」段，其中的相对路径按 `.env` 所在目录解析。

| 系统 | 做法 |
|---|---|
| Linux | `deploy/service/elk-mcp.service`（systemd），文件头有安装命令 |
| macOS | `deploy/service/com.elk-mcp.plist`（launchd），文件头有安装命令 |
| Windows | 在管理员 PowerShell 中运行 `powershell -ExecutionPolicy Bypass -File deploy\service\install-windows.ps1`（注册为开机自启的计划任务） |

临时试跑，各系统相同：`python3 scripts/mcp_http.py --env-file deploy/.env`（Windows 上用 `python`）。

> 原生 Windows 支持全部 `elk_*` 工具和 `code_locate`。`code_prepare` 需要后台构建 GitNexus 索引，只能在 Docker 或 WSL 上用。服务器上的 ELK 密码只能写 `env:` 引用，`keychain:` 仅 macOS 本机可用。

## 2. 成员管理

```bash
python3 scripts/mcp_http.py gen-token alice
```

命令会输出两样东西：

1. **token**：只显示一次，通过私密渠道发给成员。
2. **一行 JSON**：追加到 `config/tokens.json` 的 `tokens` 数组。

| 字段 | 含义 |
|---|---|
| `prod: true` | 允许查生产。没有此权限时查询生产会被明确拒绝，`code_*` 退回到分支最新提交 |
| `admin: true` | 允许执行 `elk_doctor fix=true`，否则只能诊断 |
| `disabled: true` | 临时停用该成员，删除该行则永久移除 |

修改 `tokens.json` 后自动生效。Docker 部署挂载的是整个目录，编辑器替换文件也能被感知。

## 3. 成员客户端配置

先把自己的 token 设成环境变量 `ELK_MCP_TOKEN`，这样配置文件里就不用写明文 token：

| 系统 | 命令（设置后重开终端或客户端） |
|---|---|
| macOS（zsh） | `echo 'export ELK_MCP_TOKEN=elk_xxx' >> ~/.zshrc` |
| Linux（bash） | `echo 'export ELK_MCP_TOKEN=elk_xxx' >> ~/.bashrc` |
| Windows | `setx ELK_MCP_TOKEN "elk_xxx"` |

> 以下示例中的 `https://elk-mcp.example.com/mcp` 请换成实际地址。之前在本机用过 stdio 版 elk 的成员，请先在 cc-switch 或 `claude mcp remove elk` 中移除旧条目，避免同名冲突。

### Claude Code（Windows / macOS / Linux 命令相同）

```bash
claude mcp add --transport http -s user elk https://elk-mcp.example.com/mcp --header 'Authorization: Bearer ${ELK_MCP_TOKEN}'
```

- 单引号保证写入的是 `${ELK_MCP_TOKEN}` 这个字面量，Claude Code 连接时才展开。Windows 的 cmd 不认单引号，要改用双引号；PowerShell 可以直接用单引号。
- 验证：`claude mcp list` 中出现 `elk: ... - ✔ Connected`。
- 也可以把配置放进项目的 `.mcp.json` 提交到仓库，团队共享；每个成员首次打开时需确认一次：

```json
{ "mcpServers": { "elk": { "type": "http", "url": "https://elk-mcp.example.com/mcp",
  "headers": { "Authorization": "Bearer ${ELK_MCP_TOKEN}" } } } }
```

### cc-switch

新增 MCP，类型选 `http`，URL 填 `https://elk-mcp.example.com/mcp`，headers 填 `{"Authorization": "Bearer ${ELK_MCP_TOKEN}"}`，再同步到 Claude Code 或 Codex。

### Codex CLI（`~/.codex/config.toml`，Windows 在 `%USERPROFILE%\.codex\config.toml`）

```toml
[mcp_servers.elk]
url = "https://elk-mcp.example.com/mcp"
bearer_token_env_var = "ELK_MCP_TOKEN"
```

### Cursor（`~/.cursor/mcp.json`，Windows 在 `%USERPROFILE%\.cursor\mcp.json`）

```json
{ "mcpServers": { "elk": { "url": "https://elk-mcp.example.com/mcp",
  "headers": { "Authorization": "Bearer ${env:ELK_MCP_TOKEN}" } } } }
```

### Claude Desktop（macOS / Windows，需要 Node.js）

配置文件位置：macOS 在 `~/Library/Application Support/Claude/claude_desktop_config.json`，Windows 在 `%APPDATA%\Claude\claude_desktop_config.json`。

```json
{ "mcpServers": { "elk": { "command": "npx",
  "args": ["-y", "mcp-remote", "https://elk-mcp.example.com/mcp", "--header", "Authorization:${AUTH}"],
  "env": { "AUTH": "Bearer elk_xxx" } } } }
```

`Authorization:${AUTH}` 冒号两边不要留空格，否则 Windows 上参数会被拆开。

## 4. 安全要点

- **必须走 HTTPS**（或只在 VPN / 内网中开放）。token 是 Bearer 凭据，明文 HTTP 下可以被嗅探。
- 生产环境建议在 Kibana 中**单独建一个只读账号**，并配置 `max_size`。服务端的 `prod` 开关防的是误用，不能代替 ES 侧的权限控制。
- `.env`、`config/envs.json`、`config/tokens.json` 已加入 `.gitignore`；`.dockerignore` 保证它们不会进入构建上下文。
- 成员离职时，删除 `tokens.json` 中对应的行即可，立即生效。

## 5. 运维

| 事项 | 命令 |
|---|---|
| 看日志（每次工具调用一行：成员、工具、环境、耗时） | `docker compose logs -f elk-mcp`、`journalctl -u elk-mcp -f`，或 Windows 上的 `deploy\data\elk-mcp.log` |
| 结构化调用日志 | 容器内 `/data/cache/mcp.log`（不含参数值和日志内容） |
| 升级 | `git pull && docker compose up -d --build`，成员侧无需任何改动 |
| 服务端自检 | 用 admin token 调用 `elk_doctor`，或 `docker compose exec elk-mcp python /app/scripts/elk.py doctor` |
| 客户端连不上 | 先 `curl https://<域名>/healthz`；返回 401 说明 token 错误或已停用；再用 `claude mcp list` 查看具体报错 |
