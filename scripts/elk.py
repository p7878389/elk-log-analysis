#!/usr/bin/env python3
"""ELK 日志查询 CLI（只读）。供人工调试/兜底使用；Claude 优先使用 MCP 工具。"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envfile  # noqa: E402

envfile.apply_from_argv()  # 服务端原生部署：elk.py --env-file deploy/.env repo-sync
import code_core as code  # noqa: E402
import diag  # noqa: E402
import elk_core as core  # noqa: E402
import repo_sync  # noqa: E402


def add_filters(p):
    p.add_argument("-q", "--query", help="Lucene query_string，如 'NullPointerException AND order'")
    p.add_argument("--since", default="15m", help="起始：15m/2h/1d 或 '2026-09-23 10:00'")
    p.add_argument("--until", default="now", help="结束，默认 now")
    p.add_argument("--level", help="日志级别，逗号分隔：ERROR,WARN")
    p.add_argument("--service", help="应用名，逗号分隔")
    p.add_argument("--host", help="主机/Pod")
    p.add_argument("--logger", help="logger 名")
    p.add_argument("--trace", help="traceId")
    p.add_argument("--term", action="append", default=[], help="字段精确过滤 field=value，可重复")
    p.add_argument("-i", "--index", help="覆盖环境默认索引")
    p.add_argument("--dsl", action="store_true", help="只打印 DSL 不执行")


def add_output(p):
    p.add_argument("-n", "--size", type=int, default=50)
    p.add_argument("--desc", action="store_true", help="取最新 N 条（默认按 time.keyword 升序取最早 N 条）")
    p.add_argument("--full", action="store_true", help="完整消息与堆栈")
    p.add_argument("--source", dest="show_source", action="store_true", help="--full 时显示 _index/_id")
    p.add_argument("--json", action="store_true", help="输出原始 _source JSON")
    p.add_argument("--width", type=int, default=300, help="单行消息截断长度")
    p.add_argument("--columns", help="展示列，逗号分隔，默认 time.keyword,level,host.name,traceId,message")


def to_params(args):
    p = dict(vars(args))
    terms = {}
    for kv in p.pop("term", None) or []:
        if "=" not in kv:
            raise core.ElkError("--term 格式为 field=value")
        k, v = kv.split("=", 1)
        terms[k] = v
    p["terms"] = terms
    return p


def run_code(args, cfg):
    ccfg = code.load_code_config()
    a = {k: v for k, v in vars(args).items() if v is not None}
    if args.cmd == "code-services":
        return code.op_services(ccfg, a, core.get_env(cfg, args.env) if args.env else None)
    if args.cmd == "code-gc":
        return code.op_gc(ccfg, args.dry_run, args.days, args.exact_days)
    env = core.get_env(cfg, args.env)
    allow_elk = not env.get("production") or args.confirm_production
    tz = core.parse_tz(args.tz)
    if args.cmd == "code-locate":
        a["stack"] = sys.stdin.read() if args.stack == "-" else open(args.stack, encoding="utf-8").read()
        return code.op_locate(ccfg, env, a, tz, allow_elk)
    a["index"] = not args.no_index
    return code.op_prepare(ccfg, env, a, tz, allow_elk)


def add_code_target(p):
    p.add_argument("--host", help="host.name（pod 名）")
    p.add_argument("--service", help="应用名或模块名")
    p.add_argument("--repo", help="仓库名")
    p.add_argument("--module", help="模块相对路径（配合 --repo）")
    p.add_argument("--align", choices=["deploy", "head"], help="默认 deploy：按部署时间对齐 commit")
    p.add_argument("--commit", help="显式指定 commit/ref")
    p.add_argument("--fetch", action="store_true", default=None, help="忽略节流强制 fetch")
    p.add_argument("--exact", action="store_true", default=None,
                   help="worktree/索引按部署 commit 单独建立（默认跟随分支 HEAD）")
    p.add_argument("--refresh", action="store_true", default=None,
                   help="强制把 worktree 推进到分支 HEAD（默认使用中不推进）")
    p.add_argument("--confirm-production", action="store_true",
                   help="prod 下允许查询日志推断部署时间（仅用户明确要求时）")


def run(args):
    if args.cmd == "config-export":
        if not args.no_mcp_env:
            diag.apply_mcp_env()
        cfg = core.load_config()
        arr = diag.export_array(cfg, not args.keep_globals)
        refs = sorted({item[k][4:] for item in arr for k in ("password", "api_key")
                       if str(item.get(k, "")).startswith("env:")})
        obj = {"environments": arr}
        if cfg.get("default_env") and not any(item.get("default") for item in arr):
            obj = {"default_env": cfg["default_env"], "environments": arr}
        doc = json.dumps({"elk": obj}, ensure_ascii=False, indent=2)
        # env 的值只能是字符串（Claude Code 会拒绝对象/数组），所以环境配置放在 elk MCP 条目顶层的 elk 字段
        return ("# 推荐写法：在 cc-switch 中编辑 elk MCP 的 JSON，把下面的 elk 字段加到顶层（与 type/command/env 同级）。\n"
                "# 1. <用户名：沿用原值> 替换为原值；password/api_key 的 env: 引用可直接替换为真实密码，\n"
                "#    或保留引用并在 elk MCP env 中放同名字符串变量：%s\n"
                "# 2. elk MCP env 中只保留 ELK_CODE_*（及上面保留的凭据变量），删除 ELK_ENVIRONMENTS、ELK_ENVS 与其余 ELK_<ENV>_* 变量%s\n"
                "#    ——它们存在时 elk 字段不会被读取。\n"
                "# 3. 删除 %s（如有），然后在 Claude Code / Codex 中重连 elk MCP。\n\n%s"
                % (", ".join(refs) or "（无）",
                   "" if args.keep_globals else "，以及已写进每个环境的全局 ELK_<KEY>",
                   core.CONFIG_PATH.replace(os.path.expanduser("~"), "~", 1), doc))
    if args.cmd == "repo-sync":
        scfg = repo_sync.load_sync_config()
        if args.status or scfg is None:
            return repo_sync.describe(scfg)
        if args.no_index:
            scfg["index_envs"] = []
        if args.dry_run:
            ps = repo_sync.plan_dirs(scfg, repo_sync.list_projects(scfg), repo_sync._load_manifest(scfg))
            rows = ["%s %-50s → %s" % ("·" if os.path.exists(os.path.join(scfg["dir"], p["dir"])) else "+",
                                        p["path_with_namespace"], p["dir"]) for p in ps]
            return "\n".join(rows + ["共 %d 个项目（+ 将新 clone，· 已存在将 fetch），目录 %s" % (len(ps), scfg["dir"])])
        repo_sync.run_sync(scfg, progress=lambda m: print(m, file=sys.stderr, flush=True))
        return repo_sync.describe(scfg)
    if args.cmd == "doctor":  # 配置坏了也要能诊断：在 load_config 之前处理
        if not args.no_mcp_env:
            diag.apply_mcp_env()
        return diag.run_doctor(args.fix, not args.no_conn, args.confirm_production, args.days, cli=True)
    cfg = core.load_config()
    if args.cmd == "envs":
        return core.describe_envs(cfg, args.verbose)
    if args.cmd.startswith("code-"):
        return run_code(args, cfg)
    env = core.get_env(cfg, args.env)
    if env.get("production"):
        print("[elk] 当前环境: %s (PRODUCTION，只读)" % env["_name"], file=sys.stderr)
    tz = core.parse_tz(args.tz)

    if args.cmd == "ping":
        return core.op_ping(env)
    if args.cmd == "indices":
        return core.op_indices(env, args.pattern, args.limit)
    if args.cmd == "fields":
        return core.op_fields(env, args.index, args.grep)
    if args.cmd == "raw":
        body = None
        if args.body:
            body = json.loads(open(args.body[1:]).read() if args.body.startswith("@") else args.body)
        return core.op_raw(env, args.method, args.path, body)

    p = to_params(args)
    if args.cmd == "trace":
        p["trace"] = args.trace_id
    if args.cmd in ("search", "trace"):
        if args.dsl:
            return json.dumps(core.search_body(env, p, tz), ensure_ascii=False, indent=2)
        return core.op_search(env, p, tz)
    if args.cmd == "agg":
        if args.dsl:
            return json.dumps(core.agg_body(env, p, tz), ensure_ascii=False, indent=2)
        return core.op_agg(env, p, tz)


def main():
    ap = argparse.ArgumentParser(description="ELK 只读日志查询")
    ap.add_argument("-e", "--env", help="环境名或别名（dev/test/prod…）")
    ap.add_argument("--tz", default="+08:00", help="显示与解析时区，默认 +08:00")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("envs", help="列出已配置环境（不显示凭据）")
    p.add_argument("-v", "--verbose", action="store_true", help="显示每项配置的来源层")
    sub.add_parser("ping", help="连通性检查")
    p = sub.add_parser("indices", help="列出索引")
    p.add_argument("pattern", nargs="?")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("fields", help="查看索引字段")
    p.add_argument("-i", "--index")
    p.add_argument("--grep")

    p = sub.add_parser("search", help="检索日志")
    add_filters(p)
    add_output(p)

    p = sub.add_parser("trace", help="按 traceId 串联全链路日志")
    p.add_argument("trace_id")
    add_filters(p)
    add_output(p)
    p.set_defaults(since="7d", size=500)

    p = sub.add_parser("agg", help="聚合统计：TopN / 时间趋势")
    p.add_argument("field", nargs="?", default="",
                   help="字段名或逻辑名(level/service/logger/host)，需为 keyword 类型")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--interval", help="趋势间隔，如 1m/5m/1h")
    add_filters(p)

    p = sub.add_parser("raw", help="调用只读 API（_search/_count/_mapping/_cat…）")
    p.add_argument("method")
    p.add_argument("path")
    p.add_argument("body", nargs="?", help="JSON 字符串或 @file.json")

    p = sub.add_parser("code-services", help="应用名 ↔ 仓库/模块清单（ELK_CODE_ROOT 下）")
    p.add_argument("--host")
    p.add_argument("--service")
    p.add_argument("--grep")
    p = sub.add_parser("config-export", help="把当前生效配置导出为 cc-switch elk 对象写法（密码改为 env: 引用）")
    p.add_argument("--keep-globals", action="store_true", help="全局 ELK_<KEY> 保留为全局，不写进每个环境")
    p.add_argument("--no-mcp-env", action="store_true", help="不套用 ~/.claude.json 中 elk MCP 的 env")
    p = sub.add_parser("doctor", help="自检：配置、连通性、依赖、缓存、进程版本、日志错误分类")
    p.add_argument("--fix", action="store_true", help="执行安全修复（只涉及缓存与运行时状态）")
    p.add_argument("--no-conn", action="store_true", help="跳过 ELK 连通性与服务映射检查")
    p.add_argument("--confirm-production", action="store_true", help="同时检查生产环境连通性（仅用户明确要求时）")
    p.add_argument("--days", type=int, default=7, help="日志分析天数，默认 7")
    p.add_argument("--no-mcp-env", action="store_true", help="不套用 ~/.claude.json 中 elk MCP 的 env")
    p = sub.add_parser("code-gc", help="清理闲置的缓存 worktree 与 GitNexus 索引")
    p.add_argument("--dry-run", action="store_true", help="只预览不删除")
    p.add_argument("--days", type=float, help="分支 worktree 闲置天数阈值（默认 ELK_CODE_GC_DAYS=10）")
    p.add_argument("--exact-days", type=float, help="按 commit 的 worktree 闲置天数阈值（默认 ELK_CODE_GC_EXACT_DAYS=3）")
    p = sub.add_parser("code-locate", help="把堆栈定位到环境分支源码")
    p.add_argument("stack", help="堆栈文本文件，- 表示从 stdin 读取")
    p.add_argument("--context", type=int)
    p.add_argument("--max-snippets", type=int)
    p.add_argument("--prepare", action="store_true", default=None, help="同时准备 worktree 与 GitNexus 索引")
    add_code_target(p)
    p = sub.add_parser("code-prepare", help="准备只读 worktree 与 GitNexus 索引")
    p.add_argument("--no-index", action="store_true", help="不触发索引构建")
    add_code_target(p)

    p = sub.add_parser("repo-sync", help="从 GitLab 同步有权限的仓库（ELK_GITLAB_URL / ELK_GITLAB_TOKEN）")
    p.add_argument("--status", action="store_true", help="只查看同步状态与已同步仓库")
    p.add_argument("--dry-run", action="store_true", help="只列出将同步的项目与本地目录")
    p.add_argument("--no-index", action="store_true", help="本次不预建 GitNexus 索引")

    args = ap.parse_args()
    try:
        print(run(args))
    except core.ElkError as e:
        print("[elk] %s" % e, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
