# Zeabur/标准部署：泰国节点 Docker Hub + 官方 PyPI 均可达，无需镜像绕路。
# （北京旧部署用 docker.1ms.run + 阿里云 pip 镜像，见 git 历史；已迁移 Zeabur，弃用。）
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY webapp ./webapp

RUN mkdir -p /app/output

# Zeabur 注入 PORT；web 服务监听 $PORT（默认 8003）。
# cron 服务在 Zeabur 面板把启动命令覆盖为：python notify_lark.py
EXPOSE 8003
CMD ["sh", "-c", "uvicorn webapp.app:app --host 0.0.0.0 --port ${PORT:-8003}"]
