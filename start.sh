#!/bin/bash
cd "$(dirname "$0")"

pip3 install -q -r requirements.txt 2>/dev/null

echo "============================================"
echo "  汉口北集团不动产信息检索"
echo "============================================"
echo ""

# Kill old processes
lsof -ti:8000 2>/dev/null | xargs kill -9 2>/dev/null
pkill -f "ssh.*-R.*10000" 2>/dev/null
sleep 1

# ── 1. Start web server ──
echo "[1/2] 启动本地服务..."
python3 server.py &
sleep 2

# ── 2. Start persistent SSH tunnel ──
echo "[2/2] 建立公网隧道..."
export SSHPASS='SSH_PASSWORD'
while true; do
  sshpass -e ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -N -R 10000:localhost:8000 root@YOUR_SERVER_IP
  sleep 5
done &
sleep 4

echo ""
echo "================================================================="
echo "  本地访问: http://localhost:8000"
echo ""
echo "  公网访问: http://YOUR_SERVER_IP"
echo ""
echo "  地址固定不变，备案通过后绑定域名 bwin369.fun"
echo "================================================================="
echo ""
echo "  按 Ctrl+C 停止所有服务"

wait
