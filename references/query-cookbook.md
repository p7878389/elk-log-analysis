# ELK 查询速查

## Lucene（`-q`）
- 短语：`"订单创建失败"`
- 与或非：`timeout AND (pay OR refund) NOT health`
- 字段：`message:"Connection refused" AND level:ERROR`
- 通配：`exception_class:*SQLException`
- 范围：`duration:>3000`、`status:[500 TO 599]`
- 需要转义的字符：`+ - = && || > < ! ( ) { } [ ] ^ " ~ * ? : \ /`

## 典型排查场景

> 以下为命令行写法（MCP 不可用时的兜底）；MCP 工具参数同名：`-q`→`query`、`--since`→`since`、`--term a=b`→`terms:{a:b}`、`agg <field>`→`field`。

```bash
ELK="python3 ~/.claude/skills/elk-log-analysis/scripts/elk.py"

# 1. 某时间段接口 500
$ELK -e prod search -q '"/api/order/create" AND 500' --since '2026-09-23 14:00' --until '2026-09-23 14:20'

# 2. 某用户/订单号的全部日志（默认按 time.keyword 升序）
$ELK -e prod search -q '"ORD202609230001"' --since 1d -n 200

# 3. 异常突增：先看趋势再看 Top 异常
$ELK -e prod agg --interval 5m --level ERROR --since 3h
$ELK -e prod agg logger_name.keyword --level ERROR --since 30m --top 10

# 4. 慢请求
$ELK -e prod search -q 'duration:>3000' --since 1h --term app_name=clinic-client

# 5. 某台实例是否异常
$ELK -e prod agg host.name --level ERROR --since 1h

# 6. 复杂 DSL：先 --dsl 生成再改，最后 raw 执行
$ELK -e prod search --level ERROR --since 1h --dsl > /tmp/q.json
$ELK -e prod raw POST '/clinic-prod-*/_search' @/tmp/q.json
```

## 环境差异提醒
- 不同环境字段映射可能不同（例如 k8s 环境的服务名在 `kubernetes.labels.app`），在 `envs.json` 的环境内 `fields` 覆盖。
- 生产索引通常按天滚动，时间窗口越小越快。
