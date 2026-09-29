#!/usr/bin/env bash
#
# 启动 Cloudflare 隧道脚本
# 使用项目内的配置和凭据，不依赖 ~/.cloudflared
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.yml"

echo "启动 Cloudflare 隧道..."
echo "  配置文件: $CONFIG_FILE"

exec cloudflared tunnel --no-autoupdate --config "$CONFIG_FILE" run sglang-gateway
