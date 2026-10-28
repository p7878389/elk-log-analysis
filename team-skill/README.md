# 团队版 skill

给只连接团队共享 elk MCP 的成员使用：一个压缩包，解压即可，不需要 clone 本仓库、不需要本机脚本。

## 管理员：生成压缩包

```bash
python3 team-skill/build.py --url http://<服务地址>:8765/mcp
```

生成 `elk-log-analysis-team.zip`，内含 `elk-log-analysis/SKILL.md` 与 `references/query-cookbook.md`，服务地址已填好（`gitnexus-remote` 地址按 `…/gitnexus/mcp` 推导）。压缩包含内部地址，只在团队内部分发，不要提交到本仓库。修改 `SKILL.team.md` 后重新生成。

## 成员：导入

| 客户端 | 做法 |
|---|---|
| Claude Code | 解压到 `~/.claude/skills/`，得到 `~/.claude/skills/elk-log-analysis/SKILL.md`；重开会话生效 |
| Codex | 解压到 `~/.codex/skills/` |
| claude.ai / Claude Desktop | 设置 → Capabilities → Skills 中上传该 zip |

导入后按 SKILL.md 第 0 节配置 `ELK_MCP_TOKEN` 与两个 MCP（`elk`、`gitnexus-remote`）。已用 `install.sh` 安装过完整版的成员无需再导入（同名目录，完整版已包含远程模式说明）。
