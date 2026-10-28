# elk-log-analysis

让 Claude Code / Codex 等 AI 编程助手**只读查询 ELK（Elasticsearch / Kibana）日志**，并把异常堆栈定位到对应环境分支的源码，结合 [GitNexus](https://www.npmjs.com/package/gitnexus) 分析根因。

由两部分组成：

- **Skill**（`SKILL.md`）：告诉模型排查日志的流程，比如先聚合看量级、再看明细、再串 traceId 链路，以及生产环境的约束。
- **MCP 服务**（`scripts/`）：提供实际的查询工具。只依赖 Python 3.8+ 标准库，有两种运行方式：
  - **本地 stdio**：每人本机运行。
  - **远程 HTTP**：部署到服务器，团队共用。

## 功能

| 工具 | 作用 |
|---|---|
| `elk_envs` / `elk_ping` / `elk_indices` / `elk_fields` | 查看环境、连通性、索引和字段 |
| `elk_agg` | 聚合统计：TopN、时间趋势 |
| `elk_search` / `elk_trace` | 检索日志明细；按 traceId 串联全链路 |
| `elk_query_dsl` | 自定义 Query DSL，只允许 `_search` / `_count` |
| `code_services` / `code_locate` / `code_prepare` | 从 pod 识别服务；把堆栈定位到部署版本的源码；准备只读 worktree 和 GitNexus 索引 |
| `code_repos` | GitLab 仓库同步状态；管理员可手动触发同步 |
| `elk_doctor` | 自检：配置、凭据、连通性、依赖、缓存和调用日志 |

几条安全约束：

- 只做只读查询。
- 生产环境必须由用户明确要求才会查询。
- 凭据不会出现在工具返回值中。
- 不会改动你本机的代码工作区。

## 安装

clone 仓库后运行安装脚本。不带参数时会逐项询问：选远程还是本地、服务地址、token（输入时不回显）。

```bash
git clone https://github.com/p7878389/elk-log-analysis.git
cd elk-log-analysis
./install.sh
```

Windows 上在 PowerShell 中运行：

```powershell
git clone https://github.com/p7878389/elk-log-analysis.git
cd elk-log-analysis
powershell -ExecutionPolicy Bypass -File install.ps1
```

安装脚本默认使用 **CC Switch 统一管理 MCP 和 Skills**：

1. **生成导入页面**：默认保存到 `~/.config/elk-log-analysis/cc-switch-import.html`，可能含个人 token，文件限制为本人可读写。
2. **导入 MCP**：打开页面并点击 MCP 链接，在 CC Switch 确认，勾选 Claude Code / Codex，由 CC Switch 同步客户端配置。
3. **安装 Skill**：点击 Skill 链接添加来源仓库，再在 CC Switch 的 Skills 页安装 `elk-log-analysis` 并启用目标应用。

安装脚本不直接修改客户端配置或复制客户端 Skill；CC Switch 是这些配置的管理入口。
本地 stdio 模式使用 CC Switch 的 Skill 目录运行脚本，请先安装 Skill，再启用 MCP；
若 CC Switch 使用自定义数据目录，需要在 MCP 条目中调整脚本路径。

### 两种调用方式

| | 远程（推荐） | 本地 |
|---|---|---|
| 原理 | 连接团队部署的 HTTP 服务 | 本机运行 `scripts/mcp_server.py`，直接连 ELK |
| 需要什么 | 服务地址和个人 token（向管理员索取） | 自己配置 ELK 地址和凭据 |
| 命令 | `./install.sh --mode remote --url https://<服务地址>/mcp` | `./install.sh --mode local --code-root ~/code` |
| 生产权限 | 由管理员按人控制 | 取决于你自己的 ELK 账号 |

如果服务端开启了 GitNexus，远程模式会一并注册 `gitnexus-remote`，用来直接查询服务端预建的调用链和影响面索引，本机不需要 clone 任何代码。

远程模式在生成导入页前验证 token，无效时不写文件。本地模式会检测 GitNexus 和 Node，结果包含在 MCP 导入配置中；ELK 环境配置在 CC Switch 的同一 MCP 条目中管理，见 [references/mcp-config.md](references/mcp-config.md)。

### 常用选项

| 选项 | 作用 |
|---|---|
| `--manager cc-switch` | 默认值：只生成 CC Switch 导入页面，由其管理和分发 |
| `--ccswitch-output <路径>` | 指定导入页面保存位置（含凭据，请勿分享） |
| `--skill-repo owner/repo --skill-branch main` | 指定 Skill 来源，与 MCP 查询代码时的 `branch` 无关 |
| `--manager direct` | 显式使用旧版直接安装，不适用于统一管理流程 |
| `--token-stdin` | 从标准输入读取 token，适合脚本批量安装 |
| `--token-store env` | 配置中只引用环境变量 `ELK_MCP_TOKEN`，不写入 token 本身 |
| `--clients claude,codex` | 指定要配置的客户端（默认自动检测） |
| `--gitnexus <路径>` | 本地模式：指定 gitnexus 的可执行文件或安装目录（npm 全局目录、nvm 版本目录、gitnexus 包目录均可）。不填则自动检测，检测不到时会询问 |
| `--node <路径>` | 本地模式：指定运行 gitnexus 的 node（要求 ≥22.18）。不填则自动选择 |
| `--link` | 仅 direct 模式：用符号链接/junction 指向仓库 |
| `--dry-run` | 只显示将要执行的操作，不写入任何文件 |
| `-y` | 非交互，已有条目直接替换（会先备份） |
| `--uninstall [--purge]` | cc-switch 模式提示在管理器中卸载；direct 模式清理旧版直接安装 |

**更新**：在 CC Switch 更新 Skill；MCP 地址、token、启用状态均在 CC Switch 编辑。公开导入协议遇到同名 MCP 时只合并应用开关，不覆盖已有配置。

**从旧版迁移**：在 CC Switch 的 MCP / Skills 页面使用「从应用导入」接管已有条目，再勾选目标应用；之后不再使用 `--manager direct` 修改同名项，避免配置被覆盖。

其他客户端（Cursor、Claude Desktop 等）的手动配置方法，见 [deploy/README.md](deploy/README.md#3-成员客户端配置)。

## 部署远程服务

提供 Docker Compose（可选 Caddy 自动 HTTPS），以及 Linux systemd、macOS launchd、Windows 计划任务的原生部署方式。每个成员一个 token，可以按人控制生产权限和源码权限。

服务端可以**定时从 GitLab 同步仓库**（自建实例也支持）：自动列出令牌有权限的项目，用部分 clone 拉取，并可预建 GitNexus 索引。生产出问题时，代码已经在服务器上准备好了，谁都不用临时去 clone。详见 [deploy/README.md](deploy/README.md)。

## 文档

- [SKILL.md](SKILL.md)：模型使用的排查流程
- [references/mcp-config.md](references/mcp-config.md)：配置项说明
- [references/query-cookbook.md](references/query-cookbook.md)：Lucene 查询示例
- [deploy/README.md](deploy/README.md)：团队部署与成员接入

## License

[Apache-2.0](LICENSE)
