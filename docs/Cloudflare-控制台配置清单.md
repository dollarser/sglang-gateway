# Cloudflare 控制台配置清单

本文只讲 **Cloudflare 侧**需要做什么，不涉及服务器端部署（那部分见《新机器部署手册》）。

场景：把内网的 SGLang + API Key 网关，通过 Cloudflare Tunnel 暴露成公网 API 服务。

---

## 零、入口一览

| 用途 | 入口 | 说明 |
|---|---|---|
| 主控制台（域名、DNS、WAF、缓存） | https://dash.cloudflare.com | 大部分配置在这里 |
| Zero Trust（Tunnel、Access） | https://one.dash.cloudflare.com | Tunnel 和身份认证在这里，**独立于主控制台** |
| 账户 API Token | https://dash.cloudflare.com/profile/api-tokens | 需要自动化时用 |

注意这两个控制台是分开的。**Tunnel 不在主控制台里**，新手最容易在这里迷路。

菜单路径以下均以主控制台选中域名后为准（`dash.cloudflare.com` → 点进你的域名 → 左侧菜单）。

---

## 一、域名接入

**入口**：https://dash.cloudflare.com → 添加站点

**要做的**：

1. 输入域名，选择 Free 计划
2. 到你的域名注册商处，把 NS（名称服务器）改成 Cloudflare 给的两条
3. 等待生效（通常几分钟到几小时）

**验证**：主控制台该域名显示「活动」，且 Overview 页面能看到流量。

> 没有域名的话，后面所有配置都无从谈起。Tunnel 的正式主机名必须挂在 Cloudflare 托管的域名下。

---

## 二、Zero Trust 初始化

**入口**：https://one.dash.cloudflare.com

**首次使用**：

1. 系统会要求起一个 **team name**（团队名），例如 `yourname`，之后访问路径变成 `one.dash.cloudflare.com/yourname/...`
2. 选择免费计划（Free，上限 50 用户）

**要做的**：

| 配置项 | 路径 | 说明 |
|---|---|---|
| 创建 Tunnel | Networks → Tunnels | 命令行 `cloudflared tunnel create` 创建后会自动出现在这里 |
| 查看连接状态 | Networks → Tunnels → 点进隧道 | 显示各边缘节点的连接情况 |
| Access 应用（可选） | Access → Applications | 需要白名单访问时配置，见《Cloudflare-Access-白名单配置指南.md》 |
| Service Token（可选） | Access → Service Auth → Service Tokens | 脚本调用用，同上 |

> Tunnel 的 ingress 规则有两种管理方式：**本地 config.yml**（本文和部署手册采用）和 **控制台配置**（token 模式）。两者不要混用，否则会互相覆盖。

---

## 三、SSL/TLS

**入口**：主控制台 → 选中域名 → **SSL/TLS**

| 配置项 | 位置 | 设置值 | 为什么 |
|---|---|---|---|
| 加密模式 | SSL/TLS → Overview | **Full** 或 **Full (Strict)** | 默认的 Flexible 会让 Cloudflare 以 HTTP 回源，虽然后面有 Tunnel 兜底，但设成 Full 更规范 |
| 始终使用 HTTPS | SSL/TLS → Edge Certificates → Always Use HTTPS | **开启** | 避免用户误用 http:// 导致请求被拒 |
| 最低 TLS 版本 | SSL/TLS → Edge Certificates → Minimum TLS Version | TLS 1.2 | 1.0/1.1 已不安全 |
| 自动 HTTPS 重写 | SSL/TLS → Edge Certificates → Automatic HTTPS Rewrites | 开启 | 无害，顺手开 |

> 用 Tunnel 时源站不暴露，加密模式的实际影响不大，但保持 Full 可以避免以后换成其他接入方式时踩坑。

---

## 四、WAF 速率限制（重点）

**入口**：主控制台 → 选中域名 → **Security → WAF → Rate limiting rules**

**免费版配额**：只能创建 **1 条**规则（界面显示 `0/1 rules`）。Pro 是 10 条，Business 是 15 条。

因为只有一条，要把它用在最关键的地方——也就是你的 API 路径。

### 推荐配置

| 字段 | 填写值 |
|---|---|
| Rule name | `api-rate-limit` |
| If incoming requests match | `URI Path` **contains** `/v1/` |
| When rate exceeds | `IP Address`，每 **60 秒** 超过 **60** 次请求 |
| Then take action | **Block**（见下方警告） |
| Duration | 10 分钟 |

### 警告：API 场景必须选 Block，不能选 Managed Challenge

网上大多数教程会推荐 `Managed Challenge`，理由是"误伤影响最小"。**那是给网页场景的建议，对 API 服务是错的。**

Managed Challenge 会返回一个 HTML 挑战页，要求浏览器执行 JavaScript。而你的用户是 OpenAI SDK、curl、Cherry Studio 这类客户端——它们**不会执行 JS，也看不懂 HTML 挑战页**，只会收到一个解析失败的响应。结果是：一旦触发限流，正常用户也会一直失败，而且报错信息完全看不出是被 Cloudflare 拦了。

同理，**JS Challenge 和 CAPTCHA 也都不能用**。

API 场景只有两个可选动作：

- **Block** —— 直接返回 403，客户端能明确看到被拒
- **Log** —— 只记录不拦截，用于观察阶段

建议先用 `Log` 跑一天看看会拦到多少，确认阈值合理后再切 `Block`。

### 阈值怎么定

这条规则和网关自身的限流是**叠加**的，两层各有分工：

| 层级 | 作用 |
|---|---|
| Cloudflare 速率限制 | 在边缘拦掉扫描和暴力尝试，请求**不会**到达你的机器 |
| 网关的 `--rpm` 限制 | 按 Key 精确控制每个用户的配额 |

Cloudflare 这层按 IP 计数，阈值应该设得比单个用户的 `--rpm` **宽一些**，否则正常用户会被误伤。比如网关给每个 Key 设 60 次/分钟，Cloudflare 这层可以设 120 次/分钟——它的目的是拦异常流量，不是精确配额。

---

## 五、关闭 Under Attack 模式（重要）

**入口**：主控制台 → 选中域名 → **Security → Settings**

| 配置项 | 设置值 | 说明 |
|---|---|---|
| Security Level | **Medium** 或 **Low** | 不要设成 **I'm Under Attack** |
| Under Attack Mode | **关闭** | 见下方说明 |

**为什么**：I'm Under Attack 模式会对所有访客插入 JavaScript 挑战。和上面同理，API 客户端过不了这一关，开启后你的服务会立刻全面不可用。这个模式只在遭受大规模 DDoS 时临时开启，且应该配合 IP 白名单使用。

Security Level 也不要设成 High——它会按 IP 信誉自动挑战可疑访客，可能误伤正常的 API 调用。

---

## 六、缓存绕过（必须做）

**入口**：主控制台 → 选中域名 → **Caching → Cache Rules**（或 **Rules → Page Rules**）

**为什么必须做**：Cloudflare 默认会缓存它认为是静态资源的内容。一旦 `/v1/*` 的响应被缓存，会出现非常诡异的现象——不同用户拿到同一个回答，或者已经过期的数据一直返回。这类问题极难排查。

**用 Cache Rules（推荐）**：

| 字段 | 填写值 |
|---|---|
| Rule name | `bypass-api-cache` |
| If incoming requests match | `URI Path` **starts with** `/v1/` |
| Then | **Bypass cache** |

**用 Page Rules（免费版 3 条）**：

```
URL: llm.example.com/v1/*
Setting: Cache Level → Bypass
```

> 严格来说 Cloudflare 默认不会缓存 POST 请求，而绝大多数推理调用都是 POST。但 `/v1/models` 是 GET，且未来的端点可能变化，显式绕过更稳妥。

---

## 七、Bot 防护（按需）

**入口**：主控制台 → 选中域名 → **Security → Bots**

免费版提供 **Bot Fight Mode**。它可以拦截自动化流量，但**可能误伤你的 API 客户端**——因为 SDK 和脚本的请求特征与爬虫相似。

**建议**：

- 先开启观察几天，用 **Security → Events** 看拦截记录
- 如果发现有正常用户被拦，关闭它
- 网关已经有 Key 鉴权，Bot 防护在这里不是必需的

如果确实需要，优先考虑用 **WAF 自定义规则**按 User-Agent 精确放行，而不是用全局的 Bot Fight Mode。

---

## 八、超时限制（无法修改，需要知道）

Cloudflare 免费版有一条硬限制：**源站响应超过 100 秒未返回数据，连接会被切断并返回 524 错误**。

对 LLM 服务的影响：

- 正常的流式输出不受影响——因为数据在持续返回，计时器会不断重置
- **首 token 延迟超过 100 秒会被切断**。长上下文（几万 token）的 prefill 阶段可能接近这个数字
- 非流式请求如果总耗时超过 100 秒，一定失败

**应对**：

1. 客户端尽量用流式（`stream: true`）
2. 控制 `--context-length`，避免单次 prompt 过长
3. 网关的 `MAX_OUTPUT_TOKENS` 已经限制了输出长度，配合使用
4. 这个限制只在付费版（Enterprise）可调

---

## 九、通知告警

**入口**：https://dash.cloudflare.com → 右上角账户 → **Notifications**

建议添加这几条：

| 告警类型 | 用途 |
|---|---|
| Expiring Access Service Token | 用了 Access 白名单时，Token 快过期前一周提醒 |
| Tunnel Health | 隧道连接异常时通知 |
| Origin Error Rate | 源站错误率异常时通知 |
| HTTP DDoS Attack Alert | 遭受攻击时通知 |

Tunnel 断开是最需要及时知道的情况——服务看起来"没挂"，但外部已经完全访问不到了。

---

## 十、验证清单

配置完成后逐项确认：

- [ ] 域名状态为「活动」，NS 已指向 Cloudflare
- [ ] Zero Trust 已初始化，Tunnel 显示 Healthy
- [ ] SSL/TLS 加密模式为 Full 或 Full (Strict)
- [ ] Always Use HTTPS 已开启
- [ ] 速率限制规则已创建，动作为 **Block**（不是 Managed Challenge）
- [ ] Security Level 未设为 I'm Under Attack
- [ ] `/v1/*` 的缓存绕过规则已生效
- [ ] Bot Fight Mode 状态已确认（开启或关闭，且验证过不误伤）
- [ ] 通知告警已配置至少 Tunnel Health 一条

**最直接的验证方式**——从外部网络跑这几条：

```bash
B=https://你的域名
K=sk-qw-你的密钥

# 1. 鉴权生效（期望 401）
curl -s -o /dev/null -w "%{http_code}\n" $B/v1/models

# 2. 正常调用（期望 200）
curl -s -o /dev/null -w "%{http_code}\n" $B/v1/models -H "Authorization: Bearer $K"

# 3. 流式输出正常（应逐块吐字，不是一次性返回）
curl -N $B/v1/chat/completions \
  -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
  -d '{"model":"qwen","messages":[{"role":"user","content":"数到五"}],"stream":true}'

# 4. 缓存未生效（连发两次，内容应完全一致而非被缓存）
curl -s $B/v1/models -H "Authorization: Bearer $K"
```

第 3 条最能说明问题。如果输出是一次性全部出现的，说明中间某层开了缓冲。

---

## 十一、免费版配额速查

与本项目相关的部分：

| 项目 | 免费版额度 | 是否够用 |
|---|---|---|
| CDN 流量 | 不限（但禁止托管视频和大文件） | 够 |
| 速率限制规则 | **1 条** | 够，用在 `/v1/` 上 |
| WAF 托管规则集 | Free Managed Ruleset（OWASP Top 10 核心） | 够 |
| WAF 自定义规则 | 可用 | 够 |
| Page Rules | 3 条 | 够 |
| DNS 记录 | 1000 条 | 远远够 |
| Zero Trust 用户数 | **50** | 白名单场景够 |
| 源站超时 | **100 秒** | 流式可绕过，注意首 token |
| 单文件缓存上限 | 512 MB | 与本项目无关 |

需要注意的两条：**速率限制只有 1 条规则**，所以要想清楚用在哪里；**100 秒超时不可调**，是免费版的硬约束。

---

## 附：完整配置顺序

如果从零开始，按这个顺序走：

1. 域名接入 Cloudflare，等 NS 生效
2. Zero Trust 初始化，起团队名
3. 服务器上跑 `cloudflared tunnel login` 和 `cloudflared tunnel create`
4. 回控制台确认 Tunnel 出现且 Healthy
5. 配置 SSL/TLS（Full + Always Use HTTPS）
6. 配置缓存绕过规则
7. 配置速率限制规则（先用 Log 观察，再切 Block）
8. 确认 Security Level 和 Under Attack 模式状态
9. 配置通知告警
10. 从外部网络跑第十节的验证清单
