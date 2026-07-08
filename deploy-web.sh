#!/usr/bin/env bash
# 部署量化可视化平台（web 服务，常驻）到服务器。
# 用法：  ./deploy-web.sh
#   （不需要 TUSHARE_TOKEN —— web 只读库，数据同步用 deploy.sh 单独做）
set -euo pipefail

SERVER=ctj_xujz
PORT=8003
# 放 home 下（/opt 需 root）。取远端绝对路径，docker -v 需要绝对路径
REMOTE_DIR=$(ssh "$SERVER" 'echo $HOME/quant')

echo "==> 1/4 同步代码到 $SERVER:$REMOTE_DIR"
ssh "$SERVER" "mkdir -p $REMOTE_DIR/output"
rsync -az --delete \
  --exclude '__pycache__' --exclude 'output' --exclude '.git' \
  --exclude 'notify.log' \
  ./ "$SERVER:$REMOTE_DIR/"

echo "==> 2/4 构建镜像 quant-bt（含 web）"
ssh "$SERVER" "cd $REMOTE_DIR && docker build -t quant-bt ."

echo "==> 3/4 启动/重启 web 容器 quant-web（常驻，app-net，端口 $PORT）"
ssh "$SERVER" "bash -s" <<EOF
set -e
test -f \$HOME/quant.env || { echo '❌ 缺 ~/quant.env'; exit 1; }
docker rm -f quant-web 2>/dev/null || true
# env-file 提供 DB 连接 + TUSHARE_TOKEN + TWELVEDATA_KEY
docker run -d --name quant-web --restart always \
  --network app-net -p $PORT:8003 \
  --env-file \$HOME/quant.env \
  quant-bt uvicorn webapp.app:app --host 0.0.0.0 --port 8003

sleep 3
echo "--- 容器状态 ---"
docker ps --filter name=quant-web --format "{{.Names}} {{.Status}}"
echo "--- 健康检查 ---"
docker exec quant-web python -c "import urllib.request,json; print(urllib.request.urlopen('http://localhost:8003/api/health').read().decode())" || echo "（健康检查稍后再试）"
EOF

echo "==> 4/4 nginx 子域名"
echo "    nginx 路由已手动整合进生产 /opt/ctj/nginx/default.conf（quant.xiuqilai.com → quant-web:8003）。"
echo "    本脚本不自动改 nginx，避免覆盖生产 CTJ 配置。如需调整见 nginx-quant.conf 注释。"

echo ""
echo "✅ web 容器已起。访问： http://quant.xiuqilai.com"
echo "   （HTTPS 升级见 README「配置子域名 SSL」）"
