# Cloudflare Access 白名单配置指南

> **这是备选方案。** 当前采用自建 API Key 网关（见 `README.md`），本文仅在你决定**不**用网关、改回 Access 白名单时参考。
>
> 两者的取舍：Access 白名单上限 50 用户、需要邮箱验证码交互，适合「几十个熟人」；一旦要对外发 Key、按用量限流、随时吊销单个用户，就用网关。
>
> 场景：SGLang 部署的 Qwen 模型在内网，通过 Cloudflare Tunnel 对外提供 OpenAI 兼容 API。
> 目标：只有白名单内的人能用，浏览器访问走邮箱验证码，脚本调用走服务令牌。

## 0. 前置条件

| 项目 | 要求 |
|---|---|
| 域名 | 已托管在 Cloudflare（DNS 由 Cloudflare 管理） |
| 账号 | Cloudflare 账号，Zero Trust 已激活（免费版上限 50 用户） |
| 隧道 | 已创建 Named Tunnel，`cloudflared` 已连通 |
| 服务 | SGLang 只监听 `127.0.0.1`，前面有 Nginx 做网关 |

免费版 Zero Trust 支持 50 个用户（按邮箱去重计数），超出后 7 美元/用户/月。给白名单场景用完全够。

## 1. 核心思路

Access 应用里配置**两条策略，逻辑是 OR**——命中任意一条即放行：

| 策略 | Action | 匹配条件 | 给谁用 |
|---|---|---|---|
| `allowlist-emails` | **Allow** | Include → Emails（白名单邮箱） | 浏览器访问，走一次性验证码 |
| `service-token-api` | **Service Auth** | Include → Service Token | 脚本 / SDK / 自动化，走请求头 |

两条策略的机制完全不同，必须都配，否则会出现「浏览器能进但 API 调不通」或反过来。

## 2. 创建 Service Token（给脚本用）

路径：**Zero Trust → Access → Service Auth → Service Tokens → Create Service Token**

1. Name 填一个能认出来的名字，如 `sglang-api-client`
2. Service Token Duration 选择有效期（可后续 Refresh 续期，建议先设 1 年）
3. 点击 Generate token
4. **立即复制 Client Secret——它只显示这一次**，丢了只能重新生成

生成后你会拿到两个值：

```
Client ID:     xxxxxxxx.access
Client Secret: yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy
```

注意 Client ID 通常带 `.access` 后缀。

## 3. 创建 Access 应用并配两条策略

路径：**Zero Trust → Access → Applications → Add an application → Self-hosted**

基础设置：

| 字段 | 值 |
|---|---|
| Application name | `sglang-qwen` |
| Application domain | `llm.example.com`（换成你自己的域名） |
| Session Duration | 24 hours |

### 策略 A：邮箱白名单

```
Policy name: allowlist-emails
Action:      Allow
Configure rules:
  Selector: Emails
  Value:    alice@example.com, bob@example.com
```

可以加多个邮箱，用逗号分隔。也可以换成 Selector = `Emails ending in` 来放行整个域。

### 策略 B：服务令牌

```
Policy name: service-token-api
Action:      Service Auth          <-- 关键，不能选 Allow
Configure rules:
  Selector: Service Token
  Value:    sglang-api-client
```

**Action 必须选 `Service Auth`**。选错成 `Allow` 的话，Access 会要求身份提供商登录，脚本永远调不通。

## 4. cloudflared 配置

`~/.cloudflared/config.yml`：

```yaml
tunnel: <你的 TUNNEL_ID>
credentials-file: /Users/you/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: llm.example.com
    service: http://127.0.0.1:2233
  - service: http_status:404
```

这里指向的是 **Nginx 的 2233**，不是 SGLang 的 30007。最后一条兜底规则必须有，否则未匹配的请求可能被转发到错误的服务。

## 5. Nginx 网关

```nginx
limit_req_zone $http_cf_access_client_id zone=llm:10m rate=60r/m;

server {
    listen 127.0.0.1:2233;

    location /v1/ {
        limit_req zone=llm burst=10 nodelay;

        proxy_pass http://127.0.0.1:30007;
        proxy_http_version 1.1;

        proxy_buffering off;          # 流式输出必须关，否则 SSE 会攒成一次性返回
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
    }

    location / { return 404; }        # 其余路径一律拒绝
}
```

`limit_req_zone` 的 key 用 `$http_cf_access_client_id`，这样每个 Service Token 单独计数，一个客户端刷爆不影响别人。

Nginx 可以直接读取 Cloudflare 注入的身份头（源站只经隧道可达，不会被外部伪造）：

- `Cf-Access-Authenticated-User-Email` —— 浏览器访问者的邮箱
- `Cf-Access-Client-Id` —— 服务令牌的 Client ID
- `Cf-Access-Jwt-Assertion` —— 完整 JWT

## 6. 三种调用方式

### 浏览器

直接访问 `https://llm.example.com`，跳转到 Cloudflare 登录页，输入白名单邮箱，收一次性验证码，验证后拿到 24 小时的会话 Cookie。

### curl

```bash
curl https://llm.example.com/v1/chat/completions \
  -H "CF-Access-Client-Id: xxxxxxxx.access" \
  -H "CF-Access-Client-Secret: yyyyyyyy..." \
  -H "Authorization: Bearer <sglang 的 api key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'
```

### OpenAI SDK（Python）

两个 Access 头是自定义头，用 `default_headers` 传：

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://llm.example.com/v1",
    api_key="<sglang 的 api key>",
    default_headers={
        "CF-Access-Client-Id": "xxxxxxxx.access",
        "CF-Access-Client-Secret": "yyyyyyyy...",
    },
)

resp = client.chat.completions.create(
    model="qwen",
    messages=[{"role": "user", "content": "你好"}],
    stream=True,
)
for chunk in resp:
    print(chunk.choices[0].delta.content or "", end="")
```

Node.js 的 `openai` 包同理，用 `defaultHeaders` 选项。

## 7. 常见坑

**Client Secret 只显示一次。** 没存下来就只能删掉重建。

**只有 Service Auth 策略时，每次请求都必须带令牌。** 官方文档明确写了：Access 只有在应用里至少存在一条 Allow 策略时，才会签发可复用的 JWT Cookie。所以策略 A 和策略 B 要同时存在，脚本侧也老老实实每次带两个头。

**Access 不会自动帮你做业务鉴权。** 白名单只解决「谁能进门」，进门后调用大模型的权限仍然靠 SGLang 的 `--api-key`。两层是叠加的，不是二选一。

**别用 Bypass 策略放行 API 路径。** `Bypass` 会让该路径完全不受 Access 保护，公网可直接访问。除非你确认 SGLang 侧已有强鉴权和限流，否则不要用。

**Service Token 会过期。** 到 Cloudflare 控制台 → Notifications 里加一个 `Expiring Access Service Token` 提醒，提前一周收到通知。

**流式输出不受 Access 影响。** Access 只在请求入口做校验，SSE 长连接本身可以正常穿透。真正会卡住流式的是 Nginx 的 `proxy_buffering` 没关，或者 Quick Tunnel 本身不支持长连接。

**50 用户是按邮箱去重计的。** 白名单里放几十个邮箱没问题，但不要指望用它做面向公众的开放注册。

## 8. 如果目标是「真正公开」

白名单和公开访问是互斥的。要面向不特定公众开放，正确做法是：

1. 去掉 Access 的邮箱白名单，改为 `Bypass` 指定路径 + SGLang 侧发放 API Key
2. 限流必须做在 Nginx 和 Cloudflare WAF 两层
3. 按《生成式人工智能服务管理暂行办法》，面向境内公众提供生成式 AI 服务需完成算法备案和上线备案

对小范围熟人共享，Access 白名单是最省事的方案；对真公开，建议换到国内云主机并走备案流程。
