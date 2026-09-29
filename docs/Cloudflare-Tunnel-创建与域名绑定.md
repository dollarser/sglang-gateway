# Cloudflare Tunnel 创建与域名绑定（手把手）

面向第一次操作的人，每一步都有**确切命令**、**预期输出**和**怎么验证**。

域名已接入 Cloudflare、NS 已生效的情况下，从这里开始。

---

## 零、先搞清楚这三样东西

**Tunnel（隧道）是什么**

你家里/机房的机器没有公网 IP，外面的用户访问不到。Tunnel 的做法是：你的机器**主动向外**连到 Cloudflare，建立一条长连接。之后公网用户访问 `https://你的域名`，请求会顺着这条连接被送进你的机器。

关键点：**你的机器始终是「往外连」的一方**，所以不需要公网 IP，也不需要路由器做端口映射，更不需要在防火墙开任何入站端口。

**三个角色，别搞混**

| 角色 | 是什么 | 在哪配置 |
|---|---|---|
| 域名（`llm.example.com`） | 用户访问的地址 | 主控制台 `dash.cloudflare.com` |
| Tunnel（隧道） | 那条长连接，有唯一 ID | Zero Trust `one.dash.cloudflare.com` |
| cloudflared | 跑在你机器上的程序，负责建立连接 | 你的机器 |

**两个控制台是分开的**，这是最容易迷路的地方：

- 域名、DNS、WAF、缓存 → `dash.cloudflare.com`
- Tunnel、Access → `one.dash.cloudflare.com`

Tunnel 不在主控制台里。

**整个流程一共 5 步**

```
1. 创建隧道   →  得到一个 TUNNEL_ID 和凭证文件
2. 绑定域名   →  在 DNS 里加一条指向隧道的记录
3. 写配置文件 →  告诉隧道「哪个域名转到本机哪个端口」
4. 前台试运行 →  确认能通
5. 装成服务   →  开机自启，不用管了
```

---

## 一、动手前的检查

先确认三件事，都是几分钟的事，但少一样后面就会卡住。

```bash
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
```

### 1.1 cloudflared 装了吗

```bash
which cloudflared && cloudflared --version
```

预期：输出路径和版本号。没有的话先装：

```bash
brew install cloudflared          # macOS
```

```bash
# Linux（Debian/Ubuntu）
curl -L --output /tmp/cloudflared.deb \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i /tmp/cloudflared.deb
```

### 1.2 登录过吗

```bash
ls -la ~/.cloudflared/cert.pem
```

预期：文件存在，权限 `-rw-------`。

**如果没有这个文件**，需要先登录：

```bash
cloudflared tunnel login
```

它会打印一个 URL 或自动打开浏览器。在浏览器里选择你要授权的域名，点授权。成功后 `cert.pem` 就生成了。

> 服务器上没有图形界面时，把打印出来的 URL 复制到你自己电脑的浏览器里打开，效果一样。
>
> 注意：`cert.pem` 只对**你授权的那一个域名**有效。有多个域名的话，`tunnel login` 可以重复执行分别授权。

> **划重点：整个流程只有这一步会碰到网页，而且只需要点一下「授权」。**
>
> 后面的创建隧道、绑定域名、写配置、启动，**全部在命令行完成，不用再打开 Cloudflare 控制台**。
> `cert.pem` 里存着一个 token，命令行工具就是拿它去调 Cloudflare API 的。
>
> 如果 `cert.pem` 已经存在（以前登录过），连这一步都能跳过——直接进下一步。

### 1.3 已有的隧道（避免撞名或误改）

```bash
cloudflared tunnel list
```

预期类似：

```
ID                                   NAME              CREATED              CONNECTIONS
a1b2c3d4-5e6f-7890-abcd-ef1234567890 my-other-tunnel   2026-01-01T00:00:00Z 2xlax01, ...
```

**这一列是已有隧道。如果你看到一条正在跑的隧道，那是别的用途的，不要动它，我们新建一条。** 名字别重复就行。

---

## 二、第 1 步：创建隧道

```bash
cloudflared tunnel create sglang-gateway
```

`sglang-gateway` 是隧道名字，随便起，但要和后面命令里的一致。

**预期输出：**

```
Tunnel credentials written to /Users/你的用户名/.cloudflared/<TUNNEL_ID>.json.
Created tunnel sglang-gateway with id <TUNNEL_ID>
```

**要做两件事：**

1. **记下这个 ID**（那一长串 UUID）。后面写配置文件要用。忘了也没关系，`cloudflared tunnel list` 随时能查到。
2. 确认凭证文件生成了：

```bash
ls -la ~/.cloudflared/*.json
```

**这一步做了什么**：在 Cloudflare 那边注册了一条隧道，并在本地生成了它的「身份证」文件。以后 cloudflared 靠这个文件证明「我是这条隧道」。

> 如果你打算在**另一台机器**上跑 cloudflared，这个 `.json` 文件必须一起拷过去，否则那台机器连不上。

---

## 三、第 2 步：绑定域名

```bash
cloudflared tunnel route dns sglang-gateway llm.example.com
```

**把 `llm.example.com` 换成你自己的域名**（比如 `api.你的域名.com`）。

**预期输出：**

```
Added CNAME llm.example.com which will route to this tunnel
```

**这一步做了什么**：在 Cloudflare 的 DNS 里自动创建了一条 CNAME 记录，把 `llm.example.com` 指向这条隧道。你不用手动去控制台加记录。

**怎么验证：**

去主控制台 `dash.cloudflare.com` → 选中你的域名 → 左侧 **DNS → Records**，应该能看到一条：

| Type | Name | Content |
|---|---|---|
| CNAME | llm | `a1b2c3d4-....cfargotunnel.com` |

看到 `cfargotunnel.com` 结尾就对了。

> **如果提示记录已存在**：说明这个域名已经有 A/CNAME 记录了（比如之前解析到别处）。加 `-f` 覆盖：
>
> ```bash
> cloudflared tunnel route dns -f sglang-gateway llm.example.com
> ```
>
> 覆盖前确认那条旧记录确实不要了。

---

## 四、第 3 步：写配置文件

创建 `~/.cloudflared/config.yml`：

```bash
cat > ~/.cloudflared/config.yml <<'EOF'
tunnel: 把这里换成你的TUNNEL_ID
credentials-file: /Users/你的用户名/.cloudflared/把这里换成你的TUNNEL_ID.json

ingress:
  - hostname: llm.example.com
    service: http://127.0.0.1:2233
  - service: http_status:404
EOF
```

> 上面的 `/Users/你的用户名` 是 macOS 的写法，Linux 上换成 `/home/你的用户名`。**两个路径都必须是绝对路径**，不能写 `~/`。

**逐行解释：**

| 行 | 作用 |
|---|---|
| `tunnel:` | 告诉 cloudflared 跑哪条隧道。填 ID 或隧道名都行，**推荐填 ID**（名字改了也不影响） |
| `credentials-file:` | 凭证文件路径，就是第 1 步生成的那个 |
| `ingress:` | 路由规则列表，从上往下匹配 |
| `- hostname: ...` | 匹配这个域名 |
| `service: http://127.0.0.1:2233` | 匹配到之后，转发到本机的 **2233 端口**（网关的端口） |
| `- service: http_status:404` | **兜底规则**，前面都没匹配上就返回 404 |

**两个最容易踩的坑：**

1. **`service` 要指向网关的 2233，不是 SGLang 的 30007。** 用户的请求必须先过网关做鉴权，不能直连推理服务。
2. **最后那条 `- service: http_status:404` 不能少。** 它是「其他情况」的兜底。没有它，cloudflared 启动时会直接报错，或者把不匹配的请求转到错误的地方。

**校验配置有没有写错：**

```bash
cloudflared tunnel ingress validate
```

预期：`OK`。如果报错，多半是 YAML 缩进问题（必须是空格，不能是 Tab）。

**再确认一下路由会走到哪：**

```bash
cloudflared tunnel ingress rule https://llm.example.com/v1/models
```

预期输出会显示匹配到哪条规则、转发到哪个 service。这一步能提前发现「域名写错」或「端口写错」。

---

## 五、第 4 步：前台试运行

先别急着装服务，在前台跑一次，能看到实时日志，出问题好排查。

**先确认网关在跑：**

```bash
curl -s http://127.0.0.1:2233/healthz
```

预期：`{"status":"ok"}`。不是这个就先解决网关，别往下走。

**然后启动隧道：**

```bash
cloudflared tunnel run sglang-gateway
```

**预期输出**（关键几行）：

```
INF Registered tunnel connection connIndex=0 connection=... location=lax01 protocol=quic
INF Registered tunnel connection connIndex=1 connection=... location=sjc06 protocol=quic
```

看到 **`Registered tunnel connection`** 就是连上了。会打印 4 条（连到 4 个不同的 Cloudflare 边缘节点，做冗余）。

**另开一个终端验证：**

```bash
# 1) 不带 Key —— 期望 401（说明请求到达了网关，且鉴权生效）
curl -s -o /dev/null -w "%{http_code}\n" https://llm.example.com/v1/models

# 2) 带正确 Key —— 期望 200 和模型列表
curl -s https://llm.example.com/v1/models \
  -H "Authorization: Bearer sk-qw-你的密钥"
```

**结果对照表：**

| 现象 | 说明 |
|---|---|
| 401 | ✅ 通了，只是没带 Key。这是**最理想**的结果 |
| 200 + 模型列表 | ✅ 完全正常 |
| 404 | 域名写错了，或 config.yml 的 hostname 对不上 |
| 502 / 1033 | 隧道通了但连不上本机 2233 —— 网关没跑，或端口写错 |
| 530 / 1016 | DNS 还没生效，等几分钟 |
| 连不上 / 超时 | 隧道没起来，看 `cloudflared tunnel run` 的输出 |

确认没问题后，回前台终端按 `Ctrl+C` 停掉。

---

## 六、第 5 步：装成后台服务

```bash
cloudflared service install
```

**macOS 上不需要 sudo**，它装的是一个**用户级 launch agent**（`~/Library/LaunchAgents/com.cloudflare.cloudflared.plist`）。

装完后：

```bash
# 查看状态
launchctl list | grep cloudflare

# 看日志
tail -f /Users/你的用户名/.cloudflared/*.log
```

**Linux 上**要 sudo，装成 systemd 服务：

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared
```

> Linux 下 systemd 服务以 root 运行，它读的是 `/etc/cloudflared/config.yml`。如果配置写在 `~/.cloudflared/`，需要拷过去：
>
> ```bash
> sudo mkdir -p /etc/cloudflared
> sudo cp ~/.cloudflared/config.yml /etc/cloudflared/
> sudo cp ~/.cloudflared/<TUNNEL_ID>.json /etc/cloudflared/
> sudo sed -i 's|/home/你的用户名/.cloudflared|<TUNNEL_ID>|' /etc/cloudflared/config.yml   # 注意改成正确路径
> sudo systemctl restart cloudflared
> ```

---

## 七、完整验证

全部跑一遍，确认端到端没问题：

```bash
# 1) 三个组件都在
curl -s http://127.0.0.1:30007/v1/models -H "Authorization: Bearer 后端密钥"   # SGLang
curl -s http://127.0.0.1:2233/healthz                                        # 网关
launchctl list | grep cloudflare                                             # 隧道（macOS）

# 2) 公网可达 + 鉴权生效
curl -s -o /dev/null -w "%{http_code}\n" https://llm.example.com/v1/models                       # 401
curl -s -o /dev/null -w "%{http_code}\n" https://llm.example.com/v1/models \
  -H "Authorization: Bearer sk-qw-你的密钥"                                                       # 200

# 3) 流式输出正常（要能看到逐块吐字，不是一次性全出来）
curl -N https://llm.example.com/v1/chat/completions \
  -H "Authorization: Bearer sk-qw-你的密钥" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3.8-27B","messages":[{"role":"user","content":"数到十"}],"stream":true}'
```

**第 3 步很重要**。如果内容是一次性全部出现的，说明中间某层开了缓冲。Tunnel 本身不缓冲，但如果前面加过 Nginx，需要 `proxy_buffering off`。

---

## 八、常见错误对照

| 报错 / 现象 | 原因 | 处理 |
|---|---|---|
| `Cannot determine default configuration path` | 找不到 config.yml | 确认在 `~/.cloudflared/config.yml`，或用 `--config` 指定绝对路径 |
| `failed to parse config file` | YAML 缩进用了 Tab | 改成空格，缩进 2 格 |
| `no ingress rules were defined` | 少了 `ingress:` 或规则为空 | 检查 config.yml |
| `Cannot find tunnel credentials` | 凭证文件路径错 | 检查 `credentials-file` 是否为绝对路径且文件存在 |
| `You are not logged in` | 没登录或 cert.pem 丢了 | 重新 `cloudflared tunnel login` |
| `tunnel with name X already exists` | 名字撞了 | 换个名字，或 `cloudflared tunnel delete X` 删掉旧的 |
| `record with that host already exists` | DNS 已有同名记录 | 加 `-f` 覆盖，或先去控制台删掉旧记录 |
| 页面 1033（隧道刚建好） | **新隧道边缘路由未同步**，此时各项检查都正常 | 等 1-2 分钟，还不行就**重启一次 cloudflared**，比重改配置有效 |
| 页面 1033（一直如此） | 隧道进程没在跑，或源站连不上 | `pgrep -fl cloudflared` 看进程；`cloudflared tunnel info <名字>` 看连接数；再确认网关在 2233 |
| 页面 530 / 1016 | DNS 未生效 | 等几分钟；检查 NS 是否已指向 Cloudflare |
| 502 | 转发到了错误端口 | config.yml 里 `service` 应为 `http://127.0.0.1:2233` |
| **403 / error code 1010** | 被 Cloudflare 安全策略拦（浏览器完整性检查 / Under Attack） | 见下方「1010 专项」 |
| 一切正常但很慢 | 走了系统代理 | 确认 `NO_PROXY` 含 `127.0.0.1,localhost` |

### 1010 专项：Cloudflare 拦了你的客户端 UA

公网访问返回 `403 error code: 1010`，请求**根本没到隧道**。这是**浏览器完整性检查**
（Browser Integrity Check）判定 User-Agent 是自动化工具。

实测（同一域名、同一路径，只改 UA）：

| User-Agent | 结果 |
|---|---|
| `curl/8.7.1` | ✅ 200 |
| `python-requests/2.32.3` | ✅ 200 |
| `httpx/0.28.1` | ✅ 200 |
| `OpenAI/Python 1.60.0` | ✅ 200 |
| `OpenAI/NodeJS 4.77.0` | ✅ 200 |
| `PostmanRuntime/7.36.0` | ✅ 200 |
| 浏览器 UA / 空 UA | ✅ 200 |
| **`Python-urllib/3.13`** | ❌ **403** |

**影响面**：只有用标准库 `urllib` 手写脚本的场景会中招；`openai` SDK、`requests`、`httpx`、curl 都不受影响。

**两种处理方式**：

1. **推荐**——关掉浏览器完整性检查：
   `dash.cloudflare.com` → 选域名 → **Security → Settings** → **Browser Integrity Check** 关。
   纯 API 域名不需要这项检查。

2. 临时绕过——给客户端换个正常 UA：
   ```python
   urllib.request.Request(url, headers={"User-Agent": "OpenAI/Python 1.60.0"})
   ```

> 注意：用 `cloudflared tunnel login` 得到的 `cert.pem`，里面那个 `cfut_` 开头的 Token
> **只有 Tunnel 相关权限**，调不了 zone 级别的安全设置接口，这一步必须去控制台点。

**看实时日志排查：**

```bash
# 前台运行时直接看输出；装成服务后：
tail -f /Users/你的用户名/.cloudflared/*.log        # macOS
sudo journalctl -u cloudflared -f                # Linux
```

---

## 附：一次性照抄版

把 `llm.example.com` 换成你的域名，按顺序执行：

```bash
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# 1. 创建隧道（记下输出的 ID）
cloudflared tunnel create sglang-gateway
cloudflared tunnel list          # 从 NAME 列找到 sglang-gateway，复制它的 ID

# 2. 绑定域名
cloudflared tunnel route dns sglang-gateway llm.example.com

# 3. 写配置（把两个 <TUNNEL_ID> 换成上一步的 ID）
cat > ~/.cloudflared/config.yml <<'EOF'
tunnel: <TUNNEL_ID>
credentials-file: /Users/你的用户名/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: llm.example.com
    service: http://127.0.0.1:2233
  - service: http_status:404
EOF

# 4. 校验
cloudflared tunnel ingress validate

# 5. 前台试运行（看到 Registered tunnel connection 即成功，另开终端测公网访问）
cloudflared tunnel run sglang-gateway

# 6. Ctrl+C 停掉后，装成后台服务
cloudflared service install
```

---

## 下一步

隧道通了之后，还有 4 项 Cloudflare 控制台设置必须做，否则 API 客户端会被拦住。见《Cloudflare-控制台配置清单.md》：

1. **速率限制** —— 动作必须选 **Block**，不能选 Managed Challenge
2. **关闭 Under Attack 模式**，Security Level 设 Medium
3. **缓存绕过** `/v1/*`
4. **SSL/TLS** 设为 Full
