# ELK 查询速查（MCP 参数写法）

## Lucene（`query` 参数）
- 短语：`"订单创建失败"`
- 与或非：`timeout AND (pay OR refund) NOT health`
- 字段：`message:"Connection refused" AND level:ERROR`
- 通配：`exception_class:*SQLException`（生产环境避免前导通配，按主机/应用过滤用 `host` / `service` 参数）
- 范围：`duration:>3000`、`status:[500 TO 599]`
- 需要转义的字符：`+ - = && || > < ! ( ) { } [ ] ^ " ~ * ? : \ /`

## 典型排查场景

1. 某时间段接口 500：
   `elk_search {env:"prod", confirm_production:true, query:"\"/api/order/create\" AND 500", since:"2026-09-23 14:00", until:"2026-09-23 14:20"}`
2. 某用户/订单号的全部日志（默认按时间升序取最早 N 条）：
   `elk_search {query:"\"ORD202609230001\"", since:"1d", size:200}`
3. 异常突增：先看趋势再看 Top 异常：
   `elk_agg {interval:"5m", level:"ERROR", since:"3h"}`
   `elk_agg {field:"logger_name.keyword", level:"ERROR", since:"30m", top:10}`
4. 慢请求：
   `elk_search {query:"duration:>3000", since:"1h", terms:{"app_name":"clinic-client"}}`
5. 某台实例是否异常：
   `elk_agg {field:"host.name", level:"ERROR", since:"1h"}`
6. 堆栈定位到源码并看调用链：
   `elk_search {level:"ERROR", query:"NullPointerException", full:true, size:3}` → 取异常段
   `code_locate {host:"<pod 名>", stack:"<异常段>"}`
   `code_prepare {host:"<pod 名>"}` → 用返回的 `repo` 调 `gitnexus-remote` 的 `context` / `impact`

## 环境差异提醒
- 不同环境字段映射可能不同，先用 `elk_fields {grep:"..."}` 核对字段名。
- 不传 `env` 时使用默认环境（`elk_envs` 中标 default 的那个），回复中写明环境。
