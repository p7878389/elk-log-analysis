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

- **文件权限**：容器以 uid 10001 运行，`config/` 下的文件必须让它可读。推荐执行 `sudo chown 10001:10001 config/*.json && chmod 600 config/*.json`，也就是只有容器用户可读。用 `chmod 644` 也能运行，但日志里会有权限提示；这两个文件本身不含密码。密码放在 `.env` 里，`.env` 只由宿主机上的 compose 读取，保持 600 即可。
- **HTTPS**：服务器有公网域名时，在 `.env` 中设置 `ELK_DOMAIN`，然后运行 `docker compose --profile tls up -d --build`，Caddy 会自动申请证书。纯内网的做法见 `Caddyfile` 中的注释，也可以用 `ELK_HTTP_CERT`/`ELK_HTTP_KEY` 由服务直接提供 HTTPS。
- **不走代理、内网直连**：把 `.env` 中的 `ELK_HTTP_PUBLISH` 设为 `8765`，即监听所有网卡。
- **需要源码定位和调用链分析**：用 `ELK_IMAGE_TARGET=code docker compose up -d --build`，并配置 GitLab 仓库同步，见[第 4 节](#4-gitlab-仓库同步与服务端-gitnexus)。

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
| `admin: true` | 允许执行 `elk_doctor fix=true`、手动触发仓库同步（`code_repos sync=true`），否则只能查看 |
| `code: false` | 禁止查看源码：`code_*` 和 `/gitnexus/mcp` 都会被拒绝（默认允许） |
| `disabled: true` | 临时停用该成员，删除该行则永久移除 |

修改 `tokens.json` 后自动生效。Docker 部署挂载的是整个目录，编辑器替换文件也能被感知。

## 3. 成员客户端配置

**推荐直接用安装脚本**：clone 仓库后运行 `./install.sh --mode remote --url https://elk-mcp.example.com/mcp`（Windows 上用 `install.ps1`），会自动安装 skill、写入 Claude Code 和 Codex 的配置，并设置好文件权限，详见[根目录 README](../README.md#安装)。下面是手动配置的方法，供其他客户端或不方便运行脚本时参考。

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

## 4. GitLab 仓库同步与服务端 GitNexus

服务端可以定时从 GitLab（包括自建实例）同步仓库，让 `code_*` 随时有代码可用。成员通过 `gitnexus-remote` 直接查询服务端预建的调用链索引，**成员电脑上不需要 clone 任何仓库**。

```
GitLab API ──列出令牌有权限的项目──▶ 部分 clone / fetch 到 /repos ──▶ code_locate / code_services
                                          └─(可选) 预建 prod 分支 GitNexus 索引 ──▶ /gitnexus/mcp ──▶ 成员的 gitnexus-remote
```

### 配置

1. **选择认证方式**，在 `.env` 中填写对应的凭据（各方式的完整写法见 `.env.example`）：

   GitLab 的 API（用来列出项目）只接受令牌或账号密码；**SSH 只能用来拉代码**。所以认证分两部分：API 凭据，以及拉代码的协议。

   | 方式 | `.env` 配置 | 说明 |
   |---|---|---|
   | **自动检测**（什么都不填） | — | API：读取本机 git 凭据管理器里已保存的该 GitLab 账号（macOS 钥匙串、Windows 凭据管理器、Linux store/cache），像令牌的按令牌验证，否则按密码验证。拉代码：**先试 SSH**（沿用本机 `~/.ssh` 配置和 ssh-agent），不通再用 HTTPS。适合原生部署在自己电脑上、并且以前用 git 登录过 GitLab 的情况 |
   | **访问令牌**（推荐） | `ELK_GITLAB_TOKEN` | 个人、组或项目令牌都可以，权限选 `read_api` + `read_repository`；可设有效期，随时吊销 |
   | **账号密码** | `ELK_GITLAB_USERNAME` + `ELK_GITLAB_PASSWORD` | GitLab API 不接受密码直接访问，服务会先用 OAuth 密码模式换取临时令牌，过期前自动续期。**账号开启了双因素认证、或只能通过 SSO 登录时不可用**。较新版本的 GitLab 可能关闭了密码授权，或要求提供 OAuth 应用（`ELK_GITLAB_CLIENT_ID/SECRET`），无法满足时，会提示改用令牌 |
   | **OAuth 令牌** | `ELK_GITLAB_AUTH=oauth` + `ELK_GITLAB_TOKEN` | 适合已有统一签发令牌的系统；令牌过期需要外部更新 |

   - **拉取协议** `ELK_GITLAB_GIT_PROTOCOL`：默认 `auto`，**SSH 优先**，不通再用 HTTPS；也可以固定为 `ssh` 或 `https`。优先 SSH 的原因：
     - 密钥不会过期，改密码也不影响。
     - 不受双因素认证、SSO，以及管理员关闭「HTTPS 密码拉取」的影响。
     - 程序里不需要接触明文密码。

     常见的失败原因是公司网络封了 22 端口，这时会自动改用 HTTPS。容器里要用 SSH 时，需要设置 `ELK_GITLAB_SSH_KEY`：把私钥放在 `config/` 下，属主改为 10001，权限设为 600。切换协议后，已同步仓库的 origin 会自动改写。
   - **只读承诺**：自动检测只读取凭据管理器和 `~/.ssh`，不做任何修改。访问 GitLab 时，git 的凭据管理器调用被关闭，改由程序注入凭据，所以认证失败时 git 也不会删除你保存的账号（git 默认会删）。检测过程不会弹出登录或授权窗口。
   - **Docker 部署**：容器里看不到宿主机的钥匙串和 `~/.ssh`，自动检测一般找不到凭据，请用令牌或账号密码显式配置。
   - **凭据放在文件里**：每个凭据变量都可以改成 `<变量>_FILE` 指向一个文件，兼容 Docker secrets。
2. **填写同步范围等其他选项**（可选）：
   ```ini
   ELK_GITLAB_URL=https://gitlab.example.com
   ELK_GITLAB_GROUPS=backend          # 只同步这些组（含子组）
   ELK_REPO_SYNC_INDEX=prod           # 同步后预建 prod 分支索引
   ```
3. **验证凭据**：`--check` 会验证 API 凭据，并**分别实测 SSH 和 HTTPS 两种拉取方式**，报告各自结果以及同步时会选用哪一种：
   ```bash
   docker compose run --rm elk-mcp python /app/scripts/elk.py repo-sync --check
   ```
   原生部署时执行 `python3 scripts/elk.py --env-file deploy/.env repo-sync --check`。
4. **启动**：执行 `ELK_IMAGE_TARGET=code docker compose up -d --build`。服务启动约 15 秒后开始第一次同步，之后每 6 小时同步一次。

### 同步规则

| 情况 | 处理方式 |
|---|---|
| 同步范围 | 令牌账号是成员的全部项目，或 `ELK_GITLAB_GROUPS` 指定的组；自动跳过归档项目、空仓库，以及 `ELK_GITLAB_EXCLUDE` 命中的项目 |
| 拉取方式 | 部分 clone（`--filter=blob:none`）：只下载提交和目录结构，文件内容用到时才下载，几十个仓库也只占很少的空间 |
| 目录命名 | 目录名用项目的 `path`；不同组下有同名项目时，改用 `组_子组_项目` |
| 本脚本 clone 的仓库 | 每次同步都强制对齐到远程默认分支 |
| `/repos` 下已有的同一仓库 | 只执行 fetch，不改动其工作区 |
| 已无权限或已删除的项目 | 只在状态中标记，**不自动删除**，需要时手动删除目录 |
| 凭据安全 | 令牌、密码和换来的临时令牌只存在于服务进程的内存里。通过环境变量传给 git，只发给 `ELK_GITLAB_URL` 这个地址；不写入 `.git/config` 和 remote URL，也不出现在命令行参数和日志中。换令牌失败后，5 分钟内不会重试，避免在 GitLab 连不上时反复卡住 |

### 预建索引

- `ELK_REPO_SYNC_INDEX=prod` 会在每次同步后，**逐个**为仓库的 prod 分支（master/main）构建或增量更新 GitNexus 索引。逐个进行是为了不压垮机器；已是最新的索引会跳过。
- 每个仓库的索引约占 0.5G，首次构建每个需要几十秒到几分钟。仓库很多时，可以用 `ELK_REPO_SYNC_INDEX_REPOS` 只为核心服务建索引。其余仓库在成员第一次调用 `code_prepare` 时再按需构建。
- 服务端的 GitNexus 为**只读**：`rename`、`group_sync` 等会改写代码或数据的工具会被拒绝。

### ⚠️ 源码可见范围

同步下来的代码，**所有有 `code` 权限的成员都能通过 `code_locate` 和 `gitnexus-remote` 看到**，即使这个人在 GitLab 上本来没有该仓库的权限。

所以团队共享时**不要用个人账号的令牌**，也不要依赖「自动检测」读取你本机保存的个人账号。建议新建一个专门的服务账号（比如 `elk-bot`），只把它加入团队需要排查的项目组，再用它的令牌；或者用 `ELK_GITLAB_GROUPS` 限定同步范围。对不应接触源码的成员，在 `tokens.json` 中设置 `"code": false`。

### 运维

| 事项 | 命令 |
|---|---|
| 查看同步状态 | 在客户端让模型调用 `code_repos`；或执行 `docker compose exec elk-mcp python /app/scripts/elk.py repo-sync --status` |
| 立即同步 | 管理员调用 `code_repos` 并传入 `sync=true`；或执行 `docker compose exec elk-mcp python /app/scripts/elk.py repo-sync` |
| 验证凭据 | `docker compose exec elk-mcp python /app/scripts/elk.py repo-sync --check` |
| 预览同步范围 | `docker compose exec elk-mcp python /app/scripts/elk.py repo-sync --dry-run` |
| 同步日志 | 容器内 `/data/cache/repo-sync/sync.log`；GitNexus 服务日志在 `/data/cache/gitnexus-mcp.log` |
| 原生部署 | `python3 scripts/elk.py --env-file deploy/.env repo-sync`；定时同步由 `mcp_http.py` 内置，不需要另配 cron |

## 5. 安全要点

- **必须走 HTTPS**（或只在 VPN / 内网中开放）。token 是 Bearer 凭据，明文 HTTP 下可以被嗅探。
- 生产环境建议在 Kibana 中**单独建一个只读账号**，并配置 `max_size`。服务端的 `prod` 开关防的是误用，不能代替 ES 侧的权限控制。
- `.env`、`config/envs.json`、`config/tokens.json` 已加入 `.gitignore`；`.dockerignore` 保证它们不会进入构建上下文。
- 成员离职时，删除 `tokens.json` 中对应的行即可，立即生效。

## 6. 运维

| 事项 | 命令 |
|---|---|
| 看日志（每次工具调用一行：成员、工具、环境、耗时） | `docker compose logs -f elk-mcp`、`journalctl -u elk-mcp -f`，或 Windows 上的 `deploy\data\elk-mcp.log` |
| 结构化调用日志 | 容器内 `/data/cache/mcp.log`（不含参数值和日志内容） |
| 升级 | `git pull && docker compose up -d --build`，成员侧无需任何改动 |
| 服务端自检 | 用 admin token 调用 `elk_doctor`，或 `docker compose exec elk-mcp python /app/scripts/elk.py doctor` |
| 客户端连不上 | 先 `curl https://<域名>/healthz`；返回 401 说明 token 错误或已停用；再用 `claude mcp list` 查看具体报错 |
