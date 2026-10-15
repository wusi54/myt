# 云phone设备管理平台 V2.1.0
# 单镜像：同时用于 web(Flask/waitress) 与 relay(投屏WebSocket) 两个服务
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    ADB_PATH=/usr/bin/adb \
    DB_PATH=/app/data/data.db \
    UPLOAD_DIR=/app/uploads \
    APP_UPLOAD_DIR=/app/app_uploads \
    PORT=5001 \
    RELAY_PORT=9000

WORKDIR /app

# 安装 adb（用于投屏/触控），并预创建运行期目录
RUN apt-get update \
    && apt-get install -y --no-install-recommends adb \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app/data /app/uploads /app/app_uploads /app/seed

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 把仓库中的 data.db 作为“种子数据库”，首次启动时由 entrypoint 复制到持久化目录
RUN if [ -f /app/data.db ]; then cp /app/data.db /app/seed/data.db; fi \
    && sed -i 's/\r$//' /app/docker-entrypoint.sh \
    && chmod +x /app/docker-entrypoint.sh

EXPOSE 5001 9000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["waitress-serve", "--host=0.0.0.0", "--port=5001", "--threads=24", "app:app"]
