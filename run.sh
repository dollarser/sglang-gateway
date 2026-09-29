#!/usr/bin/env bash
#
# SGLang API 网关管理脚本（默认脱离终端在后台运行）
#
# 用法：
#   ./run.sh              # 默认：后台脱离终端启动
#   ./run.sh start        # 后台脱离终端启动
#   ./run.sh stop         # 停止网关
#   ./run.sh restart      # 重启网关
#   ./run.sh status       # 查看运行状态
#   ./run.sh logs         # 查看实时日志
#   ./run.sh fg           # 前台运行（调试用）
#
set -euo pipefail

# 收紧新建文件的权限。
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------- 配置区：按实际情况调整 ----------------

export SGLANG_BASE_URL="${SGLANG_BASE_URL:-http://127.0.0.1:30007}"
export SGLANG_API_KEY="${SGLANG_API_KEY:-}"
export GATEWAY_DB="${GATEWAY_DB:-$SCRIPT_DIR/data/gateway.db}"
export GLOBAL_MAX_CONCURRENT="${GLOBAL_MAX_CONCURRENT:-4}"
export MAX_OUTPUT_TOKENS="${MAX_OUTPUT_TOKENS:-0}"
export MAX_BODY_BYTES="${MAX_BODY_BYTES:-62914560}"
export MODEL_MAX_LEN="${MODEL_MAX_LEN:-0}"
export COMPAT_REWRITE="${COMPAT_REWRITE:-1}"
export COMPAT_CONTEXT_RETRY="${COMPAT_CONTEXT_RETRY:-1}"
export AUTH_FAIL_MAX="${AUTH_FAIL_MAX:-60}"
export CHARS_PER_TOKEN="${CHARS_PER_TOKEN:-2.0}"
export CORS_ORIGINS="${CORS_ORIGINS:-*}"
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-2233}"
PYTHON="${PYTHON:-python3}"

export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost}"
export no_proxy="$NO_PROXY"

DATA_DIR="$(dirname "$GATEWAY_DB")"
mkdir -p "$DATA_DIR"

PID_FILE="$DATA_DIR/gateway.pid"
LOG_FILE="$DATA_DIR/gateway.log"

_get_pid() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null || true)
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return 0
        fi
    fi
    local port_pid
    port_pid=$(lsof -ti :"$PORT" -sTCP:LISTEN 2>/dev/null | head -n1 || true)
    if [[ -n "$port_pid" ]] && kill -0 "$port_pid" 2>/dev/null; then
        echo "$port_pid"
        return 0
    fi
    return 1
}

status_gateway() {
    local pid
    if pid=$(_get_pid); then
        echo "网关状态: 正在运行 (PID: $pid)"
        echo "  监听地址: http://$HOST:$PORT"
        echo "  数据库:   $GATEWAY_DB"
        echo "  日志文件: $LOG_FILE"
        if curl -s -m 2 "http://$HOST:$PORT/v1/models" >/dev/null 2>&1 || [ $? -eq 22 ] || [ $? -eq 0 ]; then
            echo "  健康状态: HTTP 响应就绪"
        fi
        return 0
    else
        echo "网关状态: 未运行"
        return 1
    fi
}

stop_gateway() {
    local pid
    if pid=$(_get_pid); then
        echo "正在停止网关 (PID: $pid)..."
        kill "$pid" 2>/dev/null || true
        for _ in {1..30}; do
            if ! kill -0 "$pid" 2>/dev/null; then
                rm -f "$PID_FILE"
                echo "网关已成功停止。"
                return 0
            fi
            sleep 0.2
        done
        echo "进程未退出，强制终止..."
        kill -9 "$pid" 2>/dev/null || true
        rm -f "$PID_FILE"
        echo "网关已强制终止。"
    else
        echo "网关未运行，无需停止。"
        rm -f "$PID_FILE"
    fi
}

start_gateway() {
    local existing_pid
    if existing_pid=$(_get_pid); then
        echo "网关已在运行 (PID: $existing_pid)，请勿重复启动。"
        echo "  查看状态: ./run.sh status"
        echo "  查看日志: ./run.sh logs"
        echo "  重启网关: ./run.sh restart"
        return 0
    fi

    echo "================ 启动网关服务 (脱离终端后台运行) ================"
    echo "  SGLang 后端:   $SGLANG_BASE_URL"
    echo "  监听地址:      http://$HOST:$PORT"
    echo "  数据库:        $GATEWAY_DB"
    echo "  全局并发上限:  $GLOBAL_MAX_CONCURRENT"
    echo "  请求体上限:    $(( MAX_BODY_BYTES / 1024 / 1024 )) MB"
    echo "  输出上限:      $MAX_OUTPUT_TOKENS (0=不限)"
    echo "  日志文件:      $LOG_FILE"
    echo "  PID 文件:      $PID_FILE"
    echo "=================================================================="

    # 脱离终端核心机制：
    # 1. setsid 创建全新 session，彻底脱离父终端进程组与控制终端
    # 2. 重定向 stdin </dev/null，stdout/stderr >> "$LOG_FILE"
    # 3. disown 从当前 shell 作业表中剥离
    setsid "$PYTHON" -m uvicorn app:app --host "$HOST" --port "$PORT" "$@" </dev/null >> "$LOG_FILE" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PID_FILE"
    disown "$new_pid" 2>/dev/null || true

    sleep 2
    if kill -0 "$new_pid" 2>/dev/null; then
        echo "✅ 网关已成功在后台启动并脱离终端 (PID: $new_pid)"
        echo "  查看状态: ./run.sh status"
        echo "  查看日志: ./run.sh logs"
        echo "  停止服务: ./run.sh stop"
    else
        echo "❌ 网关启动失败，最后日志如下："
        tail -n 25 "$LOG_FILE" 2>/dev/null || true
        rm -f "$PID_FILE"
        exit 1
    fi
}

run_fg() {
    local existing_pid
    if existing_pid=$(_get_pid); then
        echo "错误：检测到已有网关在运行 (PID: $existing_pid)，请先停止：./run.sh stop"
        exit 1
    fi
    echo "在前台启动网关（绑定当前终端，按 Ctrl+C 退出）..."
    exec "$PYTHON" -m uvicorn app:app --host "$HOST" --port "$PORT" "$@"
}

logs_gateway() {
    if [[ ! -f "$LOG_FILE" ]]; then
        echo "日志文件不存在: $LOG_FILE"
        exit 1
    fi
    echo "实时跟踪网关日志 ($LOG_FILE) - 按 Ctrl+C 退出："
    tail -n 50 -f "$LOG_FILE"
}

ACTION="${1:-start}"
case "$ACTION" in
    start)
        shift 1 2>/dev/null || true
        start_gateway "$@"
        ;;
    stop)
        stop_gateway
        ;;
    restart)
        shift 1 2>/dev/null || true
        stop_gateway
        sleep 1
        start_gateway "$@"
        ;;
    status)
        status_gateway
        ;;
    logs)
        logs_gateway
        ;;
    fg|--fg|--foreground)
        shift 1 2>/dev/null || true
        run_fg "$@"
        ;;
    *)
        start_gateway "$@"
        ;;
esac
