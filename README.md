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
| `elk_doctor` | 自检：配置、凭据、连通性、依赖、缓存和调用日志 |

几条安全约束：

- 只做只读查询。
- 生产环境必须由用户明确要求才会查询。
- 凭据不会出现在工具返回值中。
- 不会改动你本机的代码工作区。

## 安装 Skill

**通过 cc-switch**：进入 Skills 页面，添加仓库 `p7878389/elk-log-analysis`，分支填 `main`，然后安装 `elk-log-analysis`。

**手动安装**：

```bash
git clone https://github.com/p7878389/elk-log-analysis.git ~/.cc-switch/skills/elk-log-analysis
```

Windows 上的路径是 `%USERPROFILE%\.cc-switch\skills\elk-log-analysis`。clone 完成后，在 cc-switch 的 Skills 页面扫描并导入本地 skill。不用 cc-switch 的话，clone 到 `~/.claude/skills/elk-log-analysis` 即可。

## 接入 MCP

### 方式一：连接团队的远程服务（推荐）

向管理员要服务地址和个人 token，把 token 设为环境变量 `ELK_MCP_TOKEN`，然后执行：

```bash
claude mcp add --transport http -s user elk https://<服务地址>/mcp --header 'Authorization: Bearer ${ELK_MCP_TOKEN}'
```

cc-switch、Codex、Cursor、Claude Desktop 的配置写法，见 [deploy/README.md](deploy/README.md#3-成员客户端配置)。

### 方式二：本机运行（stdio）

在 cc-switch 中新增一个 MCP，或者直接执行：

```bash
claude mcp add -s user elk -e ELK_CODE_ROOT=~/code -- python3 ~/.cc-switch/skills/elk-log-analysis/scripts/mcp_server.py
```

环境配置（Kibana/ES 地址、索引、凭据、字段映射）的写法，见 [references/mcp-config.md](references/mcp-config.md)。

## 部署远程服务

提供 Docker Compose（可选 Caddy 自动 HTTPS），以及 Linux systemd、macOS launchd、Windows 计划任务的原生部署方式。每个成员一个 token，可以按人控制生产环境权限。详见 [deploy/README.md](deploy/README.md)。

## 文档

- [SKILL.md](SKILL.md)：模型使用的排查流程
- [references/mcp-config.md](references/mcp-config.md)：配置项说明
- [references/query-cookbook.md](references/query-cookbook.md)：Lucene 查询示例
- [deploy/README.md](deploy/README.md)：团队部署与成员接入

## License

[Apache-2.0](LICENSE)
