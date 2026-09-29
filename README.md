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
   ├─ 5. 记录用量
   └─ 6. 记录审计日志（含被拒绝的请求）
   │
   ▼
SGLang 127.0.0.1:30000  (只监听回环)
```

网关对客户端完全透明，支持 SSE 流式输出。

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
SGLang 后端   http://127.0.0.1:30008
监听          http://127.0.0.1:2233
数据库        ~/gateway-data/gateway.db
```

也可以临时覆盖：`PORT=3000 ./run.sh`。不想用脚本就手动起：

```bash
export SGLANG_BASE_URL=http://127.0.0.1:30008
uvicorn app:app --host 127.0.0.1 --port 2233
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SGLANG_BASE_URL` | `http://127.0.0.1:30000` | SGLang 服务地址。**当前部署实际是 `30008`，必须显式设置** |
| `SGLANG_API_KEY` | 空 | 若 SGLang 启动了 `--api-key`，在此填写，网关会自动附加 |
| `GATEWAY_DB` | `gateway.db` | SQLite 数据库路径。**生产环境务必用绝对路径** |
| `MAX_BODY_BYTES` | `10485760` | 请求体上限，默认 10MB |
| `ALLOWED_PATHS` | `chat/completions,completions,embeddings,models,rerank,score` | **转发白名单**，防路径穿越，见下方安全说明 |
| `MAX_OUTPUT_TOKENS` | `0` | 单次输出上限。**`0` = 不限制**（推荐），由模型上下文窗口与剩余配额约束 |
| `MODEL_MAX_LEN` | `0` | 模型上下文窗口。`0` = 启动时自动探测 |
| `GLOBAL_MAX_CONCURRENT` | `32` | **全局**在途请求上限，按整机 GPU 承受能力设置。**实测建议 16，见下方说明** |
| `AUTH_FAIL_MAX` | `20` | 单个客户端 IP 每分钟允许的鉴权失败次数 |
| `SINGLETON_LOCK` | `<GATEWAY_DB>.lock` | 单实例锁文件路径，一般不用改 |
| `INJECT_STREAM_USAGE` | `1` | 流式请求自动注入 `stream_options.include_usage`，便于精确统计 |
| `CORS_ORIGINS` | `*` | 允许的跨域来源，逗号分隔。有 Web 前端时建议收紧到具体域名 |
| `CHARS_PER_TOKEN` | `2.0` | 拿不到 usage 时的字节数折算系数。**对中文偏保守，见下方说明** |

### 参数是否要调——逐项说明

下面是对着一台真实部署（Qwen3.8-27B，`max_model_len` 262144）实测出来的结论。

#### 必须改的

| 参数 | 默认 | 改成 | 原因 |
|---|---|---|---|
| `SGLANG_BASE_URL` | `127.0.0.1:30000` | `127.0.0.1:30008` | 你的服务不在默认端口，不改会全部 502 |
| `GATEWAY_DB` | `gateway.db` | 绝对路径，如 `~/gateway-data/gateway.db` | 相对路径依赖工作目录，systemd 启动时目录不同就找不到库，会**静默新建一个空库**——表现为所有 Key 突然失效 |

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

因为**上游对超窗口的取值返回的是空 body 的 400**。实测：

| 直连 SGLang，max_tokens= | 结果 |
|---|---|
| 262144（= `max_model_len`） | ✅ 200 |
| 300000 | ❌ 400，**body 完全为空** |
| 1000000 | ❌ 400，body 空 |

客户端只能看到一个没有任何说明的 400，根本不知道是自己 `max_tokens` 填大了。

网关启动时从 `/v1/models` 读 `max_model_len`（本机实测 `262144`），把超出的值钳到窗口大小，同样的请求就变成正常响应：

```
直连：      max_tokens=300000  ->  400（空 body）
经网关：    max_tokens=300000  ->  200，正常返回内容
```

日志里能看到探测结果：

```
INFO [gateway] 输出长度上限：MAX_OUTPUT_TOKENS=不限，模型上下文窗口=262144
```

探测失败（比如网关比 SGLang 先启动）不致命，只是不做窗口钳制，日志里会提示。可以显式指定 `MODEL_MAX_LEN=262144` 跳过探测。

> 顺带说明：SGLang 对「prompt + max_tokens 超过窗口」的情况自己会收敛。实测 4052 tokens 的 prompt 配 `max_tokens=262144` 返回 200 正常。它只在 `max_tokens` 本身超过 `max_model_len` 时才报 400。

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

这样 Key 鉴权在网关层完成，SGLang 的 30008 端口始终不暴露。

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
Environment="SGLANG_BASE_URL=http://127.0.0.1:30000"
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
3. **SGLang 始终只监听回环**，`--host 127.0.0.1`，并额外设置 `--api-key` 做第二层防护。
4. **定期 `cleanup`**，避免 `usage_log` 无限增长。
5. Key 泄露时立刻 `revoke`，比改密码快。

## 已知局限

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
