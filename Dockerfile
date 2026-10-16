# 云phone设备管理平台 V2.2.1
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
    PORT=9111 \
    RELAY_PORT=9000

WORKDIR /app

# ==== 构建期可调参数（弱网 / 代理 / 内网 mirror 环境用 --build-arg 传入）====
# 代理：也可直接用 Docker 客户端环境变量 HTTP_PROXY/HTTPS_PROXY/NO_PROXY
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
# apt 镜像源（默认阿里云；可覆盖，如 mirrors.tuna.tsinghua.edu.cn；传 deb.debian.org 用官方源）
ARG APT_MIRROR=mirrors.aliyun.com
# apt 源协议：默认 https（很多环境只放行 443）；若内网源仅支持 http，改成 http
ARG APT_SCHEME=https
# pip 镜像源（默认阿里云；可覆盖，如 https://pypi.tuna.tsinghua.edu.cn/simple）
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com

# 安装 adb（用于投屏/触控），并预创建运行期目录
# 弱网/代理环境下 apt 偶发失败（exit 100），这里做多次重试；并支持切换内网 mirror
RUN set -eux; \
    mirror="${APT_MIRROR:-mirrors.aliyun.com}"; \
    scheme="${APT_SCHEME:-https}"; \
    if [ -n "$mirror" ] && [ "$mirror" != "deb.debian.org" ]; then \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do \
            [ -f "$f" ] && sed -i -E "s|https?://deb\.debian\.org|$scheme://$mirror|g" "$f" || true; \
        done; \
    fi; \
    echo ">>> apt source: $scheme://$mirror"; \
    grep -rh 'URIs\|^deb' /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null || true; \
    ok=0; \
    for i in 1 2 3 4 5; do \
        if apt-get -o Acquire::Retries=3 update \
           && apt-get -o Acquire::Retries=3 install -y --no-install-recommends adb; then \
            ok=1; break; \
        fi; \
        echo ">>> apt attempt $i failed, retrying in 5s..."; \
        rm -rf /var/lib/apt/lists/*; \
        sleep 5; \
    done; \
    [ "$ok" = "1" ] || { echo ">>> apt-get failed after retries"; exit 1; }; \
    rm -rf /var/lib/apt/lists/*; \
    mkdir -p /app/data /app/uploads /app/app_uploads /app/seed

COPY requirements.txt .

# pip 安装同样支持内网源与重试
RUN set -eux; \
    pip_args="--no-cache-dir"; \
    if [ -n "${PIP_INDEX_URL:-}" ]; then pip_args="$pip_args --index-url ${PIP_INDEX_URL}"; fi; \
    if [ -n "${PIP_TRUSTED_HOST:-}" ]; then pip_args="$pip_args --trusted-host ${PIP_TRUSTED_HOST}"; fi; \
    ok=0; \
    for i in 1 2 3 4 5; do \
        if pip install $pip_args -r requirements.txt; then ok=1; break; fi; \
        echo ">>> pip attempt $i failed, retrying in 5s..."; sleep 5; \
    done; \
    [ "$ok" = "1" ] || { echo ">>> pip install failed after retries"; exit 1; }

COPY . .

# 把仓库中的 data.db 作为“种子数据库”，首次启动时由 entrypoint 复制到持久化目录
RUN if [ -f /app/data.db ]; then cp /app/data.db /app/seed/data.db; fi \
    && sed -i 's/\r$//' /app/docker-entrypoint.sh \
    && chmod +x /app/docker-entrypoint.sh

EXPOSE 9111 9000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["waitress-serve", "--host=0.0.0.0", "--port=9111", "--threads=24", "app:app"]
