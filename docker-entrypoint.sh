#!/usr/bin/env bash
set -euo pipefail

DB="${DB_PATH:-/app/data/data.db}"
export DB_PATH="$DB"

# 确保运行期目录存在
mkdir -p "$(dirname "$DB")" "${UPLOAD_DIR:-/app/uploads}" "${APP_UPLOAD_DIR:-/app/app_uploads}"

# 首次启动：使用镜像内置的种子数据库初始化；
# 说明：种子库取自仓库根目录的 data.db，运行期数据写入挂载卷，互不影响。
if [ ! -s "$DB" ]; then
  if [ -s /app/seed/data.db ]; then
    cp /app/seed/data.db "$DB"
    echo "[entrypoint] 已从种子库初始化数据库 -> $DB"
  else
    python -c "import db; db.init_db(); print('[entrypoint] 已创建空数据库 ->', db.DB_PATH)"
  fi
fi

# 预启动 adb server（投屏 / 触控需要）
if command -v adb >/dev/null 2>&1; then
  adb start-server >/dev/null 2>&1 || true
fi

exec "$@"
