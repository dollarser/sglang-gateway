# SGLang API Key 网关

自己实现一套 API Key 鉴权，替代 Cloudflare Access 白名单。

## 为什么不直接用 Access

Access 白名单适合「几十个熟人」的场景，但它有两个硬伤：免费版上限 50 用户，且浏览器访问必须走邮箱验证码交互。一旦你要**对外发放 Key、按用量限流、随时吊销单个用户**，白名单模型就不够用了。

这个网关把 Key 的生命周期完全握在自己手里，同时保留 Access 之外的所有防护能力。

## 架构

```
公网用户
   │  Authorization: Bearer sk-qw-xxxxx
   ▼
Cloudflare Tunnel (cloudflared)
   │
   ▼
本网关 127.0.0.1:2233
   ├─ 1. 校验 Key（SHA-256 哈希比对）
   ├─ 2. 每分钟请求数限流（状态持久化，重启不清零）
   ├─ 3. 每日 token 配额
   ├─ 4. 单 Key 并发上限 + 全局并发闸门
   ├─ 5. 请求兼容改写（developer 角色 / 参数别名 / 上下文溢出重试）
   ├─ 6. 记录用量
   └─ 7. 记录审计日志（含被拒绝的请求）
   │
   ▼
SGLang 127.0.0.1:30007  (只监听回环)
```

网关对客户端完全透明，支持 SSE 流式输出。

### 代码分层

四个模块，依赖方向单向、无环：

```
app.py           通用网关：鉴权、限流、配额、并发、审计、转发
 ├─ openai_proto.py   OpenAI 线格式的共享事实（字段名、usage 结构、错误信封）
 ├─ sglang_compat.py  本服务栈特有的兼容：角色折叠、参数别名、溢出重试、max_model_len
 └─ store.py          SQLite 存储
cli.py           Key 管理 CLI
 └─ store.py
```

`openai_proto.py` 存在的理由是：有些常量**两层都要用，放谁那儿都是错的依赖方向**。
放 `app.py` 会让 `sglang_compat` 反向 import 造成循环依赖；放 `sglang_compat` 会让
通用层反向依赖专用层——将来上游修好、想删掉兼容层时会连带把通用逻辑弄挂。

判断一段代码该放哪一层，只问一句：**换一个模型服务，它还需要吗？**
需要 → `app.py`；不需要 → `sglang_compat.py`。

`openai_proto.py` 与 `sglang_compat.py` 都是**零第三方依赖的纯函数模块**，
所以能跑纯标准库的单元测试（`test_openai_proto.py`、`test_sglang_compat.py`，共 40 项）：

```bash
python3 -m unittest discover -p "test_*.py"
```

### 关于「兼容改写」

Qwen3.8-27B 的 SGLang 服务有四类与 OpenAI 客户端不完全兼容，网关在转发前会就地修掉（见 `sglang_compat.py`）：

| 情况 | 裸 SGLang 的表现 | 网关的处理 |
|---|---|---|
| `messages` 里有 `developer` 角色 | `400 Unexpected message role.` | 折叠进第一条 system 消息 |
| 输入 + `max_tokens` 超过模型窗口 | `400 Requested token count exceeds ...` | 从错误文案反算可用预算，降 `max_tokens` 重试一次 |
| `reasoning_effort=high` | Qwen 侧认的是 `xhigh` | 别名映射 |
| `output_config.effort`（Anthropic 风格） | 上游不认识 | 摘掉该键 |
| `/v1/models` 的 `max_model_len` | 非 OpenAI 规范字段 | 兼容层负责解析，网关据此钳制输出上限 |

这段逻辑原本由一个独立的小代理进程承担（`30008 -> 30007`），2026-09-29 内化进网关，
链路由四跳减为三跳，少一个进程、少一次转发、少一个能被直接打到的无鉴权端口。

两个开关可以单独关掉：`COMPAT_REWRITE=0`、`COMPAT_CONTEXT_RETRY=0`。

## 快速开始

```bash
cd gateway
pip install -r requirements.txt

# 1. 签发第一个 Key
python cli.py create --name alice --rpm 60 --daily-tokens 2000000 --expires 90d
# 输出里的「密钥」只显示这一次，立刻保存

# 2. 启动网关（只监听本地，由 Cloudflare Tunnel 对外）
./run.sh
```

`run.sh` 把端口、后端地址、并发上限这些参数都固化在文件头部，改完直接跑，不用每次敲一长串命令。当前默认：

```
SGLang 后端   http://127.0.0.1:30007
监听          http://127.0.0.1:2233
数据库        data/gateway.db
```

也可以临时覆盖：`PORT=3000 ./run.sh`。不想用脚本就手动起：

```bash
export SGLANG_BASE_URL=http://127.0.0.1:30007
uvicorn app:app --host 127.0.0.1 --port 2233
```

兼容层的单元测试（纯标准库，不需要 SGLang）：

```bash
python3 -m unittest discover -p "test_*.py"   # 40 项，不需要 SGLang
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SGLANG_BASE_URL` | `http://127.0.0.1:30007` | SGLang 服务地址。**填裸 SGLang，不要填兼容代理的端口**——兼容改写已内化 |
| `SGLANG_API_KEY` | 空 | 若 SGLang 启动了 `--api-key`，在此填写，网关会自动附加 |
| `GATEWAY_DB` | `data/gateway.db` | SQLite 数据库路径。默认在项目内 `data/gateway.db`，完全自包含 |
| `MAX_BODY_BYTES` | `62914560` | 请求体上限，默认 60MB（严格低于 64MB，容纳多模态图片/短视频输入） |
| `ALLOWED_PATHS` | `chat/completions,completions,embeddings,models,rerank,score` | **转发白名单**，防路径穿越，见下方安全说明 |
| `MAX_OUTPUT_TOKENS` | `0` | 单次输出上限。**`0` = 不限制**（推荐），由模型上下文窗口与剩余配额约束 |
| `MODEL_MAX_LEN` | `0` | 模型上下文窗口。`0` = 启动时自动探测 |
| `COMPAT_REWRITE` | `1` | 是否做请求兼容改写（`developer` 角色、参数别名） |
| `COMPAT_CONTEXT_RETRY` | `1` | 上下文溢出时是否自动降 `max_tokens` 重试一次 |
| `CONTEXT_RETRY_SAFETY_TOKENS` | `512` | 重试时的安全边距，留出上游 tokenizer 与实际的误差 |
| `GLOBAL_MAX_CONCURRENT` | `4` | **全局**在途请求上限，按整机 GPU 与 SGLang 实际承受能力设（对齐 `--max-running-requests 4`） |
| `AUTH_FAIL_MAX` | `60` | 单个客户端 IP 每分钟允许的鉴权失败次数，默认 60 避免同 NAT 误伤 |
| `SINGLETON_LOCK` | `<GATEWAY_DB>.lock` | 单实例锁文件路径，一般不用改 |
| `INJECT_STREAM_USAGE` | `1` | 流式请求自动注入 `stream_options.include_usage`，便于精确统计 |
| `CORS_ORIGINS` | `*` | 允许的跨域来源，逗号分隔。有 Web 前端时建议收紧到具体域名 |
| `CHARS_PER_TOKEN` | `2.0` | 拿不到 usage 时的字节数折算系数。**对中文偏保守，见下方说明** |

### 参数是否要调——逐项说明

下面是对着一台真实部署（Qwen3.8-27B，`max_model_len` 262144）实测出来的结论。

#### 必须改的

| 参数 | 默认 | 改成 | 原因 |
|---|---|---|---|
| `SGLANG_BASE_URL` | `127.0.0.1:30000` | `127.0.0.1:30007` | 你的服务不在默认端口，不改会全部 502。**填裸 SGLang 的端口** |
| `GATEWAY_DB` | `data/gateway.db` | 保持默认（项目内 `data/gateway.db`） | 默认已指向项目目录内，完全自包含 |

#### 建议调的

**`GLOBAL_MAX_CONCURRENT`：32 → 16**

这是唯一一个「必须按你的机器实测」的参数。实测数据（同一台机器，输出 128 tokens）：

| 并发 | 平均延迟 | 最慢 | 相对单路 |
|---|---|---|---|
| 1 | 2.08s | 2.08s | 1.00x |
| 2 | **1.33s** | 1.87s | 0.64x |
| 4 | 1.40s | 2.04s | 0.68x |
| 8 | 1.56s | 3.05s | 0.75x |
| 16 | 2.23s | 4.05s | 1.07x |

有意思的是**并发 2 比单路还快**——单请求时 GPU 利用率不足，批处理反而提升了效率。到 8 路仍然几乎线性扩展，16 路开始回到单路延迟。

再看长输出的情况（输出 1024 tokens，KV Cache 压力大得多）：

| 并发 | 平均延迟 | 相对单路 |
|---|---|---|
| 1 | 5.34s | 1.00x |
| 2 | 8.76s | 1.64x |
| 4 | 7.61s | 1.43x |

长输出下 **4 路就已经明显劣化**。原因是 KV Cache 占用随生成长度线性增长，显存不够就得排队。

**结论**：`32` 太高了。32 路长输出足以让显存吃紧、所有请求一起变慢。建议 **16**——短输出下延迟只涨 7%，长输出下当作安全阀（超了就 429，而不是大家一起卡死）。

调优方法：改 `run.sh` 里的值，用真实业务问句（不是「说一个字」这种）压测，观察延迟到多少你觉得不能接受了，就往回调一档。

**`CHARS_PER_TOKEN`：2.0 → 3.0（可选）**

这个系数只在**上游没返回 `usage` 时**才用得上，属于兜底。真实 SGLang 每次都返回 usage，所以基本用不到。

但一旦用上，当前值不太准。实测中文响应：

| 样本 | 正文字节/正文token | 整个响应字节/总token |
|---|---|---|
| 长文（4096 tokens） | 3.29 | 3.67 |
| 中篇（1024 tokens） | 3.64 | 4.46 |
| 短答（62 tokens） | 4.33 | 12.87 |

网关的兜底算法是 `响应字节数 / CHARS_PER_TOKEN`。取 2.0 的话，中文场景会**高估约 2 倍**（短回复因为 JSON 结构占比大，能高估到 6 倍）。

高估的方向是安全的（防止配额被绕过），但会让用户在 usage 缺失时更快撞到配额。改成 **3.0** 更贴近实际。短回复无论如何都估不准——JSON 结构开销占比太大，没有哪个固定系数能同时兼顾。

#### 保持默认即可的

| 参数 | 默认 | 判断 |
|---|---|---|
| `MAX_BODY_BYTES` | 10MB | ✅ 够用。LLM 请求通常几十 KB，即使塞满 262144 上下文也就几 MB |
| `MAX_OUTPUT_TOKENS` | 0（不限） | ✅ 由上下文窗口 + 剩余配额兜底，不需要人为设限。**别设成 4096 这种小值** |
| `AUTH_FAIL_MAX` | 20 | ✅ 合理。除非用户集中在同一 NAT 出口，否则不用动 |
| `ALLOWED_PATHS` | 6 个端点 | ✅ 合理，别改成通配 |
| `INJECT_STREAM_USAGE` | 1 | ✅ 必须开，否则流式请求统计不到 token |
| `SINGLETON_LOCK` | `<DB>.lock` | ✅ 不用动 |
| `CORS_ORIGINS` | `*` | ⚠️ 只给 SDK/CLI 用没问题（鉴权走 Header 不走 Cookie，浏览器不会自动带凭证）。有网页前端时收紧到具体域名 |

### 关于 ALLOWED_PATHS

这个白名单是**必需的安全措施**，不要改成通配。

网关把 `/v1/{path}` 直接拼到上游地址。如果 `path` 不做校验，持有合法 Key 的客户端可以用 `curl --path-as-is "https://域名/v1/../flush_cache"` 越出 `/v1/` 前缀，打中 SGLang 的管理端点——`/flush_cache` 会清空 KV 缓存导致所有在线用户性能骤降，`/get_server_info` 会泄露版本、显存、模型路径。

新增推理端点时，把它加进 `ALLOWED_PATHS` 即可：

```bash
export ALLOWED_PATHS="chat/completions,completions,embeddings,models,rerank,score,你的新端点"
```

## Key 管理

```bash
python cli.py create --name bob --rpm 120 --daily-tokens 5000000 --max-concurrent 8
python cli.py list                  # 列出所有 Key
python cli.py revoke 3              # 按编号吊销
python cli.py revoke sk-qw-a1b2     # 按前缀吊销
python cli.py usage --days 7        # 查看用量
python cli.py audit --days 1 --summary   # 审计汇总
python cli.py cleanup --keep-days 30  # 清理历史记录与限流事件
```

如果数据库不在当前目录，加 `--db /path/to/gateway.db`。

## 审计日志

每一次请求都会留一条记录，**包括被拒绝的请求**——鉴权失败、限流、路径拦截这些恰恰是安全审计最需要的信息。

```bash
python cli.py audit --days 1 --summary                  # 按结果汇总
python cli.py audit --days 1                            # 明细，默认最近 50 条
python cli.py audit --days 7 --outcome auth_invalid     # 只看鉴权失败
python cli.py audit --days 7 --ip 1.2.3.4               # 查某个 IP 干了什么
python cli.py audit --days 7 --key-id 3                 # 查某把 Key 的调用记录
```

`outcome` 字段的取值：

| outcome | 含义 |
|---|---|
| `ok` | 正常完成 |
| `path_blocked` | 路径不在白名单内（路径穿越尝试会落到这里） |
| `auth_missing` | 没带 Key |
| `auth_invalid` | Key 无效 |
| `auth_revoked` / `auth_expired` | Key 已吊销 / 已过期 |
| `auth_flood` | 该 IP 鉴权失败次数过多，被暂时封禁 |
| `rate_limited` | 超过每分钟请求上限 |
| `quota_exceeded` | 当日 token 配额用尽 |
| `concurrency_limited` | 单 Key 并发超限 |
| `service_busy` | 全局并发已满 |
| `payload_too_large` | 请求体超过上限 |
| `upstream_unreachable` / `upstream_timeout` | 上游不可达 / 超时 |
| `upstream_error` | 上游返回了错误状态码 |
| `client_abort` | 流式响应中途客户端主动断开（状态码记 499） |
| `stream_broken` | 流式过程中上游中断 |
| `error` | 网关内部错误 |

日常巡检看一眼 `--summary` 就够了。如果 `auth_invalid` 或 `path_blocked` 突然增多，说明有人在扫描你的服务。

`client_abort` 值得留意：正常的客户端不会频繁断连，短时间内大量出现通常是两种原因——客户端程序有 bug，或者有人在批量抓取输出后提前断开。这两种情况都会照常计费（拿不到 `usage` 时按字符数兜底估算），配额不会被绕过。

## 输出长度是怎么限制的

网关**不设人为的固定上限**（`MAX_OUTPUT_TOKENS=0`），而是用三级约束取最小值：

| 级别 | 来源 | 默认 | 作用 |
|---|---|---|---|
| 1 | `MAX_OUTPUT_TOKENS` | `0`（不设） | 需要硬上限时才用 |
| 2 | **模型上下文窗口** | 启动时自动探测 | 把超窗口的取值钳到合法范围 |
| 3 | **该 Key 当日剩余配额** | 自动 | 单次请求不会把当天额度打穿 |

三级都可以单独关闭。全部为 0 时不做任何钳制，完全交给上游。

### 为什么要自动探测上下文窗口

因为**上游对超过 `max_model_len` 的 `max_tokens` 直接拒绝**。实测（直连裸 SGLang）：

| 直连 SGLang，max_tokens= | 结果 |
|---|---|
| 262144（= `max_model_len`） | ✅ 200 |
| 300000 | ❌ 400 `max_completion_tokens is too large: 300000. This model supports at most 262144 completion tokens.` |

报错文案本身是清楚的，但对客户端来说这仍是一次失败——很多 SDK 只会把 `message` 直接弹给用户。
网关启动时从 `/v1/models` 读 `max_model_len`（本机实测 `262144`），把超出的值钳到窗口大小，同样的请求就变成正常响应：

```
直连：      max_tokens=300000  ->  400
经网关：    max_tokens=300000  ->  200，正常返回内容
```

日志里能看到探测结果：

```
INFO [gateway] 输出长度上限：MAX_OUTPUT_TOKENS=不限，模型上下文窗口=262144
```

探测失败（比如网关比 SGLang 先启动）不致命，只是不做窗口钳制，日志里会提示。可以显式指定 `MODEL_MAX_LEN=262144` 跳过探测。

> **两类溢出要分清。** 窗口钳制只管「`max_tokens` 本身超窗口」；而「输入已经很长、输出预算还拉满」是另一种溢出，
> 上游同样返回 400，只能靠 `COMPAT_CONTEXT_RETRY` 反算预算后重试。实测：9200 字的 prompt
> （约 5600 tokens）配 `max_tokens=262144`，裸 SGLang 返回
> `400 Requested token count exceeds the model's maximum context length of 262144 tokens.`，
> 经网关则自动降到 `256016` 重试并返回 200。
>
> ⚠️ **更正**：本文档早期版本写的「SGLang 对 prompt + max_tokens 超窗口会自己收敛，只在 max_tokens 本身超限时才报 400」
> 是错的。那个结论是在**经过兼容代理**的链路上测出来的——代理在背后偷偷降 `max_tokens` 重试了一次，
> 把 400 变成了 200，看起来就像上游「自己收敛」了。直连复测即可推翻。

### 剩余配额钳制

配额校验在请求**开始前**做，所以「刚好用完配额」的那次请求会被放行。如果不管，一个 `max_tokens` 拉满的请求就能把当天额度打穿好几倍。

加上这一级之后，单次请求的输出不会超过剩余配额：

```
3K 配额 Key，请求 max_tokens=100000
  -> 实际钳到 3000，finish_reason=length
```

### 推理模型的注意事项

如果模型带思维链（Qwen3 系列、DeepSeek-R1 等），`usage` 里会多一个 `reasoning_tokens`，**它和正文共享同一个 `max_tokens` 预算**。

实测（Qwen3.8-27B，同一个复杂问题）：

| 请求 max_tokens | 实际 completion | 其中思维链 | 正文字符数 | finish_reason |
|---|---|---|---|---|
| 64 | 64 | 64 | **0** | length |
| 256 | 256 | 256 | **0** | length |
| 1024 | 1024 | 134 | 1668 | length |
| 4096 | 4096 | 100 | 7803 | length |

前两行是问题所在：预算全被思维链吃掉，**正文一个字都没有**，但返回的是 HTTP 200 加 `finish_reason: length`。客户端不会报错，界面上只会显示一片空白——这是最难排查的一类故障，因为从日志上看请求是成功的。

**这正是把上限交给上下文窗口的理由。** 一个中等复杂的问题就能吃掉 4000+ tokens，固定 4096 的上限会频繁截断。

不过要提醒：**`max_tokens` 是客户端自己传的**。如果你给用户的 Key 配额不大，客户端的默认值（常见 2048/4096）仍然可能偏小。网关只能保证「不超过上下文窗口」，没法替用户把值调大。

计费上不用担心：`completion_tokens` 已经**包含**思维链，所以推理消耗照常计入配额，不会被绕过。

**延迟要相应放宽预期**。实测生成 4096 tokens 约 **23 秒**，简单问题约 2 秒。放开上限后，一次请求最长可能跑到 24 分钟（262144 tokens ÷ 约 178 tok/s）。这有两个后果：

- Cloudflare 免费版**源站超时 100 秒且不可调**——长生成必须走流式（数据持续返回会重置计时），否则会 524
- 单次请求会长时间占用一个并发位。`GLOBAL_MAX_CONCURRENT=16` 下，4 个用户各开 4 路长请求就能把服务占满

所以放开上限的代价是**要盯住并发**。如果发现长请求影响了其他人，回到 `MAX_OUTPUT_TOKENS=32768`（约 3 分钟）之类的中间值。

## 客户端填什么

网关对外就是一个标准的 OpenAI 兼容接口，所以任何支持「OpenAI 兼容」的客户端都能接。

用户只需要填三个值：

| 客户端字段 | 填什么 |
|---|---|
| Provider / 提供方 | **OpenAI（兼容）** 或 OpenAI Compatible / 自定义 |
| Base URL / API 地址 | `https://llm.example.com/v1` |
| API Key | 网关签发的 `sk-qw-xxxxxxxx` |
| Model / 模型名 | SGLang 启动时的 `--served-model-name` |

### 关键：用户填的 Key 不是 SGLang 的 Key

这是最容易混淆的一点。链路上有两把 Key，职责完全不同：

- **网关 Key（`sk-qw-...`）** —— 给用户的，用于网关鉴权、限流、配额。用户只需要知道这个。
- **SGLang 的 `--api-key`** —— 网关到 SGLang 之间用的，用户完全不需要知道。如果 SGLang 设了它，在网关的环境变量 `SGLANG_API_KEY` 里配置，网关会自动附加。

用户不需要、也不应该拿到 SGLang 的 Key。

### /v1 后缀的坑

`/v1` 是最常见的配置错误来源：

- **OpenAI 官方 SDK**（Python / Node）要求 `base_url` **必须**带 `/v1`。
- **GUI 客户端**（Cherry Studio、NextChat、LobeChat 等）行为不一致，有的会自动补 `/v1`，有的不会。

判断方法很简单：**先按 `https://llm.example.com/v1` 填，如果返回 404，就把末尾的 `/v1` 去掉再试**。如果返回 401，说明地址是对的，只是 Key 填错了。

### 模型名怎么确定

填 SGLang 启动时 `--served-model-name` 指定的名字（没指定就填模型路径，如 `Qwen/Qwen3-32B`）。不确定的话，用任意 Key 请求一次模型列表：

```bash
curl https://llm.example.com/v1/models -H "Authorization: Bearer sk-qw-xxxxxxxx"
```

返回的 `data[].id` 就是可用的模型名。

### 常见客户端对照

| 客户端 | Provider 选 | 地址 | 备注 |
|---|---|---|---|
| OpenAI Python / Node SDK | — | `https://llm.example.com/v1` | 必须带 `/v1` |
| Cherry Studio | OpenAI | `https://llm.example.com` | 若报 404 再补 `/v1` |
| ChatBox | OpenAI API 兼容 | `https://llm.example.com/v1` | |
| NextChat | 自定义接口 | `https://llm.example.com` | 若报 404 再补 `/v1` |
| LobeChat | OpenAI | `https://llm.example.com/v1` | |
| Open WebUI | OpenAI | `https://llm.example.com/v1` | |
| Cursor | OpenAI API Key | `https://llm.example.com/v1` | 需打开 Override Base URL |
| Continue（VS Code） | `openai` | `https://llm.example.com/v1` | 配置里加 `apiBase` |
| Dify | OpenAI-API-compatible | `https://llm.example.com/v1` | |
| FastGPT | OpenAI 兼容 | `https://llm.example.com/v1` | |

## 客户端调用

> **想先确认能不能通？** 直接跑 `python3 client-test.py`——只依赖标准库，无需装任何包。
> 它会依次验证健康检查、鉴权拦截、模型列表、非流式推理、流式推理，最后给通过/失败汇总。
>
> ```bash
> python3 client-test.py                                              # 用默认公网地址和内置测试 Key
> BASE_URL=https://llm.example.com API_KEY=sk-qw-xxx python3 client-test.py
> ```

### Python（OpenAI SDK）

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://llm.example.com/v1",
    api_key="sk-qw-xxxxxxxx",      # 网关签发的 Key
)

stream = client.chat.completions.create(
    model="qwen",
    messages=[{"role": "user", "content": "你好"}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")
```

### curl

```bash
curl https://llm.example.com/v1/chat/completions \
  -H "Authorization: Bearer sk-qw-xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

也支持 `x-api-key` 头，方便对接 Anthropic 风格的客户端。

## 配合 Cloudflare Tunnel

`~/.cloudflared/config.yml` 指向网关端口，而不是 SGLang 端口：

```yaml
tunnel: <TUNNEL_ID>
credentials-file: /Users/you/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: llm.example.com
    service: http://127.0.0.1:2233
    originRequest:
      # 保持 false（允许分块），否则 SSE 流式响应会被缓冲，表现为一次性吐出全部内容
      disableChunkedEncoding: false
  - service: http_status:404
```

这样 Key 鉴权在网关层完成，SGLang 的 30007 端口始终不暴露。

**本机实际部署**：隧道 `sglang-gateway` → `http://127.0.0.1:2233`。
从零操作步骤见 [`docs/Cloudflare-Tunnel-创建与域名绑定.md`](docs/Cloudflare-Tunnel-创建与域名绑定.md)。

隧道后面按 IP 限流仍然有效：网关的 `_client_ip()` 优先读 `CF-Connecting-IP`
（Cloudflare 会覆盖客户端伪造的同名头），审计日志里是真实公网 IP 而非 `127.0.0.1`。

## 部署为后台服务（Linux systemd）

```ini
[Unit]
Description=SGLang API Gateway
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/opt/gateway
Environment="SGLANG_BASE_URL=http://127.0.0.1:30007"
Environment="GATEWAY_DB=/opt/gateway/gateway.db"
ExecStart=/usr/bin/uvicorn app:app --host 127.0.0.1 --port 2233
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

macOS 上用 launchd，或直接 `nohup uvicorn ... &`。

## 安全建议

1. **网关只监听 `127.0.0.1`**，对外一律经 Cloudflare Tunnel，不要开放公网端口。
2. **数据库文件权限设为 600**，虽然只存哈希，但用量数据也有隐私价值。
   **并且启动脚本里要有 `umask 077`**——代码里逐文件 `chmod` 只覆盖 DB 和 `-wal` / `-shm`，
   管不到日志、锁文件和 `gateway-data/` 目录本身，而且手工 `chmod` 的目录权限重启就打回原形。
3. **SGLang 始终只监听回环**，`--host 127.0.0.1`，并额外设置 `--api-key` 做第二层防护。
4. **定期 `cleanup`**，避免 `usage_log` 无限增长。
5. Key 泄露时立刻 `revoke`，比改密码快。
6. **不要在网关和 SGLang 之间再插一个无鉴权的转发层。** 之前那个兼容代理转发**所有**路径
   （包括 `/flush_cache`、`/get_server_info` 这类管理端点）且自身不做任何鉴权，只靠「绑 127.0.0.1
   + 网关的路径白名单」兜着。这类中间层一旦被误绑到 `0.0.0.0`，就等于把 SGLang 的管理面直接挂到网上。
   兼容逻辑内化进网关后，这个风险点已经消失——**路径白名单是唯一的入口，且它对所有请求生效**。
7. **确认 `/openapi.json`、`/docs`、`/redoc` 都返回 404。** 这三个是 FastAPI 自动注册的端点，
   **不需要 Key** 就能拿到完整路由表和参数 schema。它们**不经过**路径白名单（白名单只管 `/v1/{path}`），
   所以必须自己单独验一遍——本仓库已设 `openapi_url=None`，但你改动 `FastAPI(...)` 参数时别漏掉它。

## 已知局限

- **网关不再是「纯透明代理」**。它会在转发前改写请求体（折叠 `developer` 角色、重试时降 `max_tokens`）。
  这意味着网关需要解析完整 JSON body，不能退化成纯字节流转发；也意味着**上游看到的是改写后的请求**，
  排查问题时要以网关日志里的「兼容改写」行为准。不需要改写时可以 `COMPAT_REWRITE=0` 关掉。

- **必须单 worker 运行**。限流状态已经持久化到 SQLite，但**并发计数仍在内存里**，多 worker 会让全局并发上限实际翻倍。网关启动时会用文件锁挡住第二个实例——误启动会直接报错退出，而不是静默地让限制失效。
- **每个请求多几次本地数据库往返**。限流、配额、用量、审计都要落盘，比纯内存实现多了一点开销。对 LLM 这种秒级响应的服务可以忽略；如果 QPS 达到数百，建议把限流换回内存实现（接受重启清零）或改用 Redis。
- **SQLite 单机存储**，适合几十到几百个 Key 的规模。再往上应换 PostgreSQL。
- **没有 Web 管理界面**，全部通过 CLI 操作。这本身就是安全设计——管理入口不暴露在网络里。
- **token 估算有误差**。正常情况下网关会从响应的 `usage` 字段读取精确值；只有上游未返回 usage 时才按字符数折算，此时配额是近似值。
- **日配额是软上限**。配额在请求开始前校验，所以「刚好把配额用完」的那一次请求会被放行，最终用量可能略超设定值。对防止滥用来说够用，精确计费场景需要另外对账。
- **鉴权洪水防护按 IP 计数，会误伤同 IP 的正常用户**。`AUTH_FAIL_MAX` 触发后该 IP 在 60 秒内所有请求（包括合法 Key）都会被拒。Cloudflare 会通过 `CF-Connecting-IP` 传来真实 IP，所以正常情况下一人一 IP；但如果你的用户集中在一个 NAT 出口后面，需要调高这个值或改为按 IP + Key 组合计数。

## 运维要点

### 单实例锁

网关启动时会对 `SINGLETON_LOCK`（默认 `<GATEWAY_DB>.lock`）加排他锁，并把进程 PID 写进该文件。**第二个实例启动会直接报错退出**：

```
RuntimeError: 另一个网关实例正在使用 /opt/gateway/gateway.db（锁文件 .../gateway.db.lock）。
本网关设计为单 worker 运行，请先停掉已有实例。
```

这是有意为之。多实例不会报错，但会让全局并发上限静默翻倍，排查成本远高于直接启动失败。

注意 `uvicorn --workers N` 也会触发这个锁——**不要用 `--workers`**，保持默认单进程。如果确实需要多实例，得先把并发计数挪到 Redis 之类的共享存储。

锁文件里是 PID，可以用它确认当前跑的是哪个进程：

```bash
cat /opt/gateway/gateway.db.lock   # 输出当前网关的 PID
```

### 限流状态是持久化的

每分钟请求数、鉴权失败次数都存在 SQLite 的 `rate_events` 表里，**网关重启不会清零**。这一点很重要：如果状态在内存里，攻击者只要想办法让网关重启一次（比如打满内存），限流窗口就被重置了。

`rate_events` 会随请求自动清理（每小时一次全局清扫），也可以用 `python cli.py cleanup` 手动清。

### 排错：403 有两种完全不同的含义

公网访问返回 403 时，**先看 body**，两种原因的处置方式完全不同：

| body | 来源 | 含义 | 怎么办 |
|---|---|---|---|
| `{"error":{"message":"API Key 已被吊销。","code":"key_revoked"}}` | **网关** | Key 已被 `cli.py revoke` 吊销 | 重新签发。注意网关对**已吊销**的 Key 回的是 403 而不是 401 |
| `error code: 1010` | **Cloudflare** | 被 Browser Integrity Check 按 User-Agent 拦了 | 见《Cloudflare-Tunnel-创建与域名绑定.md》。`httpx` / `openai` / `requests` 等 SDK 不受影响，只有裸 `urllib` 会被拦 |

401 和 403 的分工：**401 = 没带 Key 或 Key 不认识**（`auth_missing` / `auth_invalid`）；
**403 = Key 认识但已被吊销**。区分这两者，排查时能少绕一大圈。
