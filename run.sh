#!/usr/bin/env bash
#
# SGLang API 网关启动脚本
#
# 用法：
#   ./run.sh                # 用下面的默认值启动
#   PORT=3000 ./run.sh      # 临时改端口
#   ./run.sh --reload       # 开发模式，改代码自动重载（不要用于生产）
#
set -euo pipefail

# 收紧新建文件的权限。默认 umask 常见是 002，会让 gateway.db / gateway.log
# 变成 664（同组可读写）、gateway-data/ 变成 775。数据库里是 Key 哈希和审计日志，
# 日志里有客户端 IP，都不该给同机其他用户看。
umask 077

cd "$(dirname "$0")"

# ---------------- 配置区：按你的实际情况改这里 ----------------

# SGLang 服务地址。**直连裸 SGLang**，不要填兼容代理的端口——
# 兼容改写（developer 角色、上下文溢出重试）已内化到 sglang_compat.py，多一跳没有意义。
export SGLANG_BASE_URL="${SGLANG_BASE_URL:-http://127.0.0.1:30007}"

# SGLang 若启用了 --api-key，在这里填；没启用就留空
export SGLANG_API_KEY="${SGLANG_API_KEY:-}"

# 数据库绝对路径。不要用相对路径——服务的工作目录一变就找不到库了
export GATEWAY_DB="${GATEWAY_DB:-$HOME/gateway-data/gateway.db}"

# 全局在途请求上限。这是安全阀，放宽至 32，从容支撑多 Agent / 并发请求
export GLOBAL_MAX_CONCURRENT="${GLOBAL_MAX_CONCURRENT:-32}"

# 单次输出上限。
#   0 = 不限制（推荐），由「模型上下文窗口」+「当日剩余配额」两级约束
#   >0 = 硬上限
# 推理模型（带思维链）尤其不要设小，否则预算会被思维链吃光、正文为空
export MAX_OUTPUT_TOKENS="${MAX_OUTPUT_TOKENS:-0}"

# 请求体体积上限（默认放宽至 100MB，对齐 Cloudflare 上限，从容支持图片/视频与超长 Agent 上下文）
export MAX_BODY_BYTES="${MAX_BODY_BYTES:-104857600}"

# 模型上下文窗口。留空/0 表示启动时自动从 /v1/models 探测。
# 探测到之后，超过窗口的 max_tokens 会被钳到窗口大小，让请求正常返回内容，
# 而不是拿到一个 400。探测失败时可用这个变量手工指定。
export MODEL_MAX_LEN="${MODEL_MAX_LEN:-0}"

# 兼容改写：developer 角色折叠进 system、reasoning_effort 别名、摘掉 output_config。
# 上游升级后不再需要时可以设为 0 关掉，不用改代码。
export COMPAT_REWRITE="${COMPAT_REWRITE:-1}"

# 上下文溢出自动重试：上游因「输入 + max_tokens 超窗口」返回 400 时，
# 从错误文案反算可用预算、降 max_tokens 重试一次。
export COMPAT_CONTEXT_RETRY="${COMPAT_CONTEXT_RETRY:-1}"

# 单个 IP 每分钟允许的鉴权失败次数，放宽至 60 避免误伤同 NAT 正常用户
export AUTH_FAIL_MAX="${AUTH_FAIL_MAX:-60}"

# 拿不到 usage 时的字节数折算系数。中文场景实测约 3.3-4.5，2.0 会高估约 2 倍。
# 高估方向是安全的（防止配额被绕过），想更贴近实际可以调到 3.0
export CHARS_PER_TOKEN="${CHARS_PER_TOKEN:-2.0}"

# 允许的跨域来源。只给 SDK/CLI 用保持 * 即可；有网页前端时收紧到具体域名
export CORS_ORIGINS="${CORS_ORIGINS:-*}"

# 网关监听地址与端口
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-2233}"

# Python 解释器。部署机上是虚拟环境的话，改成虚拟环境的绝对路径
PYTHON="${PYTHON:-python3}"

# ---------------- 以下一般不用改 ----------------

# 兜底：避免走了系统代理导致连不上本地 SGLang
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost}"
export no_proxy="$NO_PROXY"

mkdir -p "$(dirname "$GATEWAY_DB")"

echo "启动网关"
echo "  SGLang 后端   $SGLANG_BASE_URL"
echo "  监听          http://$HOST:$PORT"
echo "  数据库        $GATEWAY_DB"
echo "  全局并发上限  $GLOBAL_MAX_CONCURRENT"
echo "  请求体上限    $(( MAX_BODY_BYTES / 1024 / 1024 )) MB"
echo "  输出上限      $MAX_OUTPUT_TOKENS (0=不限)"
echo "  兼容改写      $COMPAT_REWRITE / 上下文重试 $COMPAT_CONTEXT_RETRY"
echo

exec "$PYTHON" -m uvicorn app:app --host "$HOST" --port "$PORT" "$@"
