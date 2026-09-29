# elk MCP 环境配置

环境配置按以下顺序取第一个存在的：

| 写法 | 位置 | 说明 |
|---|---|---|
| 数组（MCP env） | cc-switch → MCP → `elk` 的 env 中的 `ELK_ENVIRONMENTS` | 只能填**单行 JSON 字符串**（env 的值必须是字符串） |
| 平铺（兼容） | MCP env 中的 `ELK_ENVS` + `ELK_<ENV>_<KEY>` | 每个环境的每一项各占一个变量 |
| **cc-switch elk 对象（推荐）** | cc-switch → MCP → `elk` 的 JSON 顶层 `elk` 字段 | 真正的 JSON 对象，所有配置（含密码）都在 cc-switch 中管理 |
| envs.json（兜底） | `~/.config/elk-log-analysis/envs.json`（`ELK_CONFIG` 可改路径） | 密码只能写 `env:` / `keychain:` 引用 |

## cc-switch elk 对象（推荐）

在 cc-switch 中编辑 elk MCP，JSON 写成：

```json
{
  "type": "stdio",
  "command": "/usr/bin/python3",
  "args": ["/Users/<你>/.claude/skills/elk-log-analysis/scripts/mcp_server.py"],
  "env": {"ELK_CODE_ROOT": "~/code"},
  "elk": {
    "default_env": "test",
    "environments": [
      {"env": "test", "url": "http://kibana-test.example.com", "mode": "kibana", "index": "app-test-*",
       "username": "<账号>", "password": "<密码>", "default": true},
      {"env": "stag", "url": "http://kibana-test.example.com", "mode": "kibana", "index": "app-staging-*",
       "username": "<账号>", "password": "<密码>"},
      {"env": "prod", "url": "http://kibana.example.com", "mode": "kibana", "index": "app-prod-*",
       "username": "<账号>", "password": "<密码>", "max_size": 2000}
    ]
  }
}
```

- **为什么不放在 `env` 里**：env 的值只能是字符串。写成对象或数组时，Claude Code 会报 `env.<KEY>: Invalid input` 并跳过整个 elk 服务，cc-switch 同步 Codex 时也会直接丢弃非字符串的值。
- **顶层 `elk` 字段对客户端无害**：cc-switch 同步到 `~/.claude.json` 时原样写入，Claude Code 忽略未知字段；同步到 Codex 时 cc-switch 会跳过这种嵌套对象，`config.toml` 中不会出现（也就不会有密码）。
- **读取方式**：MCP 进程拿不到顶层自定义字段，因此 `mcp_server.py` 启动时以只读方式打开 `~/.cc-switch/cc-switch.db`，读取 `mcp_servers` 表中 id 为 `elk` 的条目。数据库路径和条目 id 可用 `ELK_CCSWITCH_DB` / `ELK_CCSWITCH_ID` 覆盖。
- 修改后需在 Claude Code / Codex 中重连 elk MCP（`/mcp`）才会生效。
- `password` / `api_key` 可以写明文，也可以写 `env:<变量名>` 或 `keychain:<service>/<account>`（见文末）。
- 此时 elk MCP env 只保留 `ELK_CODE_*`；**不能再有 `ELK_ENVIRONMENTS` 或 `ELK_ENVS`**，否则 elk 对象不会被读取。
- 对象顶层可选：`default_env`、`fields`（全局字段映射）、`defaults`（所有环境共用的默认值，键同环境项）。
- **每项的字段**：
  - `env`：必填，环境名。
  - `default: true`：设为默认环境（也可以用顶层 `default_env`）；都不写时取第一项。
  - 其余字段就是下面两张表里的 KEY 改成小写，例如 `url`、`index`、`max_size`、`branch`、`service_map`。
- **值的类型**：
  - `aliases`、`columns` 可以写成数组。
  - `indices`、`fields`、`service_map` 写成对象。
  - 布尔和数字直接写，例如 `"production": true`、`"max_size": 2000`。
- 字段写错（如 `indx`）不会生效，`elk_doctor` 会提示正确写法。
- **从其他写法迁移**：执行下面的命令，它按当前生效的配置生成 elk 对象（明文密码会改为 `env:` 引用，不会输出明文，粘贴到 cc-switch 后再把引用替换为真实密码）：
  ```bash
  python3 ~/.claude/skills/elk-log-analysis/scripts/elk.py config-export
  ```

## envs.json 写法（兜底）

格式与 elk 对象相同（`default_env` / `defaults` / `fields` / `environments`），文件权限设为 600。区别是 `password` / `api_key` **不能写明文**，只能写 `env:<变量名>`（真实密码作为字符串变量放在 elk MCP env 中）或 `keychain:<service>/<account>`。只有 MCP env 和 cc-switch 中都没有配置时才会读取。

## 分层规则

同一项配置按以下顺序取值，**上层覆盖下层**：

| 优先级 | 层 | 写法 | 用途 |
|---|---|---|---|
| 1（最高） | 环境 | `ELK_<ENV>_<KEY>` | 只影响该环境，例如生产的地址和索引 |
| 2 | 全局 | `ELK_<KEY>` | 所有环境共用，例如连接方式和账号 |
| 3 | 级别预设 | 按环境名推断，或用 `ELK_<ENV>_TIER` 指定 | 生产保护、代码分支、版本对齐、别名 |
| 4（最低） | 内置默认 | — | 例如 `MODE=es`、`INDEX=*`、`MAX_SIZE=500` |

数组写法（envs.json 或 `ELK_ENVIRONMENTS`）中，「环境」层就是数组里的那一项，`defaults` / 全局 `ELK_<KEY>` 为全局层。平铺写法中，`<ENV>` 为环境名大写，非字母数字的字符替换为 `_`，例如 `uat-2` → `ELK_UAT_2_URL`。

`elk_envs {verbose:true}`（命令行 `elk.py envs -v`）会逐项显示每个值来自哪一层；`elk_doctor` 会检查拼错的变量名、未启用环境的配置、可删除的重复项，以及可以提到全局的共用项。

### 环境级别预设

| 级别 | 按名称识别 | 生产保护 | 代码分支 | 版本对齐 | 预设别名* |
|---|---|---|---|---|---|
| `prod` | prod / production / prd / live / online / 含“生产”“线上” | **强制开启**（需确认、默认脱敏；`PRODUCTION=false` 会被忽略） | `master,main` | 按部署时间 | 生产, 线上 |
| `staging` | stag / staging / stage / sta / pre / preprod / uat / 含“预发” | 关闭 | `staging` | 按部署时间 | 预发 |
| `test` | test / testing / beta / qa / sit / 含“测试” | 关闭 | `beta` | 按部署时间 | 测试 |
| `dev` | dev / develop / local / 含“开发” | 关闭 | `dev` | 分支最新 | 开发 |

\* 预设别名只在该级别只有一个环境时生效，避免同一个别名指向两个环境；显式配置 `ELK_<ENV>_ALIASES` 时以显式为准。
名称识别不出来的环境需要设置 `ELK_<ENV>_TIER`，或至少设置 `ELK_<ENV>_BRANCH`，否则 code_* 无法确定分支。
名字像生产、实际不是生产的环境：显式设置 `ELK_<ENV>_TIER=test` 或 `staging`。

## 系统变量

| 变量 | 说明 | 示例 |
|---|---|---|
| `ELK_ENVS` | 环境列表，逗号分隔（必填） | `stag,test,prod` |
| `ELK_DEFAULT_ENV` | 未指定环境时使用，**不要设为生产** | `test` |
| `ELK_FIELDS` | 全局字段映射(JSON)。`sort` 排序字段（默认 `time.keyword`，升序）；`time` 展示时间（默认 `time`）；`timestamp` 时间范围过滤/趋势（默认 `@timestamp`，需 date 类型）；`host_keyword` 主机的 keyword 字段（默认 `<host>.keyword`） | `{"trace":"traceId"}` |

## 可全局、也可按环境配置的项（`ELK_<KEY>` 或 `ELK_<ENV>_<KEY>`）

| KEY | 说明 | 示例 / 默认 |
|---|---|---|
| `URL` | 地址（必填）：es 模式填 ES 地址，kibana 模式填 Kibana 地址 | `http://kibana-test.example.com` |
| `MODE` | `es`（直连，默认）/ `kibana`（经 Kibana 代理） | `kibana` |
| `INDEX` | 默认索引，可逗号分隔多个、可通配 | 默认 `*` |
| `INDICES` | 索引别名(JSON)，查询时 `index` 可直接写别名 | `{"clinic":"clinic-*"}` |
| `USERNAME` / `PASSWORD` | Basic 认证；密码写法见文末 | |
| `API_KEY` | API Key 认证。环境级 API_KEY 优先于用户名密码；全局 API_KEY 只在没有用户名时使用 | |
| `MAX_SIZE` | 单次最多返回条数 | 默认 `500` |
| `TIMEOUT` | 超时秒数 | 默认 `60` |
| `MASK` | 是否脱敏手机号/身份证 | 生产默认 `true` |
| `VERIFY_SSL` / `CA_CERT` | 证书校验 / 自签 CA 路径 | 默认校验 |
| `KIBANA_SPACE` / `KIBANA_API` | Kibana Space / 访问方式 `auto`（默认，Console 403 时改走搜索接口）、`console`、`search` | |
| `PROXY` | HTTP 代理；不配则直连（忽略系统代理） | `http://127.0.0.1:7897` |
| `COLUMNS` | 明细展示列 | 默认 `time.keyword,level,host.name,traceId,message` |

## 只能按环境配置的项（`ELK_<ENV>_<KEY>`）

| KEY | 说明 | 示例 |
|---|---|---|
| `TIER` | 环境级别 `dev` / `test` / `staging` / `prod`，覆盖按名称的推断 | `staging` |
| `ALIASES` | 环境别名，逗号分隔 | `生产,线上` |
| `PRODUCTION` | 是否生产。prod 级别强制为 true | `true` |
| `FIELDS` | 该环境字段映射覆盖(JSON)，合并在 `ELK_FIELDS` 之上 | `{"service":"kubernetes.labels.app"}` |
| `BRANCH` | 代码分支，逗号分隔多个候选（多个同时存在时优先 origin/HEAD，其次最近提交） | `release,master` |
| `SERVICE_MAP` | 该环境的 deployment → `仓库[/模块]`(JSON)，合并在 `ELK_CODE_SERVICE_MAP` 之上 | `{"sso":"auth-center/sso-server"}` |
| `ALIGN` | code_* 的默认版本对齐方式：`deploy`（按部署时间）/ `head`（分支最新） | `head` |
| `AUTO_INDEX` | 该环境是否自动建 GitNexus 索引，覆盖 `ELK_CODE_AUTO_INDEX` | `false` |

## 示例：平铺写法下的精简配置

stag、test、prod 共用连接方式和账号，stag 与 test 共用地址。原来 21 个变量，精简后 12 个，解析结果完全一致（已验证）：

```
ELK_ENVS=stag,test,prod
ELK_DEFAULT_ENV=test
ELK_CODE_ROOT=~/code

# 全局：所有环境共用
ELK_MODE=kibana
ELK_URL=http://kibana-test.example.com
ELK_USERNAME=<账号>
ELK_PASSWORD=<密码或 keychain:...>

# 环境：只写不同的部分（级别、分支、别名、生产保护由级别预设提供）
ELK_TEST_INDEX=app-test-*
ELK_STAG_INDEX=app-staging-*
ELK_PROD_URL=http://kibana.example.com
ELK_PROD_INDEX=app-prod-*
ELK_PROD_MAX_SIZE=2000
```

新增环境：在 `ELK_ENVS` 中加名字，再只补与全局不同的 `ELK_<名字>_*` 变量。例如新增 `uat`：会被识别为 staging 级别，分支取 `staging`；只需补 `ELK_UAT_INDEX`，如果地址不同再补 `ELK_UAT_URL`。

## 代码分析（code_* 工具）

| 变量 | 说明 | 示例 / 默认 |
|---|---|---|
| `ELK_CODE_ROOT` | 代码根目录（**必填**，否则 code_* 不可用）。多个用逗号或冒号分隔，按顺序扫描。每项可以是仓库的父目录（扫描其下一级子目录），也可以直接是某个仓库。同一真实路径只计一次；不同目录下的同名仓库会改名为 `<上级目录名>_<仓库名>`（如 `code_api`、`work_api`），worktree 和 GitNexus 的 repo 名也随之改变 | `~/code,~/work/projects,~/code/some-repo` |
| `ELK_CODE_BRANCHES` | 旧写法：环境 → 远程分支(JSON)。仍兼容，优先级介于 `ELK_<ENV>_BRANCH` 与级别预设之间；新配置请用 `ELK_<ENV>_BRANCH` | `{"test":"beta"}` |
| `ELK_CODE_SERVICE_MAP` | 所有环境共用的 deployment → `仓库[/模块]`(JSON)；某个环境特有的映射写在 `ELK_<ENV>_SERVICE_MAP` | `{"saas-getway":"ruoyi-cloud-plus/saas-gateway"}` |
| `ELK_CODE_CACHE` | worktree、索引日志、锁的缓存目录 | 默认 `~/.cache/elk-log-analysis` |
| `ELK_CODE_FETCH_TTL` | 同一仓库 fetch 节流秒数 | 默认 `600` |
| `ELK_CODE_LEASE` | 副本被 `code_prepare` 使用后，多少秒内即使分支有新提交也不推进（保护进行中的分析）；`refresh=true` 可强制推进 | 默认 `1800` |
| `ELK_CODE_GC_DAYS` | 分支 worktree（`<仓库>@<分支>`）闲置多少天后自动删除（连同 GitNexus 索引）；`0` 关闭 | 默认 `10` |
| `ELK_CODE_GC_EXACT_DAYS` | 按 commit 的 worktree（`<仓库>@<commit>`，exact 模式产生）闲置多少天后自动删除；`0` 关闭 | 默认 `3` |
| `ELK_CODE_AUTO_INDEX` | `code_prepare` 时是否自动后台构建/增量更新 GitNexus 索引 | 默认 `true` |
| `ELK_CODE_GITNEXUS` | gitnexus 可执行文件 | 默认 PATH 中的 `gitnexus`，其次 `/opt/homebrew/bin/gitnexus` |

worktree 与 GitNexus 索引默认按「仓库@分支」各维护一份，跟随分支 HEAD。`exact=true` 时按「仓库@commit」额外建立（闲置 3 天后自动清理）。每份索引约 0.5G（order-platform 实测 568M）。

### 自动清理

- **什么时候触发**：code_* 工具被调用时顺带检查，每天最多一次，在后台线程执行，不阻塞当前查询。
- **判定闲置**：以下时间中最新的一个，距今超过阈值即视为闲置——`code_prepare` 的最近使用时间、索引构建/完成时间、worktree 创建时间。直接调用 GitNexus MCP 查询不会被记录。
- **删除动作**：先 `gitnexus remove -f <worktree>`（删除索引并从注册表注销），再 `git worktree remove --force`（注销主仓库里的 worktree 登记）。
- **会跳过**：索引构建中的；加锁后复核发现刚被使用的。
- **范围**：只处理 `~/.cache/elk-log-analysis/worktrees/` 下的目录，不会碰仓库自己的 `.gitnexus`。
- **记录**：每次删除都写入 `~/.cache/elk-log-analysis/gc.log`。
- **手动执行**：
  ```bash
  python3 ~/.claude/skills/elk-log-analysis/scripts/elk.py code-gc --dry-run
  ```
  去掉 `--dry-run` 才会真正删除；`--days` / `--exact-days` 可临时覆盖阈值。

也可以手动清理单个 worktree（例如不再需要该环境时），用户自己在终端执行：
```bash
git -C ~/code/<仓库> worktree remove --force ~/.cache/elk-log-analysis/worktrees/<仓库>@<分支>
```

## 密码 / API Key 的三种写法

| 写法 | 含义 | 存储位置 |
|---|---|---|
| `明文` | 直接填（MCP env 或 cc-switch elk 对象；envs.json 中禁止） | 明文存于 cc-switch.db、~/.claude.json；写在 MCP env 时还会进入 ~/.codex/config.toml |
| `keychain:<service>/<account>` | 从 macOS 钥匙串读取（推荐生产使用） | 钥匙串 |
| `env:<变量名>` | 从 MCP 进程的环境变量读取（envs.json 推荐写法，变量放在 elk MCP env 中） | cc-switch 的 elk MCP env |

钥匙串写入（用户自己在终端执行，回车后输入密码）：
```bash
security add-generic-password -U -s elk-log-analysis -a prod -w
```
然后在 elk 对象（或 envs.json）的环境项中写 `"password": "keychain:elk-log-analysis/prod"`；MCP env 写法则为 `ELK_PASSWORD=keychain:elk-log-analysis/default`（全局）或 `ELK_PROD_PASSWORD=keychain:elk-log-analysis/prod`（只给生产单独配）。

## GitLab 仓库同步（服务端）

团队部署时，可以让服务端定时从 GitLab 同步仓库，代替手动 clone 到 `ELK_CODE_ROOT`。相关变量是 `ELK_GITLAB_URL`、`ELK_GITLAB_TOKEN`、`ELK_GITLAB_GROUPS`、`ELK_REPO_SYNC_*`，说明见 `scripts/repo_sync.py` 文件开头和 `deploy/README.md` 第 4 节。同步目录会自动加入代码目录扫描，不用另外改 `ELK_CODE_ROOT`。
