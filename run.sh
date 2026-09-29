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

cd "$(dirname "$0")"

# ---------------- 配置区：按你的实际情况改这里 ----------------

# SGLang 服务地址。注意不是默认的 30000，你的服务在 30008
export SGLANG_BASE_URL="${SGLANG_BASE_URL:-http://127.0.0.1:30008}"

# SGLang 若启用了 --api-key，在这里填；没启用就留空
export SGLANG_API_KEY="${SGLANG_API_KEY:-}"

# 数据库绝对路径。不要用相对路径——服务的工作目录一变就找不到库了
export GATEWAY_DB="${GATEWAY_DB:-$HOME/gateway-data/gateway.db}"

# 全局在途请求上限。这是安全阀，不是目标值，按 GPU 实际能力设
export GLOBAL_MAX_CONCURRENT="${GLOBAL_MAX_CONCURRENT:-16}"

# 单次输出上限。
#   0 = 不限制（推荐），由「模型上下文窗口」+「当日剩余配额」两级约束
#   >0 = 硬上限
# 推理模型（带思维链）尤其不要设小，否则预算会被思维链吃光、正文为空
export MAX_OUTPUT_TOKENS="${MAX_OUTPUT_TOKENS:-0}"

# 模型上下文窗口。留空/0 表示启动时自动从 /v1/models 探测。
# 探测到之后，超过窗口的 max_tokens 会被钳到窗口大小——因为 SGLang 对超限值
# 返回的是空 body 的 400，客户端看不出原因。探测失败时可用这个变量手工指定。
export MODEL_MAX_LEN="${MODEL_MAX_LEN:-0}"

# 单个 IP 每分钟鉴权失败上限
export AUTH_FAIL_MAX="${AUTH_FAIL_MAX:-20}"

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
echo "  输出上限      $MAX_OUTPUT_TOKENS"
echo

exec "$PYTHON" -m uvicorn app:app --host "$HOST" --port "$PORT" "$@"
