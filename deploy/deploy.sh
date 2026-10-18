#!/usr/bin/env bash
# MediaMTX live 一键部署（在 live 服务器上执行）
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo ">>> 已生成 .env 模板，请先编辑 .env（公网 IP 与认证密码），再重新执行 ./deploy.sh"
  exit 1
fi

echo ">>> 启动 MediaMTX..."
docker compose up -d --force-recreate

echo ">>> 容器状态："
docker compose ps

echo ">>> 若启用防火墙（ufw），放行以下端口："
echo "    sudo ufw allow 8554/tcp   # RTSP 推流/拉流"
echo "    sudo ufw allow 8889/tcp   # WebRTC 页面/WHEP"
echo "    sudo ufw allow 8189/udp   # WebRTC ICE 数据面（必须，否则黑屏）"
echo "    sudo ufw allow 8890/udp   # SRT（可选）"
