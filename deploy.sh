#!/usr/bin/env bash
# 把量化项目部署到服务器并在 docker 里跑回测（数据同步）。
# 用法：  ./deploy.sh
#   token + DB 凭证从服务器的 ~/quant.env 读取（首次用下面命令创建一次）：
#     cat > ~/quant.env <<E
#     TUSHARE_TOKEN=你的token
#     DB_HOST=mysql
#     DB_PORT=3306
#     DB_USER=root
#     DB_PASSWORD=你的MySQL密码
#     DB_NAME=quant
#     E
#     chmod 600 ~/quant.env
set -euo pipefail

SERVER=ctj_xujz
REMOTE_DIR=$(ssh "$SERVER" 'echo $HOME/quant')

echo "==> 1/3 同步代码到 $SERVER:$REMOTE_DIR"
ssh "$SERVER" "test -f ~/quant.env || { echo '❌ 服务器缺 ~/quant.env，见 deploy.sh 注释创建'; exit 1; }"
ssh "$SERVER" "mkdir -p $REMOTE_DIR/output"
rsync -az --delete \
  --exclude '__pycache__' --exclude 'output' --exclude '.git' \
  ./ "$SERVER:$REMOTE_DIR/"

echo "==> 2/3 构建镜像 quant-bt"
ssh "$SERVER" "cd $REMOTE_DIR && docker build -t quant-bt ."

echo "==> 3/3 运行回测容器（token/DB 从 ~/quant.env 读）"
ssh "$SERVER" "bash -s" <<EOF
set -e
docker rm -f quant-bt 2>/dev/null || true
docker run --rm --name quant-bt \
  --network app-net --env-file \$HOME/quant.env \
  -v $REMOTE_DIR/output:/app/output \
  quant-bt python run.py
EOF

echo "==> 拉回净值图到本地"
rsync -az "$SERVER:$REMOTE_DIR/output/" ./output/ 2>/dev/null || true
echo "✅ 完成。结果图在 ./output/（如有）"
